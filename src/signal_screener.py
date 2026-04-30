#!/usr/bin/env python3
"""
4H technical screener for Binance USDT-M futures.

Pipeline:
  1. Pull 24h ticker, filter pairs by quote volume.
  2. For each pair, fetch 250 of 4H klines.
  3. Compute EMA20/50/100/200, RSI(14), 4H volume ratio (vs 20-bar mean).
  4. Classify into one of four buckets:
        - 做多          : full bull stack + RSI [40,65] + dist EMA20 in [0,12]% + vol >=1.2x
        - 做空          : full bear stack + RSI [35,60] + dist EMA20 in [-12,0]% + vol >=1.2x
        - 回踩多观察    : full bull stack but overheated (RSI>65 or dist>12%)
        - 反弹空观察    : full bear stack but oversold  (RSI<35 or dist<-12%)
  5. Score each candidate and push the top N per bucket to WeCom.

Usage:
  python3 src/signal_screener.py
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


def env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


load_env_file(Path(os.getenv("CONFIG_FILE", DEFAULT_CONFIG_FILE)))

FUTURES_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
FUTURES_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"

KLINE_INTERVAL = os.getenv("SCREEN_KLINE_INTERVAL", "4h")
KLINE_LIMIT = env_int("SCREEN_KLINE_LIMIT", 250)
MIN_QUOTE_VOL = env_float("SCREEN_MIN_QUOTE_VOL", 5_000_000)
CONCURRENCY = env_int("SCREEN_CONCURRENCY", 20)
REQ_TIMEOUT = env_int("REQ_TIMEOUT", 10)

LONG_RSI_LO = env_float("SCREEN_LONG_RSI_LO", 40)
LONG_RSI_HI = env_float("SCREEN_LONG_RSI_HI", 65)
SHORT_RSI_LO = env_float("SCREEN_SHORT_RSI_LO", 35)
SHORT_RSI_HI = env_float("SCREEN_SHORT_RSI_HI", 60)
MAX_DIST_PCT = env_float("SCREEN_MAX_DIST_PCT", 12.0)
HOT_RSI = env_float("SCREEN_HOT_RSI", 65)
COLD_RSI = env_float("SCREEN_COLD_RSI", 35)
MIN_VOL_RATIO = env_float("SCREEN_MIN_VOL_RATIO", 1.2)
TOP_N_PER_BUCKET = env_int("SCREEN_TOP_N", 5)

WECOM_WEBHOOK_KEY = os.getenv("WECOM_KEY", "PUT_YOUR_WECOM_KEY_HERE")
WECOM_MAX_CONTENT = 3800

STABLE_BASES = {
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP", "FDUSD", "PYUSD",
    "USDS", "USD1", "RLUSD", "BFUSD", "U", "XUSD", "AEUR", "EUR",
    "EURI", "PAXG", "XAUT",
}

BUCKET_LONG = "long"
BUCKET_SHORT = "short"
BUCKET_PULLBACK = "pullback_long"
BUCKET_BOUNCE = "bounce_short"


class Metric:
    __slots__ = (
        "base", "symbol", "price", "ema20", "ema50", "ema100", "ema200",
        "rsi", "vol_ratio", "dist_pct", "quote_vol", "chg24h",
    )

    def __init__(self, base, symbol, price, ema20, ema50, ema100, ema200,
                 rsi, vol_ratio, dist_pct, quote_vol, chg24h):
        self.base = base
        self.symbol = symbol
        self.price = price
        self.ema20 = ema20
        self.ema50 = ema50
        self.ema100 = ema100
        self.ema200 = ema200
        self.rsi = rsi
        self.vol_ratio = vol_ratio
        self.dist_pct = dist_pct
        self.quote_vol = quote_vol
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


def fetch_universe() -> Dict[str, Tuple[float, float]]:
    """Return {symbol: (quote_vol, change_24h_pct)} for tradable USDT pairs."""
    resp = requests.get(FUTURES_TICKER_URL, timeout=REQ_TIMEOUT)
    resp.raise_for_status()
    out: Dict[str, Tuple[float, float]] = {}
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
            out[sym] = (qv, float(d["priceChangePercent"]))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def fetch_klines(symbol: str) -> Optional[List[List[float]]]:
    try:
        resp = requests.get(
            FUTURES_KLINES_URL,
            params={"symbol": symbol, "interval": KLINE_INTERVAL, "limit": KLINE_LIMIT},
            timeout=REQ_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError):
        return None


def compute_metric(symbol: str, qv: float, chg24h: float, klines: List[List[float]]) -> Optional[Metric]:
    if not klines or len(klines) < 60:
        return None
    # Drop the in-progress 4H bar — its volume isn't comparable to closed bars
    # and partial close prices skew indicators. Indicators run on closed bars;
    # display "price" stays the live last-trade price.
    price_live = float(klines[-1][4])
    closed = klines[:-1]
    closes = [float(k[4]) for k in closed]
    vols = [float(k[5]) for k in closed]
    if len(closes) < 60:
        return None
    e20 = ema(closes, 20)[-1]
    e50 = ema(closes, 50)[-1]
    e100 = ema(closes, 100)[-1]
    e200 = ema(closes, 200)[-1] if len(closes) >= 200 else float("nan")
    r = rsi_wilder(closes, 14)
    if r is None or e20 <= 0:
        return None
    vol_avg = sum(vols[-21:-1]) / 20.0 if len(vols) >= 21 else (sum(vols) / max(len(vols), 1))
    vol_ratio = vols[-1] / vol_avg if vol_avg > 0 else 0.0
    dist_pct = (price_live - e20) / e20 * 100.0
    price = price_live
    return Metric(
        base=symbol[:-4],
        symbol=symbol,
        price=price,
        ema20=e20,
        ema50=e50,
        ema100=e100,
        ema200=e200,
        rsi=r,
        vol_ratio=vol_ratio,
        dist_pct=dist_pct,
        quote_vol=qv,
        chg24h=chg24h,
    )


def is_full_bull(m: Metric) -> bool:
    if m.ema200 != m.ema200:  # NaN
        return False
    return m.ema20 > m.ema50 > m.ema100 > m.ema200


def is_full_bear(m: Metric) -> bool:
    if m.ema200 != m.ema200:
        return False
    return m.ema20 < m.ema50 < m.ema100 < m.ema200


def long_score(m: Metric) -> float:
    # EMA alignment: full bull is mandatory for entering this bucket → 40 baseline
    s_ema = 40.0
    # Distance: 0% from EMA20 = 20, 12% = 0
    s_dist = max(0.0, 20.0 * (1.0 - m.dist_pct / MAX_DIST_PCT)) if 0 <= m.dist_pct <= MAX_DIST_PCT else 0.0
    # Vol ratio: cap at 2.5x → 20
    s_vol = min(m.vol_ratio, 2.5) / 2.5 * 20.0
    # RSI: triangular, peak at 55, zero at 40 and 70
    s_rsi = max(0.0, 20.0 * (1.0 - abs(m.rsi - 55.0) / 15.0))
    return s_ema + s_dist + s_vol + s_rsi


def short_score(m: Metric) -> float:
    s_ema = 40.0
    abs_dist = -m.dist_pct  # for shorts, dist_pct is negative
    s_dist = max(0.0, 20.0 * (1.0 - abs_dist / MAX_DIST_PCT)) if -MAX_DIST_PCT <= m.dist_pct <= 0 else 0.0
    s_vol = min(m.vol_ratio, 2.5) / 2.5 * 20.0
    # RSI: triangular, peak at 45, zero at 30 and 60
    s_rsi = max(0.0, 20.0 * (1.0 - abs(m.rsi - 45.0) / 15.0))
    return s_ema + s_dist + s_vol + s_rsi


def watch_score(m: Metric, bullish: bool) -> float:
    """Score for pullback/bounce buckets — structural strength only."""
    # 40 for full stack alignment + bonus for stretchedness (more stretched = stronger trend)
    s_ema = 40.0
    s_strength = min(abs(m.dist_pct), 50.0) / 50.0 * 30.0
    s_vol = min(m.vol_ratio, 3.0) / 3.0 * 20.0
    # RSI farther from 50 = stronger trend
    s_rsi = min(abs(m.rsi - 50.0), 30.0) / 30.0 * 10.0
    return s_ema + s_strength + s_vol + s_rsi


def classify(m: Metric) -> Tuple[Optional[str], float]:
    if is_full_bull(m):
        overheated = m.rsi > HOT_RSI or m.dist_pct > MAX_DIST_PCT
        if not overheated:
            if (LONG_RSI_LO <= m.rsi <= LONG_RSI_HI
                    and 0 <= m.dist_pct <= MAX_DIST_PCT
                    and m.vol_ratio >= MIN_VOL_RATIO):
                return BUCKET_LONG, long_score(m)
            return None, 0.0
        return BUCKET_PULLBACK, watch_score(m, bullish=True)
    if is_full_bear(m):
        oversold = m.rsi < COLD_RSI or m.dist_pct < -MAX_DIST_PCT
        if not oversold:
            if (SHORT_RSI_LO <= m.rsi <= SHORT_RSI_HI
                    and -MAX_DIST_PCT <= m.dist_pct <= 0
                    and m.vol_ratio >= MIN_VOL_RATIO):
                return BUCKET_SHORT, short_score(m)
            return None, 0.0
        return BUCKET_BOUNCE, watch_score(m, bullish=False)
    return None, 0.0


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


BUCKET_TITLES = {
    BUCKET_LONG: "做多 — 现在可建仓",
    BUCKET_SHORT: "做空 — 现在可建仓",
    BUCKET_PULLBACK: "回踩多观察 — 等回踩 EMA20/50",
    BUCKET_BOUNCE: "反弹空观察 — 等反弹到 EMA20/50",
}

BUCKET_COLORS = {
    BUCKET_LONG: "info",
    BUCKET_SHORT: "warning",
    BUCKET_PULLBACK: "comment",
    BUCKET_BOUNCE: "comment",
}


def render_bucket(title: str, items: List[Tuple[Metric, float]]) -> None:
    print(f"\n=== {title} ===")
    if not items:
        print("  (none)")
        return
    print(f"  {'BASE':<10}{'SCORE':>6}{'PRICE':>14}{'EMA20':>14}"
          f"{'RSI':>6}{'VOL_X':>7}{'DIST%':>8}{'24H%':>8}")
    for m, s in items:
        print(f"  {m.base:<10}{s:>6.1f}{fmt_price(m.price):>14}{fmt_price(m.ema20):>14}"
              f"{m.rsi:>6.1f}{m.vol_ratio:>7.2f}{m.dist_pct:>+8.2f}{m.chg24h:>+8.2f}")


def md_bucket(bucket: str, items: List[Tuple[Metric, float]]) -> str:
    color = BUCKET_COLORS[bucket]
    title = BUCKET_TITLES[bucket]
    if not items:
        return f'<font color="{color}">**{title}**</font>\n> 无\n'
    lines = []
    for m, s in items[:TOP_N_PER_BUCKET]:
        lines.append(
            f"> `{m.base:<8}` {s:>5.1f}  RSI {m.rsi:>4.1f}  "
            f"量比 {m.vol_ratio:>4.2f}  距EMA20 {m.dist_pct:>+5.1f}%  24h {m.chg24h:>+5.1f}%"
        )
    return f'<font color="{color}">**{title}**</font>\n' + "\n".join(lines) + "\n"


def push_wecom(title: str, content: str) -> None:
    if WECOM_WEBHOOK_KEY.startswith("PUT_"):
        print("\n[screener] WECOM_KEY not set — skipping push (DRY-RUN).", file=sys.stderr)
        return
    url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM_WEBHOOK_KEY}"
    payload = {"msgtype": "markdown", "markdown": {"content": f"### {title}\n{content}"}}
    try:
        r = requests.post(url, json=payload, timeout=REQ_TIMEOUT)
        body = r.json()
        if body.get("errcode") != 0:
            print(f"[screener] WeCom push failed: {body}", file=sys.stderr)
        else:
            print(f"[screener] pushed to WeCom ({len(content)} chars).", file=sys.stderr)
    except Exception as e:
        print(f"[screener] WeCom push exception: {e}", file=sys.stderr)


def format_message(buckets: Dict[str, List[Tuple[Metric, float]]]) -> Tuple[str, str]:
    title = f"4H 信号筛选 {datetime.now().strftime('%m-%d %H:%M')}"
    counts = " ".join(
        f"{BUCKET_TITLES[b].split(' — ')[0]}={len(buckets.get(b, []))}"
        for b in (BUCKET_LONG, BUCKET_SHORT, BUCKET_PULLBACK, BUCKET_BOUNCE)
    )
    parts = [f"> {counts}\n"]
    for b in (BUCKET_LONG, BUCKET_SHORT, BUCKET_PULLBACK, BUCKET_BOUNCE):
        parts.append(md_bucket(b, buckets.get(b, [])))
    content = "\n".join(parts).rstrip()
    while len(content.encode("utf-8")) > WECOM_MAX_CONTENT and "\n" in content:
        content = content.rsplit("\n", 1)[0]
    return title, content


def main() -> int:
    try:
        universe = fetch_universe()
    except requests.RequestException as e:
        print(f"[screener] universe fetch failed: {e}", file=sys.stderr)
        return 1
    print(f"# Signal screener: {len(universe)} pairs above {fmt_vol(MIN_QUOTE_VOL)}", file=sys.stderr)

    metrics: List[Metric] = []
    failures = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = {ex.submit(fetch_klines, sym): sym for sym in universe}
        for fut in as_completed(futures):
            sym = futures[fut]
            qv, chg = universe[sym]
            kl = fut.result()
            if kl is None:
                failures += 1
                continue
            m = compute_metric(sym, qv, chg, kl)
            if m is not None:
                metrics.append(m)
    print(f"# computed metrics for {len(metrics)} pairs (failed {failures})", file=sys.stderr)

    buckets: Dict[str, List[Tuple[Metric, float]]] = {
        BUCKET_LONG: [], BUCKET_SHORT: [],
        BUCKET_PULLBACK: [], BUCKET_BOUNCE: [],
    }
    for m in metrics:
        bucket, score = classify(m)
        if bucket:
            buckets[bucket].append((m, score))
    for b in buckets:
        buckets[b].sort(key=lambda t: t[1], reverse=True)

    for b in (BUCKET_LONG, BUCKET_SHORT, BUCKET_PULLBACK, BUCKET_BOUNCE):
        render_bucket(BUCKET_TITLES[b], buckets[b][:TOP_N_PER_BUCKET])

    title, content = format_message(buckets)
    push_wecom(title, content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
