# CandlePilot

研究 Binance USDT-M 永续合约的标的筛选规则与日内/周内量化策略，**仅做回测**，不接模拟盘或实盘。

完整文档见 [DOCS.md](DOCS.md)。

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
git config core.hooksPath .githooks

.venv/bin/candlepilot universe
.venv/bin/candlepilot ingest --symbols BTCUSDT --start 2024-01 --end 2026-07
.venv/bin/candlepilot status
```

数据来自公开归档 `data.binance.vision`，无需 API key。

## 状态

- [x] 历史数据管线（K 线 + 标记价 + 资金费率，含退市标的）
- [x] 回测引擎（撮合、持仓、funding、强平、成本扫描）
- [x] 标的筛选层（时点特征、滚动标的池、含退市标的）
- [x] 多标的组合回测（共享权益、退市强制平仓）
- [ ] 策略与评估
