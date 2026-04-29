# Binance Monitor

币安现货 + U 本位永续合约异动监控脚本，命中信号后推送到企业微信群机器人。

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

- 现货：5 分钟涨幅达到阈值，且 5 分钟成交额高于过去 1 小时均值指定倍数。
- 合约：5 分钟涨幅达到阈值，且 OI 5 分钟增量达到阈值。
- A 级共振：同一币种现货和合约同时命中。
- B 级单边：仅现货或仅合约命中。
- 冷却：同一币种默认 30 分钟内不重复推送。

## 配置

默认读取 `configs/.env`。也可以通过环境变量 `CONFIG_FILE` 指定其他配置文件：

```bash
CONFIG_FILE=/path/to/.env python src/binance_monitor.py
```

环境变量优先级高于配置文件。
