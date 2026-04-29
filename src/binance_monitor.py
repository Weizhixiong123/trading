#!/usr/bin/env python3
"""
币安现货 + U本位永续合约 异动监控,推送到企业微信群机器人

信号逻辑:
  现货:5m涨幅 ≥ 阈值 + 5m成交量 > 过去1h均值 N 倍
  合约:5m涨幅 ≥ 阈值 + OI 5m 增量 ≥ 阈值
  共振(A级):同币种现货+合约同时命中 → 最强信号
  单边(B级):仅一边命中
  冷却:同币种 30 分钟内不重复推送

使用:
  cp configs/.env.example configs/.env
  编辑 configs/.env
  python3 src/binance_monitor.py
"""
import os
import time
import requests
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, Dict, List
from concurrent.futures import ThreadPoolExecutor, as_completed


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
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


load_env_file(Path(os.getenv("CONFIG_FILE", DEFAULT_CONFIG_FILE)))

# ============ 配置 ============
WECOM_WEBHOOK_KEY   = os.getenv("WECOM_KEY", "PUT_YOUR_WECOM_KEY_HERE")

SCAN_INTERVAL_SEC   = env_int("SCAN_INTERVAL_SEC", 60)
ALERT_COOLDOWN_SEC  = env_int("ALERT_COOLDOWN_SEC", 1800)

MIN_24H_QUOTE_VOL   = env_float("MIN_24H_QUOTE_VOL", 1_000_000)  # 24h 成交额下限 (USDT)
TOP_N_BY_VOLUME     = env_int("TOP_N_BY_VOLUME", 150)            # 每个市场只扫 24h 成交额前 N
CONCURRENCY         = env_int("CONCURRENCY", 40)                 # 并发请求数

# 现货信号
SPOT_PRICE_5M_PCT   = env_float("SPOT_PRICE_5M_PCT", 2.0)
SPOT_VOL_5M_RATIO   = env_float("SPOT_VOL_5M_RATIO", 3.0)

# 合约信号
FUT_PRICE_5M_PCT    = env_float("FUT_PRICE_5M_PCT", 2.0)
FUT_OI_5M_PCT       = env_float("FUT_OI_5M_PCT", 3.0)

REQ_TIMEOUT         = env_int("REQ_TIMEOUT", 8)
SPOT_API            = "https://api.binance.com"
FAPI                = "https://fapi.binance.com"
# =============================

last_alert_at = {}  # type: Dict[str, float]
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "binance-monitor/1.0"})
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
            log(f"推送失败: {r.text}")
    except Exception as e:
        log(f"推送异常: {e}")


def can_alert(key: str) -> bool:
    now = time.time()
    if now - last_alert_at.get(key, 0) > ALERT_COOLDOWN_SEC:
        last_alert_at[key] = now
        return True
    return False


def is_leveraged_token(sym: str) -> bool:
    return any(x in sym for x in ["UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT"])


def get_candidates(url: str, leveraged_filter: bool) -> List[dict]:
    r = SESSION.get(url, timeout=REQ_TIMEOUT)
    data = r.json()
    out = []
    for d in data:
        if not d["symbol"].endswith("USDT"):
            continue
        if leveraged_filter and is_leveraged_token(d["symbol"]):
            continue
        if float(d["quoteVolume"]) < MIN_24H_QUOTE_VOL:
            continue
        out.append(d)
    out.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
    return out[:TOP_N_BY_VOLUME]


def fetch_5m_klines(base_url: str, symbol: str, kline_path: str) -> Optional[Tuple[float, float, float]]:
    """返回 (5m涨幅%, 5m量比, 现价);量比 = 当前5m成交额 / 过去1h均值"""
    r = SESSION.get(
        f"{base_url}{kline_path}",
        params={"symbol": symbol, "interval": "5m", "limit": 13},
        timeout=REQ_TIMEOUT,
    )
    kl = r.json()
    if not isinstance(kl, list) or len(kl) < 13:
        return None
    curr = kl[-1]
    open_p, close_p, qvol = float(curr[1]), float(curr[4]), float(curr[7])
    prev_qvols = [float(k[7]) for k in kl[-13:-1]]
    avg = sum(prev_qvols) / len(prev_qvols)
    if open_p == 0 or avg == 0:
        return None
    price_pct = (close_p / open_p - 1) * 100
    vol_ratio = qvol / avg
    return price_pct, vol_ratio, close_p


def check_spot(symbol: str, vol_24h: float, price_24h: float) -> Optional[dict]:
    try:
        kl = fetch_5m_klines(SPOT_API, symbol, "/api/v3/klines")
        if not kl:
            return None
        price_pct, vol_ratio, price = kl
        if price_pct >= SPOT_PRICE_5M_PCT and vol_ratio >= SPOT_VOL_5M_RATIO:
            return {
                "symbol": symbol, "price_5m": price_pct, "vol_ratio": vol_ratio,
                "price": price, "vol_24h_m": vol_24h / 1e6, "price_24h": price_24h,
            }
    except Exception:
        pass
    return None


def check_fut_price(symbol, vol_24h, price_24h):
    """第一阶段:只判断价格,通过后再查 OI"""
    try:
        kl = fetch_5m_klines(FAPI, symbol, "/fapi/v1/klines")
        if not kl:
            return None
        price_pct, vol_ratio, price = kl
        if price_pct >= FUT_PRICE_5M_PCT:
            return {
                "symbol": symbol, "price_5m": price_pct, "vol_ratio": vol_ratio,
                "price": price, "vol_24h_m": vol_24h / 1e6, "price_24h": price_24h,
            }
    except Exception:
        pass
    return None


def fetch_oi_pct(symbol):
    """返回 OI 5m 增长 %"""
    try:
        r = SESSION.get(
            f"{FAPI}/futures/data/openInterestHist",
            params={"symbol": symbol, "period": "5m", "limit": 2},
            timeout=REQ_TIMEOUT,
        )
        oi = r.json()
        if isinstance(oi, list) and len(oi) >= 2:
            prev = float(oi[0]["sumOpenInterest"])
            curr = float(oi[-1]["sumOpenInterest"])
            if prev > 0:
                return (curr / prev - 1) * 100
    except Exception:
        pass
    return 0.0


def fetch_all_funding():
    """一次拉所有合约资金费率,返回 {symbol: funding_pct}"""
    try:
        r = SESSION.get(f"{FAPI}/fapi/v1/premiumIndex", timeout=REQ_TIMEOUT)
        return {d["symbol"]: float(d.get("lastFundingRate", 0)) * 100 for d in r.json()}
    except Exception:
        return {}


def scan_once() -> None:
    spot_cands = get_candidates(f"{SPOT_API}/api/v3/ticker/24hr", leveraged_filter=True)
    fut_cands = get_candidates(f"{FAPI}/fapi/v1/ticker/24hr", leveraged_filter=False)
    log(f"扫描候选 现货:{len(spot_cands)} 合约:{len(fut_cands)}")

    spot_hits = {}   # type: Dict[str, dict]
    fut_price_hits = {}  # type: Dict[str, dict]

    # 第一阶段:并发拉所有 K 线,价格筛选
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        sf = {ex.submit(check_spot, c["symbol"], float(c["quoteVolume"]),
                        float(c["priceChangePercent"])): c["symbol"] for c in spot_cands}
        ff = {ex.submit(check_fut_price, c["symbol"], float(c["quoteVolume"]),
                       float(c["priceChangePercent"])): c["symbol"] for c in fut_cands}
        for fut in as_completed(sf):
            r = fut.result()
            if r:
                spot_hits[r["symbol"]] = r
        for fut in as_completed(ff):
            r = fut.result()
            if r:
                fut_price_hits[r["symbol"]] = r

    # 第二阶段:只对通过价格筛选的合约币查 OI(数量少,几个到几十个)
    fut_hits = {}    # type: Dict[str, dict]
    funding_map = fetch_all_funding()
    if fut_price_hits:
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            of = {ex.submit(fetch_oi_pct, sym): sym for sym in fut_price_hits}
            for fut in as_completed(of):
                sym = of[fut]
                oi_pct = fut.result()
                if oi_pct >= FUT_OI_5M_PCT:
                    sig = fut_price_hits[sym]
                    sig["oi_5m"] = oi_pct
                    sig["funding"] = funding_map.get(sym, 0.0)
                    fut_hits[sym] = sig

    spot_bases = {s[:-4]: s for s in spot_hits}
    fut_bases = {s[:-4]: s for s in fut_hits}
    resonance = set(spot_bases) & set(fut_bases)

    # A级:共振
    for base in sorted(resonance):
        sym = f"{base}USDT"
        if not can_alert(f"R:{sym}"):
            continue
        s, f = spot_hits[sym], fut_hits[sym]
        push_wecom(
            f"🔥 A级共振 {sym}",
            f"**现货+合约同时异动**\n"
            f"> 现货 5m: **{s['price_5m']:+.2f}%**  量比 {s['vol_ratio']:.1f}x\n"
            f"> 合约 5m: **{f['price_5m']:+.2f}%**  OI **{f['oi_5m']:+.2f}%**  量比 {f['vol_ratio']:.1f}x\n"
            f"> 资金费率: {f['funding']:+.4f}%\n"
            f"> 现价: {f['price']}  24h: {f['price_24h']:+.2f}%\n"
            f"> 24h 合约成交额: {f['vol_24h_m']:.1f}M U",
        )

    # B级:仅现货
    for sym, sig in spot_hits.items():
        if sym[:-4] in resonance or not can_alert(f"S:{sym}"):
            continue
        push_wecom(
            f"📈 现货异动 {sym}",
            f"> 5m: **{sig['price_5m']:+.2f}%**  量比 {sig['vol_ratio']:.1f}x\n"
            f"> 现价: {sig['price']}  24h: {sig['price_24h']:+.2f}%\n"
            f"> 24h 成交额: {sig['vol_24h_m']:.1f}M U",
        )

    # B级:仅合约
    for sym, sig in fut_hits.items():
        if sym[:-4] in resonance or not can_alert(f"F:{sym}"):
            continue
        push_wecom(
            f"📊 合约异动 {sym}",
            f"> 5m: **{sig['price_5m']:+.2f}%**  OI **{sig['oi_5m']:+.2f}%**\n"
            f"> 量比 {sig['vol_ratio']:.1f}x  资金费率 {sig['funding']:+.4f}%\n"
            f"> 现价: {sig['price']}  24h: {sig['price_24h']:+.2f}%\n"
            f"> 24h 成交额: {sig['vol_24h_m']:.1f}M U",
        )

    log(f"命中 现货:{len(spot_hits)} 合约:{len(fut_hits)} 共振:{len(resonance)}")


def main() -> None:
    log("启动监控")
    if WECOM_WEBHOOK_KEY.startswith("PUT_"):
        log("⚠️ 未配置 WECOM_KEY 环境变量,以 DRY-RUN 模式运行(只打印,不推送)")
    while True:
        try:
            t0 = time.time()
            scan_once()
            elapsed = time.time() - t0
            sleep_s = max(5, SCAN_INTERVAL_SEC - elapsed)
            log(f"耗时 {elapsed:.1f}s,休眠 {sleep_s:.0f}s")
            time.sleep(sleep_s)
        except KeyboardInterrupt:
            log("退出")
            break
        except Exception as e:
            log(f"循环异常: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
