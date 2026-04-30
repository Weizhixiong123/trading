#!/usr/bin/env python3
"""
Cross-reference Binance price action with public hype sources.

Hype source:
  CoinGecko /search/trending — top 15 coins by user-search activity in 24h.
  Public, no auth, ~30 req/min free tier (we use 1 per run).

Price sources:
  - Spot 24h ticker:    api.binance.com/api/v3/ticker/24hr   (public, no auth)
  - Futures 24h ticker: fapi.binance.com/fapi/v1/ticker/24hr (public, no auth)

Output buckets:
  A) Trend       : top gainers that are also high-amplitude (real momentum).
  B) Crashed     : high-amplitude losers (don't catch the falling knife).
  C) Hype-driven : trending symbols that match a tradable USDT pair —
                   the cross of "retail attention" with "Binance liquidity".

Usage:
  python3 src/hype_radar.py
  EXTRA_HYPE_KEYWORDS="LUNC,RUNE" python3 src/hype_radar.py   # add manual overrides
"""
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_FILE = PROJECT_ROOT / "configs" / ".env"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


load_env_file(Path(os.getenv("CONFIG_FILE", DEFAULT_CONFIG_FILE)))

SPOT_TICKER_URL = "https://api.binance.com/api/v3/ticker/24hr"
FUTURES_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
COINGECKO_TRENDING_URL = "https://api.coingecko.com/api/v3/search/trending"

MIN_QUOTE_VOL = env_float("RADAR_MIN_QUOTE_VOL", 5_000_000)
TOP_GAINERS = env_int("RADAR_TOP_GAINERS", 15)
TOP_VOLATILE = env_int("RADAR_TOP_VOLATILE", 15)
TREND_MIN_GAIN_PCT = env_float("RADAR_TREND_MIN_GAIN_PCT", 5.0)
TREND_MIN_AMP_PCT = env_float("RADAR_TREND_MIN_AMP_PCT", 15.0)
CRASH_MAX_LOSS_PCT = env_float("RADAR_CRASH_MAX_LOSS_PCT", -10.0)
CRASH_MIN_AMP_PCT = env_float("RADAR_CRASH_MIN_AMP_PCT", 30.0)

REQ_TIMEOUT = env_int("REQ_TIMEOUT", 10)

WECOM_WEBHOOK_KEY = os.getenv("WECOM_KEY", "PUT_YOUR_WECOM_KEY_HERE")
WECOM_PUSH_TOP_N = env_int("RADAR_WECOM_TOP_N", 8)
WECOM_MAX_CONTENT = 3800  # leave headroom under WeCom's 4096-byte cap

# Stablecoins / pegged assets — drift around 1.00, never interesting.
STABLE_BASES = {
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP", "FDUSD", "PYUSD",
    "USDS", "USD1", "RLUSD", "BFUSD", "U", "XUSD", "AEUR", "EUR",
    "EURI", "PAXG", "XAUT",
}


class Row:
    __slots__ = ("base", "symbol", "change_pct", "amp_pct", "last", "quote_vol")

    def __init__(self, base: str, symbol: str, change_pct: float,
                 amp_pct: float, last: float, quote_vol: float):
        self.base = base
        self.symbol = symbol
        self.change_pct = change_pct
        self.amp_pct = amp_pct
        self.last = last
        self.quote_vol = quote_vol


class TrendingCoin:
    __slots__ = ("symbol", "name", "mc_rank", "score")

    def __init__(self, symbol: str, name: str, mc_rank: Optional[int], score: int):
        self.symbol = symbol
        self.name = name
        self.mc_rank = mc_rank
        self.score = score


def fetch_ticker(url: str) -> List[Row]:
    resp = requests.get(url, timeout=REQ_TIMEOUT)
    resp.raise_for_status()
    rows: List[Row] = []
    for d in resp.json():
        sym = d["symbol"]
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if base in STABLE_BASES:
            continue
        try:
            qv = float(d.get("quoteVolume") or 0)
            if qv < MIN_QUOTE_VOL:
                continue
            high = float(d["highPrice"])
            low = float(d["lowPrice"])
            if low <= 0:
                continue
            rows.append(Row(
                base=base,
                symbol=sym,
                change_pct=float(d["priceChangePercent"]),
                amp_pct=(high - low) / low * 100.0,
                last=float(d["lastPrice"]),
                quote_vol=qv,
            ))
        except (KeyError, ValueError, TypeError):
            continue
    return rows


def fetch_coingecko_trending() -> List[TrendingCoin]:
    resp = requests.get(
        COINGECKO_TRENDING_URL,
        timeout=REQ_TIMEOUT,
        headers={"accept": "application/json"},
    )
    resp.raise_for_status()
    out: List[TrendingCoin] = []
    for c in resp.json().get("coins", []):
        item = c.get("item") or {}
        sym = (item.get("symbol") or "").upper().strip()
        if not sym:
            continue
        out.append(TrendingCoin(
            symbol=sym,
            name=item.get("name") or sym,
            mc_rank=item.get("market_cap_rank"),
            score=int(item.get("score") or 0),
        ))
    return out


def load_extra_keywords() -> List[str]:
    raw = os.getenv("EXTRA_HYPE_KEYWORDS", "").strip()
    if not raw:
        return []
    seen: set = set()
    out: List[str] = []
    for x in raw.replace("\n", ",").split(","):
        k = x.strip().upper()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def fmt_price(p: float) -> str:
    if p >= 100:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.001:
        return f"{p:.6f}"
    return f"{p:.3e}"


def fmt_vol(v: float) -> str:
    if v >= 1e9:
        return f"{v/1e9:.2f}B"
    if v >= 1e6:
        return f"{v/1e6:.1f}M"
    return f"{v/1e3:.0f}K"


def render_table(title: str, rows: Iterable[Row]) -> None:
    rows = list(rows)
    print(f"\n=== {title} ===")
    if not rows:
        print("  (no rows match)")
        return
    print(f"  {'BASE':<10}{'CHG%':>8}{'AMP%':>8}{'LAST':>14}{'QVOL':>10}")
    for r in rows:
        print(f"  {r.base:<10}{r.change_pct:>8.2f}{r.amp_pct:>8.2f}"
              f"{fmt_price(r.last):>14}{fmt_vol(r.quote_vol):>10}")


def gainers(rows: List[Row], n: int) -> List[Row]:
    return sorted(rows, key=lambda r: r.change_pct, reverse=True)[:n]


def volatile(rows: List[Row], n: int) -> List[Row]:
    return sorted(rows, key=lambda r: r.amp_pct, reverse=True)[:n]


def trend_bucket(rows: List[Row]) -> List[Row]:
    return sorted(
        (r for r in rows
         if r.change_pct >= TREND_MIN_GAIN_PCT and r.amp_pct >= TREND_MIN_AMP_PCT),
        key=lambda r: (r.change_pct + r.amp_pct),
        reverse=True,
    )


def crash_bucket(rows: List[Row]) -> List[Row]:
    return sorted(
        (r for r in rows
         if r.change_pct <= CRASH_MAX_LOSS_PCT and r.amp_pct >= CRASH_MIN_AMP_PCT),
        key=lambda r: r.amp_pct,
        reverse=True,
    )


def merge_markets(spot: List[Row], fut: List[Row]) -> Dict[str, Tuple[Optional[Row], Optional[Row]]]:
    out: Dict[str, Tuple[Optional[Row], Optional[Row]]] = {}
    for r in spot:
        out[r.base] = (r, None)
    for r in fut:
        s, _ = out.get(r.base, (None, None))
        out[r.base] = (s, r)
    return out


def render_trending(coins: List[TrendingCoin]) -> None:
    print("\n=== CoinGecko Trending (last 24h search activity) ===")
    if not coins:
        print("  (CoinGecko returned nothing)")
        return
    print(f"  {'#':<4}{'SYM':<10}{'NAME':<24}{'MC_RANK':>9}")
    for i, c in enumerate(coins, 1):
        rank = str(c.mc_rank) if c.mc_rank else "-"
        print(f"  {i:<4}{c.symbol:<10}{c.name[:23]:<24}{rank:>9}")


def render_hype(
    trending: List[TrendingCoin],
    extras: List[str],
    merged: Dict[str, Tuple[Optional[Row], Optional[Row]]],
) -> None:
    print("\n=== C) Hype-driven (trending ∩ Binance USDT pairs) ===")
    keys: List[Tuple[str, str]] = []  # (symbol, source)
    seen: set = set()
    for c in trending:
        if c.symbol not in seen:
            seen.add(c.symbol)
            keys.append((c.symbol, "CoinGecko"))
    for k in extras:
        if k not in seen:
            seen.add(k)
            keys.append((k, "manual"))
    hits = [(sym, src, merged[sym]) for sym, src in keys if sym in merged]
    if not hits:
        print("  (no trending symbol matched a tradable USDT pair)")
        return
    print(f"  {'BASE':<10}{'SRC':<11}{'SPOT_CHG%':>11}{'SPOT_AMP%':>11}"
          f"{'FUT_CHG%':>11}{'FUT_AMP%':>11}")
    for base, src, (s, f) in hits:
        sc = f"{s.change_pct:>11.2f}" if s else f"{'-':>11}"
        sa = f"{s.amp_pct:>11.2f}" if s else f"{'-':>11}"
        fc = f"{f.change_pct:>11.2f}" if f else f"{'-':>11}"
        fa = f"{f.amp_pct:>11.2f}" if f else f"{'-':>11}"
        print(f"  {base:<10}{src:<11}{sc}{sa}{fc}{fa}")


def fmt_pct(p: float) -> str:
    sign = "+" if p > 0 else ""
    return f"{sign}{p:.1f}%"


def md_line_row(r: Row) -> str:
    return (f"`{r.base:<8}` {fmt_pct(r.change_pct):>7}  "
            f"amp {r.amp_pct:>5.1f}%  vol {fmt_vol(r.quote_vol)}")


def md_section(title: str, color: str, rows: List[Row], top_n: int) -> str:
    if not rows:
        return f"**{title}**\n> 无\n"
    body = "\n".join(f"> {md_line_row(r)}" for r in rows[:top_n])
    return f'<font color="{color}">**{title}**</font>\n{body}\n'


def md_hype_section(
    trending: List[TrendingCoin],
    extras: List[str],
    merged: Dict[str, Tuple[Optional[Row], Optional[Row]]],
) -> str:
    keys: List[str] = []
    seen: set = set()
    for c in trending:
        if c.symbol not in seen:
            seen.add(c.symbol)
            keys.append(c.symbol)
    for k in extras:
        if k not in seen:
            seen.add(k)
            keys.append(k)
    hits = [(sym, merged[sym]) for sym in keys if sym in merged]
    if not hits:
        return '<font color="comment">**C 舆情驱动**</font>\n> 无匹配\n'
    lines = []
    for base, (s, f) in hits:
        spot = fmt_pct(s.change_pct) if s else "  -  "
        fut = fmt_pct(f.change_pct) if f else "  -  "
        amp = max(s.amp_pct if s else 0, f.amp_pct if f else 0)
        lines.append(f"> `{base:<8}` 现货 {spot:>7} 合约 {fut:>7}  amp {amp:>5.1f}%")
    return '<font color="comment">**C 舆情驱动 (CoinGecko ∩ 币安)**</font>\n' + "\n".join(lines) + "\n"


def format_wecom_message(
    trending: List[TrendingCoin],
    spot: List[Row],
    fut: List[Row],
    trend: List[Row],
    crash: List[Row],
    extras: List[str],
) -> Tuple[str, str]:
    title = f"热度雷达 {datetime.now().strftime('%m-%d %H:%M')}"
    super_hits = []
    trending_syms = {c.symbol for c in trending}
    for r in trend:
        if r.base in trending_syms:
            super_hits.append(r)

    parts: List[str] = []
    if super_hits:
        body = "\n".join(f"> {md_line_row(r)}" for r in super_hits[:WECOM_PUSH_TOP_N])
        parts.append(f'<font color="warning">**S 超级信号 (趋势 ∩ 舆情)**</font>\n{body}\n')

    parts.append(md_hype_section(trending, extras, merge_markets(spot, fut)))
    parts.append(md_section(
        f"A 趋势 (chg≥{TREND_MIN_GAIN_PCT:.0f}% & amp≥{TREND_MIN_AMP_PCT:.0f}%)",
        "info", trend, WECOM_PUSH_TOP_N,
    ))
    parts.append(md_section(
        f"B 暴雷 (chg≤{CRASH_MAX_LOSS_PCT:.0f}% & amp≥{CRASH_MIN_AMP_PCT:.0f}%)",
        "warning", crash, WECOM_PUSH_TOP_N,
    ))

    content = "\n".join(parts).rstrip()
    if len(content.encode("utf-8")) > WECOM_MAX_CONTENT:
        # Trim from the bottom (B bucket is least actionable) until fits.
        while len(content.encode("utf-8")) > WECOM_MAX_CONTENT and "\n" in content:
            content = content.rsplit("\n", 1)[0]
    return title, content


def push_wecom(title: str, content: str) -> None:
    if WECOM_WEBHOOK_KEY.startswith("PUT_"):
        print("\n[radar] WECOM_KEY not set — skipping push (DRY-RUN).", file=sys.stderr)
        return
    url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM_WEBHOOK_KEY}"
    payload = {"msgtype": "markdown", "markdown": {"content": f"### {title}\n{content}"}}
    try:
        r = requests.post(url, json=payload, timeout=REQ_TIMEOUT)
        body = r.json()
        if body.get("errcode") != 0:
            print(f"[radar] WeCom push failed: {body}", file=sys.stderr)
        else:
            print(f"[radar] pushed to WeCom ({len(content)} chars).", file=sys.stderr)
    except Exception as e:
        print(f"[radar] WeCom push exception: {e}", file=sys.stderr)


def main() -> int:
    try:
        spot = fetch_ticker(SPOT_TICKER_URL)
        fut = fetch_ticker(FUTURES_TICKER_URL)
    except requests.RequestException as e:
        print(f"[radar] Binance fetch failed: {e}", file=sys.stderr)
        return 1

    trending: List[TrendingCoin] = []
    try:
        trending = fetch_coingecko_trending()
    except requests.RequestException as e:
        print(f"[radar] CoinGecko fetch failed: {e}", file=sys.stderr)

    extras = load_extra_keywords()

    print("# Hype Radar")
    print(f"# spot pairs={len(spot)}  futures pairs={len(fut)}  "
          f"min_qvol={fmt_vol(MIN_QUOTE_VOL)}")

    render_trending(trending)
    render_table("Spot — top gainers", gainers(spot, TOP_GAINERS))
    render_table("Spot — top volatile (amplitude)", volatile(spot, TOP_VOLATILE))
    render_table("Futures — top gainers", gainers(fut, TOP_GAINERS))
    render_table("Futures — top volatile (amplitude)", volatile(fut, TOP_VOLATILE))

    trend = trend_bucket(fut)
    crash = crash_bucket(fut)
    render_table(
        f"A) Trend (chg >= {TREND_MIN_GAIN_PCT:.0f}% AND amp >= {TREND_MIN_AMP_PCT:.0f}%) — futures",
        trend,
    )
    render_table(
        f"B) Crashed (chg <= {CRASH_MAX_LOSS_PCT:.0f}% AND amp >= {CRASH_MIN_AMP_PCT:.0f}%) — futures",
        crash,
    )
    render_hype(trending, extras, merge_markets(spot, fut))

    title, content = format_wecom_message(trending, spot, fut, trend, crash, extras)
    push_wecom(title, content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
