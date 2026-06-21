# Binance Monitor

币安 U 本位永续合约启动雷达：全市场扫描，在币刚启动（24h 仅小涨但 15m 突然放量 + OI 进场）时推送企业微信预警，赶在它冲上涨幅榜之前发现。

## 项目结构

```text
trading/
├─ configs/
│  └─ .env.example
├─ src/
│  ├─ binance_monitor.py
│  ├─ hype_form.py
│  └─ hype_sources.py
├─ tests/
├─ .gitignore
├─ README.md
└─ requirements.txt
```

## 快速开始

```bash
pip install -r requirements.txt
cp configs/.env.example configs/.env
python src/binance_monitor.py
```

运行前请编辑 `configs/.env`，填入企业微信群机器人 `WECOM_KEY`。

如果不配置 `WECOM_KEY`，脚本会以 DRY-RUN 模式运行，只打印信号，不推送消息。

## 启动脚本

各脚本独立循环运行：启动雷达默认 180 秒一扫，热点形态默认 900 秒。

```bash
python src/binance_monitor.py
python src/hype_form.py
```

如果只想单次运行热点形态：

```bash
FORM_RUN_ONCE=1 python src/hype_form.py
```

## 启动雷达信号逻辑 binance_monitor.py

目标：在币**刚启动、还没冲上 24h 涨幅榜**时就发现它。**只做预警，不给开单点**——确认后用你自己的 4H/1H 右侧结构再决定是否进场。

候选池：扫描 24h 成交额 ≥ `MIN_24H_QUOTE_VOL` 的 USDT 合约，取成交额 Top `TOP_N_BY_VOLUME`。

单根命中条件（**全部满足**才算这根 15m 命中，均可在 `.env` 配置）：

| 条件 | 默认 | 含义 |
| --- | --- | --- |
| 24h 涨幅 | `1% ~ 10%` | 刚启动，排除已经在榜上的 |
| 15m 量比 | `≥ 3.0` | 当前 15m 量 / 前 20 根均量，突然放量 |
| 15m 涨幅 | `1.5% ~ 9%` | 当根在加速，但不是已暴拉 |
| 1H OI 增幅 | `≥ 5%` | 资金/杠杆进场 |
| 资金费率 | `|funding| ≤ 0.25%` | 排除已过热 |

推送模型（**高频扫描、每小时汇总推一次**）：

- 每 `SCAN_INTERVAL_SEC=180` 秒扫一次，用**上一根已收盘的 15m K 线**判定（量比需站满一整根，假启动更少）。
- 每根 15m 的命中累积成「连续命中根数」：连命中则 +1，断一根则归零。
- 每 `PUSH_INTERVAL_SEC=3600` 秒（1 小时）推送一次,**只推连续命中 ≥ `MIN_CANDLE_STREAK`（默认 2）根、且在最新一根仍命中的币** —— 即持续启动，而非一次性闪现。单条消息最多列 `DIGEST_MAX_COINS=20` 个。
- 局限：抓的是「刚启动」不是「启动前」；务必配合止损、小仓试错。

## 热点币 4H 形态  Binance Monitor

只对 `hype_sources` 圈出的热点币（CoinGecko trending + 可选 `EXTRA_HYPE_KEYWORDS`）做
4H 形态快照——**不下推荐，只描述现状**。每个币给出：

- **形态分组**：完整多头 (20>50>100>200) / 完整空头 / 转折中 / 震荡
- **距 EMA20**：短期偏离 (signed %)
- **20 根位置**：当前价在最近 20 根 4H 高低区间的位置 (0-100%)
- **RSI(14)**
- **量能**：近 3 根均量 vs 前 20 根 → 扩量 / 平量 / 缩量
- **最近 3 根 4H**：颜色组合 (G=阳, R=阴, D=十字)
- **强做多观察**：完整多头里，RSI 回落到 60-68，且距 EMA20 回落到 10% 以内，会在推送顶部单独提醒。

```bash
python3 src/hype_form.py
EXTRA_HYPE_KEYWORDS="LUNC,MEME" python3 src/hype_form.py
```

合约优先取数（更好的杠杆条件），合约没有时退到现货，两者都没有列入"未上币安"。
跑一次 5–10 秒（只看 ~15 个热点币）。

```text
FORM_KLINE_INTERVAL       K 线周期，默认 4h
FORM_KLINE_LIMIT          拉取根数，默认 250
FORM_CONCURRENCY          并发抓 K 线，默认 15
FORM_STRONG_LONG_RSI_LO   强做多观察 RSI 下限，默认 60
FORM_STRONG_LONG_RSI_HI   强做多观察 RSI 上限，默认 68
FORM_STRONG_LONG_MAX_DIST 强做多观察距 EMA20 上限，默认 10
```

## 配置

默认读取 `configs/.env`。也可以通过环境变量 `CONFIG_FILE` 指定其他配置文件：

```bash
CONFIG_FILE=/path/to/.env python src/binance_monitor.py
```

环境变量优先级高于配置文件。

常用参数：

```text
MIN_24H_QUOTE_VOL       候选币 24h 成交额下限
MAX_24H_PRICE_PCT       过滤 24h 已大幅拉升的币，降低追高概率
KLINE_INTERVAL          默认 4h
STRUCTURE_LOOKBACK      前高/前低与成交量均值观察窗口，默认20根4H
EMA_FAST_PERIOD         快EMA周期，默认20
EMA_MID_PERIOD          中EMA周期，默认50
EMA_SLOW_PERIOD         慢EMA周期，默认100
A_MIN_4H_PRICE_PCT      A级信号要求的当前4H最小涨幅
A_MAX_4H_PRICE_PCT      A级信号允许的当前4H最大涨幅，避免过度追高
A_MIN_4H_VOL_RATIO      A级信号要求的当前4H量比
A_MAX_DIST_ABOVE_EMA_FAST_PCT A级距离EMA20上限
A_MIN_OI_4H_PCT         A级信号要求的OI 4H增长
B_MAX_4H_PRICE_PCT      B级蓄势允许的当前4H最大涨幅
B_MAX_DIST_ABOVE_EMA_FAST_PCT B级距离EMA20上限
B_EMA_MID_TOLERANCE_PCT B级允许回踩EMA50的容差
B_MIN_DIST_TO_HIGH_PCT  B级距离前高下限，避免已经贴近前高
B_MAX_DIST_TO_HIGH_PCT  B级距离前高上限，避免太弱
C_MAX_DIST_TO_HIGH_PCT  C级高位观察距离前高上限
C_MIN_DIST_ABOVE_EMA_FAST_PCT C级距离EMA20下限
C_MAX_DIST_ABOVE_EMA_FAST_PCT C级距离EMA20上限
WARN_DIST_ABOVE_EMA_FAST_PCT 当前价高于EMA20多少开始提示高位风险
MAX_DIST_ABOVE_EMA_FAST_PCT  当前价高于EMA20多少直接过滤，避免追高
MAX_ABS_FUNDING_PCT     资金费率绝对值上限，过滤拥挤交易
```

cd E:\code\trading

Start-Process .\.runtime\python312\tools\python.exe -ArgumentList "-u src\binance_monitor.py" -WindowStyle Hidden -RedirectStandardOutput logs\binance_monitor.out.log -RedirectStandardError logs\binance_monitor.err.log

Start-Process .\.runtime\python312\tools\python.exe -ArgumentList "-u src\hype_form.py" -WindowStyle Hidden -RedirectStandardOutput logs\hype_form.out.log -RedirectStandardError logs\hype_form.err.log
