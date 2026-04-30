#!/usr/bin/env python3
"""
Binance USDT-M futures 4H structure monitor, with WeCom bot alerts.

Signal logic:
  A level: 4H breakout/start signal + volume expansion + OI growth
  B level: 4H trend setup signal, useful as a watchlist candidate
  Cooldown: avoid repeated alerts for the same symbol within the cooldown window

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
WECOM_WEBHOOK_KEY = os.getenv("WECOM_KEY", "PUT_YOUR_WECOM_KEY_HERE")

SCAN_INTERVAL_SEC = env_int("SCAN_INTERVAL_SEC", 300)
ALERT_COOLDOWN_SEC = env_int("ALERT_COOLDOWN_SEC", 14400)
REQ_TIMEOUT = env_int("REQ_TIMEOUT", 8)
CONCURRENCY = env_int("CONCURRENCY", 30)

MIN_24H_QUOTE_VOL = env_float("MIN_24H_QUOTE_VOL", 5_000_000)
TOP_N_BY_VOLUME = env_int("TOP_N_BY_VOLUME", 180)
MAX_24H_PRICE_PCT = env_float("MAX_24H_PRICE_PCT", 35.0)

KLINE_INTERVAL = os.getenv("KLINE_INTERVAL", "4h")
KLINE_LIMIT = env_int("KLINE_LIMIT", 120)
STRUCTURE_LOOKBACK = env_int("STRUCTURE_LOOKBACK", 20)
EMA_FAST_PERIOD = env_int("EMA_FAST_PERIOD", 20)
EMA_MID_PERIOD = env_int("EMA_MID_PERIOD", 50)
EMA_SLOW_PERIOD = env_int("EMA_SLOW_PERIOD", 100)

# ============ 4H signal config ============
A_MIN_4H_PRICE_PCT = env_float("A_MIN_4H_PRICE_PCT", 2.0)
A_MAX_4H_PRICE_PCT = env_float("A_MAX_4H_PRICE_PCT", 8.0)
A_MIN_4H_VOL_RATIO = env_float("A_MIN_4H_VOL_RATIO", 1.5)
A_MAX_DIST_ABOVE_EMA_FAST_PCT = env_float("A_MAX_DIST_ABOVE_EMA_FAST_PCT", 12.0)
A_MIN_OI_4H_PCT = env_float("A_MIN_OI_4H_PCT", 2.0)

B_MAX_4H_PRICE_PCT = env_float("B_MAX_4H_PRICE_PCT", 4.0)
B_MIN_4H_VOL_RATIO = env_float("B_MIN_4H_VOL_RATIO", 0.8)
B_MAX_DIST_ABOVE_EMA_FAST_PCT = env_float("B_MAX_DIST_ABOVE_EMA_FAST_PCT", 6.0)
B_EMA_MID_TOLERANCE_PCT = env_float("B_EMA_MID_TOLERANCE_PCT", 1.0)
B_MIN_DIST_TO_HIGH_PCT = env_float("B_MIN_DIST_TO_HIGH_PCT", 5.0)
B_MAX_DIST_TO_HIGH_PCT = env_float("B_MAX_DIST_TO_HIGH_PCT", 20.0)
B_MAX_24H_PRICE_PCT = env_float("B_MAX_24H_PRICE_PCT", 12.0)

C_MAX_DIST_TO_HIGH_PCT = env_float("C_MAX_DIST_TO_HIGH_PCT", 5.0)
C_MIN_DIST_ABOVE_EMA_FAST_PCT = env_float("C_MIN_DIST_ABOVE_EMA_FAST_PCT", 8.0)
C_MAX_DIST_ABOVE_EMA_FAST_PCT = env_float("C_MAX_DIST_ABOVE_EMA_FAST_PCT", 25.0)
C_MAX_24H_PRICE_PCT = env_float("C_MAX_24H_PRICE_PCT", 30.0)
WARN_DIST_ABOVE_EMA_FAST_PCT = env_float("WARN_DIST_ABOVE_EMA_FAST_PCT", 20.0)
MAX_DIST_ABOVE_EMA_FAST_PCT = env_float("MAX_DIST_ABOVE_EMA_FAST_PCT", 35.0)
MAX_ABS_FUNDING_PCT = env_float("MAX_ABS_FUNDING_PCT", 0.25)

FAPI = "https://fapi.binance.com"
# ==========================================

last_alert_at: Dict[str, float] = {}
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "binance-4h-monitor/1.0"})
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


def can_alert(key: str) -> bool:
    now = time.time()
    if now - last_alert_at.get(key, 0) > ALERT_COOLDOWN_SEC:
        last_alert_at[key] = now
        return True
    return False


def get_futures_candidates() -> List[dict]:
    r = SESSION.get(f"{FAPI}/fapi/v1/ticker/24hr", timeout=REQ_TIMEOUT)
    data = r.json()
    out = []
    for d in data:
        symbol = d.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        quote_volume = float(d.get("quoteVolume", 0))
        price_24h = float(d.get("priceChangePercent", 0))
        if quote_volume < MIN_24H_QUOTE_VOL:
            continue
        if price_24h > MAX_24H_PRICE_PCT:
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
    min_len = max(STRUCTURE_LOOKBACK, EMA_SLOW_PERIOD) + 1
    if not isinstance(data, list) or len(data) < min_len:
        return None
    return data


def fetch_oi_pct(symbol: str) -> float:
    try:
        r = SESSION.get(
            f"{FAPI}/futures/data/openInterestHist",
            params={"symbol": symbol, "period": KLINE_INTERVAL, "limit": 2},
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
    multiplier = 2 / (period + 1)
    value = average(values[:period])
    for price in values[period:]:
        value = (price - value) * multiplier + value
    return value


def ema_note(level: str) -> str:
    if level == "A":
        return "A级已启动, 不追高; 等回踩EMA20/50不破再找右侧多点"
    if level == "B":
        return "B级低位蓄势, EMA20/50不破时最适合观察做多"
    return "C级高位观察, 只看回踩EMA20/50后的修复, 不追高"


def analyze_4h(symbol: str, vol_24h: float, price_24h: float, funding: float) -> Optional[dict]:
    try:
        klines = fetch_klines(symbol)
        if not klines:
            return None

        closes = [float(k[4]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        quote_volumes = [float(k[7]) for k in klines]
        curr = klines[-1]
        prev = klines[-2]

        open_p = float(curr[1])
        high_p = float(curr[2])
        low_p = float(curr[3])
        close_p = float(curr[4])
        prev_close = float(prev[4])
        if open_p <= 0 or prev_close <= 0:
            return None

        ema_fast = ema(closes, EMA_FAST_PERIOD)
        ema_mid = ema(closes, EMA_MID_PERIOD)
        ema_slow = ema(closes, EMA_SLOW_PERIOD)
        high_n = max(highs[-STRUCTURE_LOOKBACK:])
        low_n = min(lows[-STRUCTURE_LOOKBACK:])
        avg_vol = average(quote_volumes[-STRUCTURE_LOOKBACK - 1:-1])
        if ema_fast <= 0 or ema_mid <= 0 or ema_slow <= 0 or high_n <= 0 or low_n <= 0 or avg_vol <= 0:
            return None

        candle_pct = (close_p / open_p - 1) * 100
        change_vs_prev = (close_p / prev_close - 1) * 100
        vol_ratio = quote_volumes[-1] / avg_vol
        pos_vs_ema_fast = (close_p / ema_fast - 1) * 100
        pos_vs_ema_mid = (close_p / ema_mid - 1) * 100
        pos_vs_ema_slow = (close_p / ema_slow - 1) * 100
        dist_to_high = (high_n / close_p - 1) * 100
        dist_to_low = (close_p / low_n - 1) * 100
        oi_pct = fetch_oi_pct(symbol)

        above_trend = close_p > ema_fast > ema_mid
        low_base_trend = close_p > ema_fast and ema_fast >= ema_mid * 0.98
        startup_zone = pos_vs_ema_fast <= A_MAX_DIST_ABOVE_EMA_FAST_PCT
        base_zone = pos_vs_ema_fast <= B_MAX_DIST_ABOVE_EMA_FAST_PCT
        ema_mid_hold = low_p >= ema_mid * (1 - B_EMA_MID_TOLERANCE_PCT / 100)
        pullback_room = B_MIN_DIST_TO_HIGH_PCT <= dist_to_high <= B_MAX_DIST_TO_HIGH_PCT
        near_high = 0 <= dist_to_high <= C_MAX_DIST_TO_HIGH_PCT
        high_breakout_zone = (
            C_MIN_DIST_ABOVE_EMA_FAST_PCT <= pos_vs_ema_fast <= C_MAX_DIST_ABOVE_EMA_FAST_PCT
        )
        not_overextended = pos_vs_ema_fast <= MAX_DIST_ABOVE_EMA_FAST_PCT
        overheat_warning = pos_vs_ema_fast >= WARN_DIST_ABOVE_EMA_FAST_PCT
        sane_24h = price_24h <= MAX_24H_PRICE_PCT
        sane_funding = abs(funding) <= MAX_ABS_FUNDING_PCT

        a_signal = (
            above_trend
            and not_overextended
            and startup_zone
            and sane_24h
            and sane_funding
            and A_MIN_4H_PRICE_PCT <= candle_pct <= A_MAX_4H_PRICE_PCT
            and vol_ratio >= A_MIN_4H_VOL_RATIO
            and oi_pct >= A_MIN_OI_4H_PCT
        )
        b_signal = (
            low_base_trend
            and not_overextended
            and base_zone
            and ema_mid_hold
            and pullback_room
            and price_24h <= B_MAX_24H_PRICE_PCT
            and sane_funding
            and 0 < candle_pct <= B_MAX_4H_PRICE_PCT
            and change_vs_prev > 0
            and vol_ratio >= B_MIN_4H_VOL_RATIO
            and oi_pct > 0
        )
        c_signal = (
            above_trend
            and not_overextended
            and near_high
            and high_breakout_zone
            and price_24h <= C_MAX_24H_PRICE_PCT
            and sane_funding
            and candle_pct > 0
            and change_vs_prev > 0
            and (vol_ratio >= 1.0 or oi_pct > 0)
        )
        if not a_signal and not b_signal and not c_signal:
            return None

        level = "A" if a_signal else "B" if b_signal else "C"

        return {
            "level": level,
            "symbol": symbol,
            "price": close_p,
            "high": high_p,
            "low": low_p,
            "candle_pct": candle_pct,
            "change_vs_prev": change_vs_prev,
            "vol_ratio": vol_ratio,
            "oi_pct": oi_pct,
            "funding": funding,
            "price_24h": price_24h,
            "vol_24h_m": vol_24h / 1e6,
            "ema_fast": ema_fast,
            "ema_mid": ema_mid,
            "ema_slow": ema_slow,
            "pos_vs_ema_fast": pos_vs_ema_fast,
            "pos_vs_ema_mid": pos_vs_ema_mid,
            "pos_vs_ema_slow": pos_vs_ema_slow,
            "overheat_warning": overheat_warning,
            "ema_note": ema_note(level),
            "high_n": high_n,
            "low_n": low_n,
            "dist_to_high": dist_to_high,
            "dist_to_low": dist_to_low,
        }
    except Exception as e:
        log(f"{symbol} analyze failed: {e}")
        return None


def format_signal(sig: dict) -> str:
    risk_note = ""
    if sig["overheat_warning"]:
        risk_note = f"\n> 风险: 价格高于EMA{EMA_FAST_PERIOD} {sig['pos_vs_ema_fast']:.2f}%, 偏高位加速"
    return (
        f"> 现价: `{sig['price']}`  24h: **{sig['price_24h']:+.2f}%**\n"
        f"> 4H: **{sig['candle_pct']:+.2f}%**  较前收: {sig['change_vs_prev']:+.2f}%\n"
        f"> 4H量比: **{sig['vol_ratio']:.2f}x**  OI({KLINE_INTERVAL}): **{sig['oi_pct']:+.2f}%**\n"
        f"> EMA{EMA_FAST_PERIOD}: {sig['ema_fast']:.8g}  EMA{EMA_MID_PERIOD}: {sig['ema_mid']:.8g}  "
        f"EMA{EMA_SLOW_PERIOD}: {sig['ema_slow']:.8g}\n"
        f"> 距EMA{EMA_FAST_PERIOD}: {sig['pos_vs_ema_fast']:+.2f}%  "
        f"距EMA{EMA_MID_PERIOD}: {sig['pos_vs_ema_mid']:+.2f}%\n"
        f"> 距{STRUCTURE_LOOKBACK}根4H高点: {sig['dist_to_high']:.2f}%  "
        f"距低点: {sig['dist_to_low']:.2f}%\n"
        f"> 资金费率: {sig['funding']:+.4f}%  24h成交额: {sig['vol_24h_m']:.1f}M U\n"
        f"> 备注: {sig['ema_note']}"
        f"{risk_note}"
    )


def scan_once() -> None:
    candidates = get_futures_candidates()
    funding_map = fetch_all_funding()
    log(f"扫描4H候选 合约:{len(candidates)}")

    hits: List[dict] = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = {
            ex.submit(
                analyze_4h,
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

    level_order = {"A": 0, "B": 1, "C": 2}
    hits.sort(key=lambda x: (level_order[x["level"]], -x["vol_ratio"], -x["oi_pct"]))

    a_count = 0
    b_count = 0
    c_count = 0
    for sig in hits:
        key = f"{sig['level']}4H:{sig['symbol']}"
        if not can_alert(key):
            continue
        if sig["level"] == "A":
            a_count += 1
            title = f"A级4H启动 {sig['symbol']}"
        elif sig["level"] == "B":
            b_count += 1
            title = f"B级4H蓄势 {sig['symbol']}"
        else:
            c_count += 1
            title = f"C级4H高位观察 {sig['symbol']}"
        push_wecom(title, format_signal(sig))

    log(f"命中4H A:{a_count} B:{b_count} C:{c_count} 总:{len(hits)}")


def main() -> None:
    log("启动4H监控")
    if WECOM_WEBHOOK_KEY.startswith("PUT_"):
        log("未配置 WECOM_KEY 环境变量,以 DRY-RUN 模式运行(只打印,不推送)")
    while True:
        try:
            t0 = time.time()
            scan_once()
            elapsed = time.time() - t0
            sleep_s = max(30, SCAN_INTERVAL_SEC - elapsed)
            log(f"耗时 {elapsed:.1f}s,休眠 {sleep_s:.0f}s")
            time.sleep(sleep_s)
        except KeyboardInterrupt:
            log("退出")
            break
        except Exception as e:
            log(f"循环异常: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
