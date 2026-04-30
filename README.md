# Binance Monitor

币安 U 本位永续合约 4H K 线结构监控脚本，命中启动或观察信号后推送到企业微信群机器人。

## 项目结构

```text
trading/
├─ configs/
│  └─ .env.example
├─ src/
│  └─ binance_monitor.py
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
