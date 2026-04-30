# Binance Monitor

币安 U 本位永续合约 4H K 线结构监控脚本，命中启动或观察信号后推送到企业微信群机器人。

## 项目结构

```text
trading/
├─ configs/
│  └─ .env.example
├─ src/
│  ├─ binance_monitor.py
│  ├─ hype_radar.py
│  └─ signal_screener.py
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

## 信号逻辑

- 候选池：扫描 24h 成交额满足阈值的 USDT 合约，过滤 24h 已经过度拉升的币。
- A 级 4H 启动：价格处于 EMA20/50 多头结构，当前 4H 放量上涨，OI 同步增长；适合等回踩 EMA20/50 不破后的右侧多点。
- B 级 4H 蓄势：价格贴近 EMA20/50，距离前高还有空间，涨幅不大但量和 OI 开始转强；最适合低位观察做多。
- C 级 4H 高位观察：价格已经靠近前高并处于高位突破区；只看回踩 EMA20/50 后的修复，不追高。
- 追高过滤：价格距离 EMA20 过远时过滤信号；接近过热区时在推送中提示高位加速风险。
- 冷却：同一币种默认 4 小时内不重复推送同级别信号。

这版监控更适合提前发现 4H 级别的启动和蓄势币，不再以 5 分钟急拉作为主要判断依据。

## 热度雷达 hype_radar.py

把"舆情热度"和"行情波动"对齐到同一个币种维度，分三类输出：

- **A 趋势**：涨幅 ≥ 阈值 且 振幅 ≥ 阈值，量价齐升的真趋势（默认看合约盘）
- **B 暴雷**：跌幅 ≤ 阈值 且 振幅 ≥ 阈值，已经崩盘的绞肉机，仅供回避或做空研究
- **C 舆情驱动**：CoinGecko trending ∩ 币安 USDT 交易对，能交易的"被搜的币"

数据源全部公开免登录：

- **CoinGecko `/search/trending`**：近 24h 用户搜索最热的 15 个币
- **Binance 现货 + U 本位合约 24h ticker**：涨幅、振幅、成交额

```bash
python3 src/hype_radar.py

# 可选：补一些 CoinGecko 没收录但你想盯的币
EXTRA_HYPE_KEYWORDS="MEME,POPCAT" python3 src/hype_radar.py
```

推送：脚本读取 `configs/.env` 里的 `WECOM_KEY`（与 `binance_monitor.py` 共用），
跑完会把 **S 超级信号 / C 舆情驱动 / A 趋势 / B 暴雷** 四个桶用 markdown 推到企业微信。
未配置时自动 DRY-RUN，只打印不推送。可以挂到 cron 定时跑：

```bash
# 每小时整点跑一次
0 * * * * cd /home/trading && /usr/bin/python3 src/hype_radar.py >> /tmp/radar.log 2>&1
```

可调阈值（环境变量）：

```text
RADAR_MIN_QUOTE_VOL       候选币 24h 成交额下限，默认 5,000,000
RADAR_TOP_GAINERS         涨幅榜显示条数，默认 15
RADAR_TOP_VOLATILE        振幅榜显示条数，默认 15
RADAR_TREND_MIN_GAIN_PCT  A 类趋势涨幅下限，默认 5.0
RADAR_TREND_MIN_AMP_PCT   A 类趋势振幅下限，默认 15.0
RADAR_CRASH_MAX_LOSS_PCT  B 类暴雷跌幅上限（负数），默认 -10.0
RADAR_CRASH_MIN_AMP_PCT   B 类暴雷振幅下限，默认 30.0
RADAR_WECOM_TOP_N         推送时每个桶的最大行数，默认 8
EXTRA_HYPE_KEYWORDS       手动补充进 C 类的币种，逗号分隔
WECOM_KEY                 企业微信群机器人 key，留空则 DRY-RUN
```

## 4H 信号筛选 signal_screener.py

把 `hype_radar` 圈出的「值得看」升级为「**该怎么操作**」。逻辑：扫全部成交额过线的
U 本位合约 4H K 线，用 EMA20/50/100/200 + RSI(14) + 量比，分四个桶：

- **做多**：完整多头排列 + RSI [40,65] + 距 EMA20 ∈ [0,12]% + 量比 ≥ 1.2 → **现在可建仓**
- **做空**：完整空头排列 + RSI [35,60] + 距 EMA20 ∈ [-12,0]% + 量比 ≥ 1.2 → **现在可建仓**
- **回踩多观察**：完整多头但 RSI > 65 或距 EMA20 > 12% → **等回踩 EMA20/50**
- **反弹空观察**：完整空头但 RSI < 35 或距 EMA20 < -12% → **等反弹到 EMA20/50**

打分维度：EMA 排列(40) + 距 EMA20(20) + 量比(20) + RSI 位置(20)，每桶取 TOP 5 推送。
指标全部用**已收盘**的 4H bar 计算（避开当前进行中的 bar 噪音）。

```bash
python3 src/signal_screener.py
```

可调参数（环境变量）：

```text
SCREEN_KLINE_INTERVAL     K 线周期，默认 4h
SCREEN_KLINE_LIMIT        拉取根数（要 ≥ 200 才能算 EMA200），默认 250
SCREEN_MIN_QUOTE_VOL      候选币 24h 成交额下限，默认 5,000,000
SCREEN_CONCURRENCY        并发抓 K 线，默认 20
SCREEN_MAX_DIST_PCT       距 EMA20 过滤上限，默认 12.0
SCREEN_MIN_VOL_RATIO      量比下限，默认 1.2
SCREEN_LONG_RSI_LO/HI     做多 RSI 窗口，默认 40 / 65
SCREEN_SHORT_RSI_LO/HI    做空 RSI 窗口，默认 35 / 60
SCREEN_HOT_RSI            过热 RSI 阈值（路由到回踩观察），默认 65
SCREEN_COLD_RSI           超卖 RSI 阈值（路由到反弹观察），默认 35
SCREEN_TOP_N              每桶推送条数，默认 5
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
