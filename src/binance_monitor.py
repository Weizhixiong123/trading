#!/usr/bin/env python3
"""
Binance USDT-M futures startup radar (启动雷达), with WeCom bot alerts.

Goal:
  Catch coins the moment they START moving — small 24h gain but a sudden
  15m volume spike + open-interest growth — BEFORE they hit the 24h gainers
  board. Alert only; not an entry signal.

Two strategies run in one pass (shared base: 24h band, 1h momentum, bullish
15m EMA stack EMA20>EMA50>EMA100 & price>EMA100, volume pickup, sane funding):
  - 慢牛型 (slow):  base + ~1h open-interest growth >= OI_1H_MIN_PCT
                    (leveraged-money-confirmed grind)
  - 起涨型 (surge): base + ~4h sustained rise + EMA20 well above EMA100
                    (trend-confirmed start, no OI — catches spot-driven moves)
A coin can hit either or both; pushes are grouped by strategy.

Push model:
  Scan frequently; every PUSH_INTERVAL_SEC push every coin that reached
  >= MIN_CANDLE_STREAK consecutive 15m candles AT ANY POINT in that window
  (accumulated per strategy), then reset the window. Strong coins re-appear each
  window while they keep qualifying; coins that stop simply drop out.

Usage:
  cp configs/.env.example configs/.env
  edit configs/.env
  python3 src/binance_monitor.py
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

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

# ============ General config ============
# Startup radar uses its own bot if set, else falls back to the shared WECOM_KEY.
WECOM_WEBHOOK_KEY = os.getenv("MONITOR_WECOM_KEY") or os.getenv("WECOM_KEY", "PUT_YOUR_WECOM_KEY_HERE")

SCAN_INTERVAL_SEC = env_int("SCAN_INTERVAL_SEC", 180)
PUSH_INTERVAL_SEC = env_int("PUSH_INTERVAL_SEC", 3600)   # push a digest once per hour
MIN_CANDLE_STREAK = env_int("MIN_CANDLE_STREAK", 2)      # push coins hit N consecutive 15m candles
DIGEST_MAX_COINS = env_int("DIGEST_MAX_COINS", 20)       # cap coins per digest message
REQ_TIMEOUT = env_int("REQ_TIMEOUT", 8)
CONCURRENCY = env_int("CONCURRENCY", 30)

MIN_24H_QUOTE_VOL = env_float("MIN_24H_QUOTE_VOL", 5_000_000)
TOP_N_BY_VOLUME = env_int("TOP_N_BY_VOLUME", 180)

# ============ Startup signal config (slow-grind model) ============
# Derived from real gainers (FIDA/EDEN/BLESS...): a multi-hour grind up with
# steady OI inflow, NOT one explosive 15m candle.
KLINE_INTERVAL = os.getenv("KLINE_INTERVAL", "15m")
KLINE_LIMIT = env_int("KLINE_LIMIT", 120)              # need ~100 closed for EMA100

GAIN_24H_MIN = env_float("GAIN_24H_MIN", 3.0)          # already moving, not flat
GAIN_24H_MAX = env_float("GAIN_24H_MAX", 20.0)         # still room before it's "done"

OI_1H_MIN_PCT = env_float("OI_1H_MIN_PCT", 2.0)        # PRIMARY: leveraged money in (~1h)
MOM_LOOKBACK = env_int("MOM_LOOKBACK", 4)              # 4 x 15m = ~1h momentum window
MOM_1H_MIN_PCT = env_float("MOM_1H_MIN_PCT", 1.5)      # ~1h cumulative rise, gentle
MOM_1H_MAX_PCT = env_float("MOM_1H_MAX_PCT", 15.0)     # skip ones already going vertical
EMA_FAST = env_int("EMA_FAST", 20)                     # 15m EMA stack: fast
EMA_MID = env_int("EMA_MID", 50)                       # 15m EMA stack: mid
EMA_SLOW = env_int("EMA_SLOW", 100)                    # 15m EMA stack: slow (key support)
VOL_RECENT = env_int("VOL_RECENT", 4)                  # recent ~1h avg volume
VOL_BASELINE = env_int("VOL_BASELINE", 20)             # prior baseline avg volume
VOL_RATIO_MIN = env_float("VOL_RATIO_MIN", 1.3)        # recent vs baseline volume pickup
MAX_ABS_FUNDING_PCT = env_float("MAX_ABS_FUNDING_PCT", 0.25)

OI_PERIOD = os.getenv("OI_PERIOD", "15m")              # 5 points of 15m ≈ last 1h
OI_LIMIT = env_int("OI_LIMIT", 5)

# ---- 起涨型 (surge) extra filters: trend-only, no OI ----
MOM4_LOOKBACK = env_int("MOM4_LOOKBACK", 16)           # 16 x 15m = ~4h sustained window
MOM4_MIN_PCT = env_float("MOM4_MIN_PCT", 3.0)          # ~4h cumulative rise >= this
EMA_SEP_MIN_PCT = env_float("EMA_SEP_MIN_PCT", 3.0)    # EMA20 above EMA100 by >= this %

FAPI = "https://fapi.binance.com"
# ==========================================

# Per-strategy streak tracking + push-window accumulation.
STRATS = ("slow", "surge")
streak: Dict[str, Dict[str, int]] = {s: {} for s in STRATS}        # consecutive-candle hit count (live only)
last_open: Dict[str, Dict[str, int]] = {s: {} for s in STRATS}     # last hit candle openTime(ms)
window_hits: Dict[str, Dict[str, dict]] = {s: {} for s in STRATS}  # coins reaching MIN streak in the current push window

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "binance-startup-radar/1.0"})
_adapter = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def push_wecom(title: str, content: str) -> None:
    if WECOM_WEBHOOK_KEY.startswith("PUT_"):
        log(f"[DRY-RUN] {title}\n{content}\n")
        return
    url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM_WEBHOOK_KEY}"
    try:
        r = requests.post(
            url,
            json={"msgtype": "markdown", "markdown": {"content": f"### {title}\n{content}"}},
            timeout=REQ_TIMEOUT,
        )
        if r.json().get("errcode") != 0:
            log(f"Push failed: {r.text}")
    except Exception as e:
        log(f"Push exception: {e}")


def get_futures_candidates() -> List[dict]:
    r = SESSION.get(f"{FAPI}/fapi/v1/ticker/24hr", timeout=REQ_TIMEOUT)
    data = r.json()
    out = []
    for d in data:
        symbol = d.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        if float(d.get("quoteVolume", 0)) < MIN_24H_QUOTE_VOL:
            continue
        out.append(d)
    out.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
    return out[:TOP_N_BY_VOLUME]


def fetch_klines(symbol: str) -> Optional[List[list]]:
    r = SESSION.get(
        f"{FAPI}/fapi/v1/klines",
        params={"symbol": symbol, "interval": KLINE_INTERVAL, "limit": KLINE_LIMIT},
        timeout=REQ_TIMEOUT,
    )
    data = r.json()
    # Need VOL_RECENT + VOL_BASELINE closed candles, plus the forming one.
    if not isinstance(data, list) or len(data) < VOL_RECENT + VOL_BASELINE + 1:
        return None
    return data


def fetch_oi_pct(symbol: str) -> float:
    """Open-interest growth over the OI window (≈ last 1h)."""
    try:
        r = SESSION.get(
            f"{FAPI}/futures/data/openInterestHist",
            params={"symbol": symbol, "period": OI_PERIOD, "limit": OI_LIMIT},
            timeout=REQ_TIMEOUT,
        )
        data = r.json()
        if isinstance(data, list) and len(data) >= 2:
            prev = float(data[0]["sumOpenInterest"])
            curr = float(data[-1]["sumOpenInterest"])
            if prev > 0:
                return (curr / prev - 1) * 100
    except Exception:
        pass
    return 0.0


def fetch_all_funding() -> Dict[str, float]:
    try:
        r = SESSION.get(f"{FAPI}/fapi/v1/premiumIndex", timeout=REQ_TIMEOUT)
        return {d["symbol"]: float(d.get("lastFundingRate", 0)) * 100 for d in r.json()}
    except Exception:
        return {}


def average(values: List[float]) -> float:
    return sum(values) / len(values)


def ema(values: List[float], period: int) -> float:
    k = 2.0 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def analyze(symbol: str, vol_24h: float, price_24h: float, funding: float) -> Optional[dict]:
    """Evaluate the shared base, then both strategies. Returns the matched set in
    'strategies' (subset of STRATS), or None if neither matches."""
    try:
        # 24h band + funding are cheap pre-filters — skip k-line fetch if they fail.
        if not (GAIN_24H_MIN <= price_24h <= GAIN_24H_MAX):
            return None
        if abs(funding) > MAX_ABS_FUNDING_PCT:
            return None

        klines = fetch_klines(symbol)
        if not klines:
            return None

        # Closed candles only (klines[-1] is still forming).
        closed = klines[:-1]
        if len(closed) < EMA_SLOW:                       # need history for EMA100
            return None
        closes = [float(k[4]) for k in closed]
        vols = [float(k[7]) for k in closed]
        price = closes[-1]
        if price <= 0:
            return None

        # ---- shared base (both strategies require this) ----
        ref = closes[-1 - MOM_LOOKBACK]
        mom_1h = (price / ref - 1) * 100 if ref > 0 else 0.0
        if not (MOM_1H_MIN_PCT <= mom_1h <= MOM_1H_MAX_PCT):
            return None

        e20 = ema(closes, EMA_FAST)
        e50 = ema(closes, EMA_MID)
        e100 = ema(closes, EMA_SLOW)
        if not (e20 > e50 > e100 and price > e100):
            return None

        recent = average(vols[-VOL_RECENT:])
        base = average(vols[-VOL_RECENT - VOL_BASELINE:-VOL_RECENT])
        vol_ratio = recent / base if base > 0 else 0.0
        if vol_ratio < VOL_RATIO_MIN:
            return None

        # ---- per-strategy extras ----
        mom_4h = (price / closes[-1 - MOM4_LOOKBACK] - 1) * 100 if len(closes) > MOM4_LOOKBACK else 0.0
        ema_sep = (e20 / e100 - 1) * 100
        oi_pct = fetch_oi_pct(symbol)

        strategies = set()
        if oi_pct >= OI_1H_MIN_PCT:                                  # 慢牛型: OI confirmed
            strategies.add("slow")
        if mom_4h >= MOM4_MIN_PCT and ema_sep >= EMA_SEP_MIN_PCT:    # 起涨型: trend confirmed
            strategies.add("surge")
        if not strategies:
            return None

        return {
            "symbol": symbol,
            "price": price,
            "mom_1h": mom_1h,
            "mom_4h": mom_4h,
            "vol_ratio": vol_ratio,
            "oi_pct": oi_pct,
            "ema_sep": ema_sep,
            "funding": funding,
            "price_24h": price_24h,
            "vol_24h_m": vol_24h / 1e6,
            "ema20": e20,
            "ema50": e50,
            "ema100": e100,
            "strategies": strategies,
            "candle_open": int(closed[-1][0]),                       # closed-candle open time (ms)
            "candle_ms": int(closed[-1][0]) - int(closed[-2][0]),    # candle length (ms)
        }
    except Exception as e:
        log(f"{symbol} analyze failed: {e}")
        return None


def run_scan() -> List[dict]:
    candidates = get_futures_candidates()
    funding_map = fetch_all_funding()

    hits: List[dict] = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = {
            ex.submit(
                analyze,
                c["symbol"],
                float(c["quoteVolume"]),
                float(c["priceChangePercent"]),
                funding_map.get(c["symbol"], 0.0),
            ): c["symbol"]
            for c in candidates
        }
        for fut in as_completed(futures):
            result = fut.result()
            if result:
                hits.append(result)
    return hits


def update_streaks(hits: List[dict]) -> None:
    """Update per-strategy streaks, accumulate sustained coins into the push window,
    and drop coins that did not qualify this scan (so no stale streaks linger)."""
    hit_syms = {st: set() for st in STRATS}
    for sig in hits:
        sym = sig["symbol"]
        co = sig["candle_open"]
        cm = sig["candle_ms"]
        for st in sig["strategies"]:
            hit_syms[st].add(sym)
            prev = last_open[st].get(sym)
            if prev is None:
                streak[st][sym] = 1             # first hit (this strategy)
            elif co == prev:
                pass                            # same candle re-scanned — unchanged
            elif co == prev + cm:
                streak[st][sym] += 1            # consecutive next candle
            else:
                streak[st][sym] = 1             # gap — restart
            last_open[st][sym] = max(prev or 0, co)
            if streak[st][sym] >= MIN_CANDLE_STREAK:
                # Sustained — remember for this push window (carries its own streak).
                window_hits[st][sym] = {**sig, "_streak": streak[st][sym]}
    # Coins not hit this scan are no longer in startup — drop them so counts stay live.
    for st in STRATS:
        for sym in list(streak[st].keys()):
            if sym not in hit_syms[st]:
                streak[st].pop(sym, None)
                last_open[st].pop(sym, None)


def persistent_count(st: str) -> int:
    return sum(1 for c in streak[st].values() if c >= MIN_CANDLE_STREAK)


def _line_slow(s: dict) -> str:
    return (
        f"> `{s['symbol']:<9}` 连{s['_streak']}根 现价{s['price']} 24h{s['price_24h']:+.0f}%\n"
        f"> 1h{s['mom_1h']:+.0f}% 量比{s['vol_ratio']:.1f}x OI{s['oi_pct']:+.0f}% 费率{s['funding']:+.3f}%"
    )


def _line_surge(s: dict) -> str:
    return (
        f"> `{s['symbol']:<9}` 连{s['_streak']}根 现价{s['price']} 24h{s['price_24h']:+.0f}%\n"
        f"> 1h{s['mom_1h']:+.0f}% 4h{s['mom_4h']:+.0f}% 量比{s['vol_ratio']:.1f}x\n"
        f"> EMA20 {s['ema20']:.5g}/EMA50 {s['ema50']:.5g} 回踩此区间找进场"
    )


def _line_both(s: dict) -> str:
    return (
        f"> `{s['symbol']:<9}` 连{s['_streak']}根 现价{s['price']} 24h{s['price_24h']:+.0f}%\n"
        f"> 1h{s['mom_1h']:+.0f}% 4h{s['mom_4h']:+.0f}% 量比{s['vol_ratio']:.1f}x OI{s['oi_pct']:+.0f}%\n"
        f"> EMA20 {s['ema20']:.5g}/EMA50 {s['ema50']:.5g} 回踩此区间找进场"
    )


def push_digest() -> tuple:
    """Split this window's hits into 双确认(both)/纯起涨/纯慢牛, push, then reset."""
    slow_syms = set(window_hits["slow"]); surge_syms = set(window_hits["surge"])
    both_syms = slow_syms & surge_syms
    onlysurge = surge_syms - slow_syms
    onlyslow = slow_syms - surge_syms
    # surge window carries 4h/ema fields, so prefer it for 双确认 display
    both = sorted((window_hits["surge"][x] for x in both_syms), key=lambda s: -s["vol_ratio"])
    surge = sorted((window_hits["surge"][x] for x in onlysurge), key=lambda s: -s["vol_ratio"])
    slow = sorted((window_hits["slow"][x] for x in onlyslow), key=lambda s: -s["vol_ratio"])
    window_hits["slow"].clear()
    window_hits["surge"].clear()
    if not (both or surge or slow):
        return (0, 0)
    parts = []
    if both:
        body = "\n".join(_line_both(s) for s in both[:DIGEST_MAX_COINS])
        parts.append(f'<font color="warning">**★双确认(慢牛+起涨) {len(both)}个 — 最强**</font>\n{body}')
    if surge:
        body = "\n".join(_line_surge(s) for s in surge[:DIGEST_MAX_COINS])
        parts.append(f'<font color="info">**【起涨型·趋势】{len(surge)}个**</font>\n{body}')
    if slow:
        body = "\n".join(_line_slow(s) for s in slow[:DIGEST_MAX_COINS])
        parts.append(f'<font color="comment">**【慢牛型·OI(偏弱,谨慎)】{len(slow)}个**</font>\n{body}')
    content = "\n".join(parts) + "\n> 仅预警, 非开单信号; 回踩 EMA20/50 确认后再决定"
    push_wecom(f"⚡雷达 {datetime.now():%m-%d %H:%M} 双确认{len(both)} 起涨{len(surge)} 慢牛{len(slow)}", content)
    log(f"推送币名 双确认[{','.join(s['symbol'] for s in both)}] 起涨[{','.join(s['symbol'] for s in surge)}] 慢牛[{','.join(s['symbol'] for s in slow)}]")
    return (len(both) + len(surge) + len(slow), len(both))


def main() -> None:
    log("雷达启动(慢牛型 + 起涨型)")
    if WECOM_WEBHOOK_KEY.startswith("PUT_"):
        log("未配置 WECOM_KEY 环境变量,以 DRY-RUN 模式运行(只打印,不推送)")
    last_push = time.time()
    while True:
        try:
            t0 = time.time()
            hits = run_scan()
            update_streaks(hits)
            log(f"本轮命中:{len(hits)} 持续(≥{MIN_CANDLE_STREAK}根) 慢牛:{persistent_count('slow')} 起涨:{persistent_count('surge')}")

            now = time.time()
            if now - last_push >= PUSH_INTERVAL_SEC:
                total, both = push_digest()
                last_push = now
                log(f"推送:{total} 双确认:{both}")

            elapsed = time.time() - t0
            sleep_s = max(30, SCAN_INTERVAL_SEC - elapsed)
            time.sleep(sleep_s)
        except KeyboardInterrupt:
            log("退出")
            break
        except Exception as e:
            log(f"循环异常: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
