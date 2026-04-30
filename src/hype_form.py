#!/usr/bin/env python3
"""
4H form analysis for hot/trending coins only.

Inputs:
  - CoinGecko /search/trending (top 15 by user search)
  - Optional EXTRA_HYPE_KEYWORDS env var (comma-separated additions)

For each hot coin that trades on Binance (futures preferred, else spot),
pulls 250 of 4H klines and reports the structural form and key indicators:
  - EMA20 / 50 / 100 / 200 stack arrangement
  - Distance from EMA20 (signed %)
  - Position within last 20-bar high-low range (0-100%)
  - RSI(14) on closed bars only
  - Volume trend: avg(last 3 closed vols) / avg(prior 20)
  - Color of last 3 closed 4H candles

Output groups coins into one of four forms and pushes a markdown digest
to the same WeCom webhook used by binance_monitor.

Usage:
  python3 src/hype_form.py
  EXTRA_HYPE_KEYWORDS="LUNC,MEME" python3 src/hype_form.py
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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


load_env_file(Path(os.getenv("CONFIG_FILE", DEFAULT_CONFIG_FILE)))

SPOT_TICKER_URL = "https://api.binance.com/api/v3/ticker/24hr"
FUTURES_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
SPOT_KLINES_URL = "https://api.binance.com/api/v3/klines"
FUTURES_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
COINGECKO_TRENDING_URL = "https://api.coingecko.com/api/v3/search/trending"

KLINE_INTERVAL = os.getenv("FORM_KLINE_INTERVAL", "4h")
KLINE_LIMIT = env_int("FORM_KLINE_LIMIT", 250)
CONCURRENCY = env_int("FORM_CONCURRENCY", 15)
REQ_TIMEOUT = env_int("REQ_TIMEOUT", 10)

WECOM_WEBHOOK_KEY = os.getenv("WECOM_KEY", "PUT_YOUR_WECOM_KEY_HERE")
WECOM_MAX_CONTENT = 3800

FORM_BULL = "完整多头"
FORM_BEAR = "完整空头"
FORM_TURN = "转折中"
FORM_RANGE = "震荡"

FORM_COLOR = {
    FORM_BULL: "info",
    FORM_BEAR: "warning",
    FORM_TURN: "comment",
    FORM_RANGE: "comment",
}
FORM_ORDER = (FORM_BULL, FORM_BEAR, FORM_TURN, FORM_RANGE)


class FormReport:
    __slots__ = (
        "rank", "base", "symbol", "market", "form", "price", "ema20", "rsi",
        "dist_pct", "range_pos", "vol_trend", "vol_ratio", "candles", "chg24h",
    )

    def __init__(self, rank, base, symbol, market, form, price, ema20, rsi,
                 dist_pct, range_pos, vol_trend, vol_ratio, candles, chg24h):
        self.rank = rank
        self.base = base
        self.symbol = symbol
        self.market = market
        self.form = form
        self.price = price
        self.ema20 = ema20
        self.rsi = rsi
        self.dist_pct = dist_pct
        self.range_pos = range_pos
        self.vol_trend = vol_trend
        self.vol_ratio = vol_ratio
        self.candles = candles
        self.chg24h = chg24h


def ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi_wilder(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) <= period:
        return None
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def classify_form(e20: float, e50: float, e100: float, e200: float) -> str:
    if e20 > e50 > e100 > e200:
        return FORM_BULL
    if e20 < e50 < e100 < e200:
        return FORM_BEAR
    short_bull = e20 > e50
    long_bull = e100 > e200
    if short_bull != long_bull:
        return FORM_TURN
    return FORM_RANGE


def candle_signature(opens: List[float], closes: List[float]) -> str:
    """Last 3 closed 4H candles, oldest first. R=red G=green D=doji."""
    out = []
    for o, c in zip(opens[-3:], closes[-3:]):
        if c > o * 1.0005:
            out.append("G")
        elif c < o * 0.9995:
            out.append("R")
        else:
            out.append("D")
    return "".join(out)


def fetch_coingecko_trending() -> List[Tuple[int, str]]:
    resp = requests.get(COINGECKO_TRENDING_URL, timeout=REQ_TIMEOUT,
                        headers={"accept": "application/json"})
    resp.raise_for_status()
    out: List[Tuple[int, str]] = []
    seen: set = set()
    for i, c in enumerate(resp.json().get("coins", []), 1):
        item = c.get("item") or {}
        sym = (item.get("symbol") or "").upper().strip()
        if sym and sym not in seen:
            seen.add(sym)
            out.append((i, sym))
    return out


def load_extras() -> List[str]:
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


def fetch_tradable_pairs() -> Tuple[Dict[str, float], Dict[str, float]]:
    """Return (futures_map, spot_map) of base->24h_change_pct."""
    fut_map: Dict[str, float] = {}
    spot_map: Dict[str, float] = {}
    try:
        fr = requests.get(FUTURES_TICKER_URL, timeout=REQ_TIMEOUT).json()
        for d in fr:
            sym = d.get("symbol", "")
            if sym.endswith("USDT"):
                fut_map[sym[:-4]] = float(d.get("priceChangePercent") or 0)
    except Exception as e:
        print(f"[form] futures ticker failed: {e}", file=sys.stderr)
    try:
        sr = requests.get(SPOT_TICKER_URL, timeout=REQ_TIMEOUT).json()
        for d in sr:
            sym = d.get("symbol", "")
            if sym.endswith("USDT"):
                spot_map[sym[:-4]] = float(d.get("priceChangePercent") or 0)
    except Exception as e:
        print(f"[form] spot ticker failed: {e}", file=sys.stderr)
    return fut_map, spot_map


def fetch_klines(market: str, symbol: str) -> Optional[List[list]]:
    url = FUTURES_KLINES_URL if market == "futures" else SPOT_KLINES_URL
    try:
        resp = requests.get(url, params={
            "symbol": symbol, "interval": KLINE_INTERVAL, "limit": KLINE_LIMIT,
        }, timeout=REQ_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError):
        return None


def analyze(rank: int, base: str, market: str, symbol: str,
            chg24h: float, klines: List[list]) -> Optional[FormReport]:
    if not klines or len(klines) < 60:
        return None
    price_live = float(klines[-1][4])
    closed = klines[:-1]
    opens = [float(k[1]) for k in closed]
    highs = [float(k[2]) for k in closed]
    lows = [float(k[3]) for k in closed]
    closes = [float(k[4]) for k in closed]
    vols = [float(k[5]) for k in closed]
    if len(closes) < 60:
        return None

    e20 = ema(closes, 20)[-1]
    e50 = ema(closes, 50)[-1]
    e100 = ema(closes, 100)[-1]
    e200 = ema(closes, 200)[-1] if len(closes) >= 200 else e100
    r = rsi_wilder(closes, 14)
    if r is None or e20 <= 0:
        return None

    form = classify_form(e20, e50, e100, e200)
    dist_pct = (price_live - e20) / e20 * 100.0

    h20 = max(highs[-20:])
    l20 = min(lows[-20:])
    range_pos = (price_live - l20) / (h20 - l20) * 100.0 if h20 > l20 else 50.0

    recent_vol = sum(vols[-3:]) / 3.0
    prior_vol = sum(vols[-23:-3]) / 20.0 if len(vols) >= 23 else (sum(vols[:-3]) / max(len(vols) - 3, 1))
    vol_ratio = recent_vol / prior_vol if prior_vol > 0 else 0.0
    if vol_ratio >= 1.3:
        vol_trend = "扩量"
    elif vol_ratio >= 0.7:
        vol_trend = "平量"
    else:
        vol_trend = "缩量"

    return FormReport(
        rank=rank, base=base, symbol=symbol, market=market, form=form,
        price=price_live, ema20=e20, rsi=r, dist_pct=dist_pct,
        range_pos=range_pos, vol_trend=vol_trend, vol_ratio=vol_ratio,
        candles=candle_signature(opens, closes), chg24h=chg24h,
    )


def fmt_price(p: float) -> str:
    if p >= 100:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.001:
        return f"{p:.6f}"
    return f"{p:.3e}"


def render_groups(reports: List[FormReport], unmatched: List[Tuple[int, str]]) -> None:
    by_form: Dict[str, List[FormReport]] = {f: [] for f in FORM_ORDER}
    for r in reports:
        by_form[r.form].append(r)
    print(f"\n# 热点币 4H 形态扫描  {datetime.now():%m-%d %H:%M}")
    print(f"# 已分析 {len(reports)} 个，未上币安 {len(unmatched)} 个")
    for form in FORM_ORDER:
        items = by_form[form]
        print(f"\n=== 【{form}】 共 {len(items)} 个 ===")
        if not items:
            print("  (无)")
            continue
        print(f"  {'#':<4}{'BASE':<10}{'MKT':<6}{'PRICE':>14}"
              f"{'DIST20':>9}{'POS%':>7}{'RSI':>6}{'VOL':>6}{'3根':>6}{'24H%':>9}")
        items.sort(key=lambda r: r.rank)
        for r in items:
            print(f"  #{r.rank:<3}{r.base:<10}{r.market:<6}{fmt_price(r.price):>14}"
                  f"{r.dist_pct:>+8.2f}%{r.range_pos:>6.0f}%{r.rsi:>6.1f}"
                  f"{r.vol_trend:>6}{r.candles:>6}{r.chg24h:>+8.2f}%")
    if unmatched:
        print(f"\n未上币安: {', '.join(b for _, b in unmatched)}")


def md_section(form: str, items: List[FormReport]) -> str:
    color = FORM_COLOR[form]
    if not items:
        return f'<font color="{color}">**【{form}】 0**</font>\n'
    lines = []
    for r in items:
        lines.append(
            f"> #{r.rank} `{r.base:<8}` {r.market:<3}  "
            f"距EMA20 {r.dist_pct:>+5.1f}%  位 {r.range_pos:>3.0f}%  "
            f"RSI {r.rsi:>4.1f}  {r.vol_trend}  {r.candles}  24h {r.chg24h:>+5.1f}%"
        )
    return f'<font color="{color}">**【{form}】 {len(items)}**</font>\n' + "\n".join(lines) + "\n"


def format_message(reports: List[FormReport], unmatched: List[Tuple[int, str]]) -> Tuple[str, str]:
    title = f"热点币 4H 形态  {datetime.now():%m-%d %H:%M}"
    by_form: Dict[str, List[FormReport]] = {f: [] for f in FORM_ORDER}
    for r in reports:
        by_form[r.form].append(r)
    for f in FORM_ORDER:
        by_form[f].sort(key=lambda r: r.rank)

    summary = " ".join(f"{f} {len(by_form[f])}" for f in FORM_ORDER)
    parts = [f"> 共 {len(reports)} 个: {summary}\n"]
    for f in FORM_ORDER:
        parts.append(md_section(f, by_form[f]))
    if unmatched:
        names = ", ".join(b for _, b in unmatched)
        parts.append(f'<font color="comment">**未上币安**</font>\n> {names}\n')

    content = "\n".join(parts).rstrip()
    while len(content.encode("utf-8")) > WECOM_MAX_CONTENT and "\n" in content:
        content = content.rsplit("\n", 1)[0]
    return title, content


def push_wecom(title: str, content: str) -> None:
    if WECOM_WEBHOOK_KEY.startswith("PUT_"):
        print("\n[form] WECOM_KEY not set — skipping push (DRY-RUN).", file=sys.stderr)
        return
    url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM_WEBHOOK_KEY}"
    payload = {"msgtype": "markdown", "markdown": {"content": f"### {title}\n{content}"}}
    try:
        r = requests.post(url, json=payload, timeout=REQ_TIMEOUT)
        body = r.json()
        if body.get("errcode") != 0:
            print(f"[form] WeCom push failed: {body}", file=sys.stderr)
        else:
            print(f"[form] pushed to WeCom ({len(content)} chars).", file=sys.stderr)
    except Exception as e:
        print(f"[form] WeCom push exception: {e}", file=sys.stderr)


def main() -> int:
    try:
        trending = fetch_coingecko_trending()
    except requests.RequestException as e:
        print(f"[form] CoinGecko fetch failed: {e}", file=sys.stderr)
        return 1
    extras = load_extras()
    targets: List[Tuple[int, str]] = list(trending)
    next_rank = max((r for r, _ in trending), default=0) + 1
    for k in extras:
        if not any(b == k for _, b in targets):
            targets.append((next_rank, k))
            next_rank += 1

    fut_map, spot_map = fetch_tradable_pairs()

    jobs: List[Tuple[int, str, str, str, float]] = []
    unmatched: List[Tuple[int, str]] = []
    for rank, base in targets:
        if base in fut_map:
            jobs.append((rank, base, "futures", f"{base}USDT", fut_map[base]))
        elif base in spot_map:
            jobs.append((rank, base, "spot", f"{base}USDT", spot_map[base]))
        else:
            unmatched.append((rank, base))

    reports: List[FormReport] = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = {
            ex.submit(fetch_klines, market, symbol): (rank, base, market, symbol, chg)
            for rank, base, market, symbol, chg in jobs
        }
        for fut in as_completed(futures):
            rank, base, market, symbol, chg = futures[fut]
            kl = fut.result()
            if not kl:
                unmatched.append((rank, base))
                continue
            r = analyze(rank, base, market, symbol, chg, kl)
            if r is not None:
                reports.append(r)

    render_groups(reports, unmatched)
    title, content = format_message(reports, unmatched)
    push_wecom(title, content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
