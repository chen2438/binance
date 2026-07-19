# DOCS

本文件是本仓库唯一权威的功能与架构文档。任何影响系统行为、接口、配置、验证方式或安全边界的变更，都必须在同一个提交中同步更新本文件。

## 项目目标

CandlePilot 用于研究 **Binance USDT-M 永续合约**：先用规则筛选标的，再在选出的标的上回测日内到周内级别的策略，杠杆上限 20 倍。

**当前范围仅限回测。** 代码不包含、也不应引入下单、撮合接入、API key 或任何账户操作；数据层只读取公开历史数据。

## 当前状态

已完成数据管线、回测引擎核心、标的筛选层、多标的组合回测与评估层，并做过一轮基线测量。

## 目录结构

- `agents.md` — Agent 协作约定（提交粒度、提交信息格式、共同作者 trailer、推送要求）。
- `scripts/check_commit_messages.py` — 提交信息校验器，本地 hook 与 CI 共用。
- `.githooks/commit-msg` — 版本化的 `commit-msg` hook，提交时调用校验器。
- `src/candlepilot/data/` — 历史数据管线（下载、校验、解析、落盘）。
- `src/candlepilot/backtest/` — 回测引擎（成本模型、持仓与强平、执行语义、单标的与组合事件循环、指标）。
- `src/candlepilot/screen/` — 标的筛选层（日线面板、时点特征、滚动标的池）。
- `src/candlepilot/evaluate/` — 评估层（walk-forward 切分、参数扫描、多重检验校正）。
- `src/candlepilot/strategies/` — 策略；参考实现与三个固定参数的基线假设。
- `scripts/run_baselines.py` — 基线测量脚本。
- `src/candlepilot/cli.py` — `candlepilot` 命令行入口。
- `tests/` — pytest 单元测试。
- `.gitignore` — 忽略 `.DS_Store`、`.venv/`、Python 编译产物及 `data/`（可从上游重建）。

## 数据管线

### 数据源

全部来自公开归档 `data.binance.vision`（`futures/um`），无需 API key。另外只读调用一次公开的 `fapi/v1/exchangeInfo`，用途仅为标记哪些标的已退市。

### 三个设计约束

管线的形态由三个回测正确性问题决定，它们都无法在事后补救：

1. **幸存者偏差** — 标的池取自 bucket 目录列表（曾经发布过数据的所有标的），而**不是** `exchangeInfo`（只含当前在册的）。实测 787 个归档标的的状态分布：

   | 状态 | 数量 | 含义 |
   |---|---|---|
   | `TRADING` | 525 | 正常交易 |
   | `DELISTED` | 139 | 已退市，不在 exchangeInfo 中 |
   | `SETTLING` | 122 | 正在结算下架，**仍在发布日线** |
   | `PENDING_TRADING` | 1 | 待上市 |

   按 `status == TRADING` 取标的池会排除 262 个标的（33%），只用它们回测会系统性高估收益。注意 `SETTLING` 既不算干净的存活也不算干净的退市——它不在交易集合里，却仍有当日数据；把它简单归入"已退市"会同时高估退市数量并错标仍有活跃 K 线的标的。`universe.parquet` 因此原样记录 `status`，`is_live` 仅是 `status == "TRADING"` 的便捷列。
2. **资金费率** — funding 与 K 线同级采集（`fundingRate` 数据集），不做事后近似。日内到周内持仓下，8 小时一次的 funding 对损益的影响与手续费同量级。
3. **前视偏差** — 筛选层必须只使用当时可得的数据。数据层为此保留每根 K 线的原始 `open_time`，不做任何跨期填充或前向插值。

### 标记价格（markPriceKlines）

Binance 的**强平按标记价格触发，不是最新成交价**。标记价锚定现货指数并做了平滑，专门用于防止插针爆仓，因此必须单独采集。

实测量级（BTCUSDT 2020-03-13，COVID 崩盘）：最新成交价一度低于标记价 **6.8%**，而 20x 杠杆的强平距离约为 4.6%。用最新价判定强平会把这个本应存活的仓位判为爆仓——**误差方向是系统性的**，且恰好集中在极端行情，也就是回测结果最依赖它的地方。

平静期的差异同样存在但较小：2024-06 单分钟下插幅度 p99.9，最新价 0.36% 对标记价 0.32%（DOGEUSDT 为 0.71% 对 0.60%）。

**标记价序列存在缺口**，最新价序列没有：BTCUSDT 2020-01-19 缺 29 根 1m K 线。两个序列不能假定逐根对齐，使用前必须显式对齐并处理缺口。

标记价归档复用 K 线布局，但所有成交量字段恒为 0（标记价由现货指数推导，不来自成交），因此只保留 OHLC。

### 归档格式的坑

解析器处理两个实际存在的格式差异（见 `src/candlepilot/data/schema.py`）：

- **表头不一致**：2020 年前后的归档没有表头行，2024 年之后有。解析器探测首行而非假定 `header=0`；写死任一种都会导致丢一根 K 线或把表头当数据。
- **时间戳单位**：目前统一为毫秒。Binance 曾在其他数据集上更改过单位，因此解析器断言时间戳落在合理的毫秒区间，超出范围直接报错（`SchemaError`），而不是静默地把所有 K 线偏移。

下载后按 `.CHECKSUM` 做 SHA256 校验，不匹配则拒绝落盘。

### 采集周期选择

- **已收盘的月份**：使用月度归档，每个标的每月一个请求。
- **当前月份**：使用日度归档，且只到**上一个已收盘的 UTC 日**——当天的归档尚未发布。

两者一经发布即不可变，因此已落盘的 parquet 会被跳过，重跑是增量的。

### 存储布局

```
data/
  universe.parquet
  klines/<interval>/<SYMBOL>/<SYMBOL>-<interval>-<period>.parquet
  markPriceKlines/<interval>/<SYMBOL>/<SYMBOL>-<interval>-<period>.parquet
  fundingRate/<SYMBOL>/<SYMBOL>-fundingRate-<period>.parquet
```

一个采集周期对应一个文件，这正是增量重跑的依据。写入先落临时文件再 `replace`，避免中断留下截断的 parquet 被后续运行误判为已采集。

### 命令行

```bash
candlepilot universe                                      # 刷新标的池（含退市标的）
candlepilot ingest --symbols BTCUSDT ETHUSDT \
    --start 2020-01 --end 2026-07                         # 采集指定标的
candlepilot ingest --all --start 2023-01 --end 2026-07    # 采集整个标的池
candlepilot ingest --all --live-only ...                  # 仅存活标的（会引入幸存者偏差，慎用）
candlepilot status                                        # 查看已采集覆盖范围
```

公共参数：`--root`（存储根目录，默认 `data/`）、`--interval`（默认 `1m`）、`--kinds`（默认三者全采）、`--workers`（默认 8）、`-v`。

HTTP 连接池按 `--workers` 自动调整。`requests` 默认只有 10 条连接，worker 多于此时每条多出的连接都会被丢弃重建，全量采集会退化成每个归档付一次 TLS 握手。

标的在某周期未上市时归档不存在，这是正常情况，计入 `missing` 而非 `failed`。

### 读取

```python
from candlepilot.data.store import ParquetStore

store = ParquetStore("data")
klines = store.load_klines("BTCUSDT", "1m", start="2024-01", end="2024-06")
mark = store.load_mark_klines("BTCUSDT", "1m")   # 强平判定用
funding = store.load_funding("BTCUSDT")
```

返回的 DataFrame 以 UTC DatetimeIndex 索引，按时间排序且去重。

## 回测引擎

### 排序规则

引擎的正确性主要由三条排序规则承担（见 `backtest/engine.py`）：

1. **信号在下一根 K 线开盘执行。** 策略在第 `i` 根收盘时决策，第 `i+1` 根开盘成交，决策无法消费自身结果。
2. **K 线内路径假定不利。** 1m 粒度下无法知道价格在一根内的走法，因此同一根内触及多个价位时，不利的先触发；两个不利价位之间，离开盘价近的先触发。反向处理会产生系统性乐观偏差，且偏差随止损收紧而放大——正是日内策略所在的区间。
3. **强平判定用标记价，止损止盈用成交价。** 两者是不同序列，理由见「标记价格」一节。

**跳空穿越**：止损是触发条件而非成交保证。若一根 K 线开盘价已越过止损，成交按开盘价计而非止损价。忽略这一点会让回测在本该重创的行情里毫发无损——而且这正是强平的现实路径，因为正确的仓位规模会让止损在普通 K 线里总是先于强平触发。

### 强平与仓位规模

强平**刻意建得粗**。Binance 真实的维持保证金来自按名义价值分层的 MMR，分层历次调整且不公布历史值，因此"精确"重建只会精确地错。用单一保守值（0.5%）诚实得多。

这个近似之所以够用，是因为**强平不应成为约束**：20x 下强平位约在 4.5% 外，而实测崩盘月（2024-08 DOGEUSDT）单分钟标记价最大跌幅只有 3.0%。强平是"仓位算错了"的告警，不是常规退出，因此结果里单独统计 `liquidations` 而不混进普通交易。

维持这一点需要仓位规模配合。`size_for_risk` 先按「每笔风险占权益的固定比例」和止损距离定数量，再**按止损距离补足保证金**，使强平位始终在止损之外（默认留 1.5 倍缓冲）。若只按 `名义价值/最大杠杆` 交保证金，每笔都会顶在 20x，强平位固定在 4.5% 外，任何比它宽的止损都永远无法触发，强平就退化成了常规退出路径。

### 成本模型

成本是**被扫描的参数，不是常数**。`sweep_costs` 用同一策略跑四档场景并对比，单一成本假设下的回测基本不具参考价值。

| 场景 | 单边滑点 | 往返成本 |
|---|---|---|
| `optimistic` | 0 | 0.10%（仅手续费，不可能跑赢的上界） |
| `base` | 0.02% | 0.14% |
| `conservative` | 0.05% | 0.20% |
| `stress` | 0.10% | 0.30% |

费率按 Binance USDT-M VIP0：maker 0.02%，taker 0.05%（每边）。滑点另有 `slippage_for_liquidity`，按标的每根 K 线的成交额中位数分层——固定滑点会系统性美化流动性最差的小市值标的，而筛选规则最容易命中的恰是这些。

**杠杆会放大成本相对权益的占比，这一点比放大盈亏更容易被忽略。** 20x 下往返 0.10% 的手续费相当于权益的 2%；若每笔风险预算是权益的 1%，手续费就是风险预算的两倍。实测参考策略单笔最差止损 -172.5，拆开是价格走到止损 -88（在 1% 预算内）、滑点 -24、手续费 -60。策略设计必须从一开始就按这个量级考虑交易频率。

### 用法

```python
from candlepilot.data.store import ParquetStore
from candlepilot.backtest import build_bars, sweep_costs, Backtest, summarize

store = ParquetStore("data")
bars = build_bars(store, "BTCUSDT", start="2024-01", end="2024-06")

result = Backtest(bars, symbol="BTCUSDT").run(MyStrategy())
print(summarize(result))

print(sweep_costs(bars, lambda: MyStrategy(), symbol="BTCUSDT"))
```

策略实现 `on_bar(ctx) -> Intent | None`。`ctx.history` 是第 0..i 根，是策略能看到的唯一历史窗口；引擎不暴露完整 frame，以免无意中前视。`Intent` 的 `action` 取 `"long" / "short" / "exit"`，做多做空必须给 `stop_price`（没有止损就无法定仓位规模）。

`build_bars` 负责对齐：以成交价索引为准，标记价前向填充但限制陈旧时长（默认 15 分钟），无法确定标记价的 K 线标记为 `tradeable=False`，引擎在这些 K 线上拒绝开仓——没有可用标记价意味着强平判定是瞎的。

`Backtest` 参数：`initial_equity`（默认 10000）、`risk_fraction`（每笔风险占权益比例，默认 1%）、`max_leverage`（默认 20，超过 20 直接报错）、`mmr`。

### 组合回测

`PortfolioBacktest` 把筛选层的滚动标的池接到引擎上，在共享权益下同时运行多个标的。

**执行语义与单标的引擎共用** `SymbolExecutor`，两者的开仓、资金费结算、退出判定、平仓走同一份代码。若组合回测的成交判定与单标的哪怕只差一点，两者的结果就不再可比，而这个差异会被误读成策略效应而非引擎假象。

三条策略决定，每一条都是回测容易自我美化的地方：

1. **调出标的池不强制平仓。** 标的被调出只会阻止**新开仓**，已有持仓不动。强制平仓会让调仓节奏覆盖策略自身的退出逻辑——周度调仓会把持仓期悄悄截断到一周，量到的边际就成了筛选规则的而非策略的。需要相反行为时用 `close_on_pool_exit=True` 显式开启。
2. **退市强制平仓。** 持仓期间标的数据终止时，按最后一根 K 线收盘价平仓并标记 `delisted`。让持仓凭空消失等于把亏损丢在地上——正是数据层费力避免的幸存者偏差。
3. **权益共享。** 风险按组合总权益定仓，各标的争夺同一份资金，一个标的的回撤会缩小其余所有仓位。分别跑单标的回测再相加等于假设资金无限。

`max_positions` 限制同时持仓数；达到上限时新信号计入 `skipped_at_capacity` 而非静默丢弃。结果对象另外报告 `skipped_out_of_pool`、`skipped_untradeable`、`delisted_exits`，这些计数是判断"策略没交易"到底是没信号还是被约束挡住的依据。

```python
from candlepilot.backtest import PortfolioBacktest

bars = {sym: build_bars(store, sym, start="2024-03", end="2024-06") for sym in symbols}
pool = Screener(rule, rebalance="W-MON").to_frame(features)   # date, symbol
result = PortfolioBacktest(bars, pool, max_positions=3).run(lambda: MyStrategy())
print(result.by_symbol())
```

`pool` 是含 `date` 与 `symbol` 两列的表，即 `Screener.to_frame` 的输出。策略工厂按标的各调用一次，因此有状态的策略不会在标的之间串状态。

时间轴取所有标的时间戳的并集，因此上市时间不同的标的不会互相错位。

### 参考策略

`strategies/reference.py` 的 `DonchianBreakout` **不是研究成果**，它存在的目的是让引擎能端到端验证、让成本扫描有东西可跑。它产出的任何回测数字都应视为测试夹具。

## 标的筛选层

### 日线筛选 / 1m 执行

筛选跑日线，执行跑 1m。这不是妥协：同一个月的日线归档比 1m 归档小约 **865 倍**，这个差距决定了能筛全部 787 个标的、还是只能筛"手头方便的那几个"。而按方便程度挑标的池，正是幸存者偏差在被设计排除之后重新溜回来的路径。

面板采用长表（`date`, `symbol` 双重索引）而非宽表。标的的存续区间各不相同，宽表会为尚未上市的标的凭空造出行，而那些 NaN 恰恰是粗糙的排序会当成信号的东西。

### 时点正确性

所有特征都用滚动窗口计算，然后**统一右移一天**：日期 `T` 上携带的值只由 `T-1` 及之前的数据推出。移位在 `compute_features` 里集中做一次，而不是每个特征各做各的——这样新加的特征不可能忘记移。

移位按标的分组进行，因此在面板中相邻的两个标的之间，后一个不会继承前一个的最后一行。

内置特征：`liquidity`（成交额中位数）、`realized_vol`、`momentum`、`funding_carry`、`range_ratio`、`dollar_range`。

### 退市标的的处理

退市在两个方向上都必须正确，方向相反：

- 退市**之后**不可被选中。这是自动成立的：标的退市后面板里就没有它的行，任何后续调仓日都取不到它。
- 退市**之前**必须可被选中。在 `T` 日选出的池子里包含一个 `T+3` 退市的标的是**正确的**——当时无从得知。把它剔除就是用了后见之明，而这种剔除正是抬高回测表现的主要来源。

两个方向都有测试覆盖。

### 调仓与标的池

`Screener` 在每个调仓日对当日的合格截面应用规则。调仓日会吸附到面板中真实存在的日期上，否则一个没有数据的调仓日会静默地选出空池。

合格性要求特征齐备且 `age_days >= min_history`——刚上市三天的标的不该参与排序。

`turnover` 报告每次调仓被替换掉的比例（新池中不在旧池里的占比）。换手率高意味着规则在追噪声，而每一次替换都要付回测成本扫描里量到的往返成本。

### 用法

```bash
candlepilot screen --rank-by dollar_range --top 20 --min-liquidity 1e6 --rebalance W-MON
```

```python
from candlepilot.screen import build_panel, compute_features, Screener, top_n

panel = build_panel(store, interval="1d", start="2023-01")
features = compute_features(panel, window=30, min_history=30)
rule = top_n("dollar_range", n=20, filters={"liquidity": (">=", 1e6)})
selections = Screener(rule, rebalance="W-MON").run(features)
```

规则是 `Callable[[pd.DataFrame], list[str]]`，接收单个调仓日的合格截面，返回标的列表。`top_n` 只是内置的一种。

## 评估层

这一层**在规则搜索开始之前**建成，是刻意的。一旦看过样本内结果，之后关于切分方式、目标函数、校正方法的每个选择都由一个已经知道答案的人做出，校正也就不再是校正。

### 为什么单靠样本外不够

扫描 N 组参数必然产生一个好看的最优结果，即使没有任何参数组具备边际——N 个含噪 Sharpe 的最大值随 N 增长。把这个最大值当作单一假设来汇报，是策略研究里最核心的过拟合错误。

实测演示（`纯随机游走`数据，60000 根 1m K 线，零成本）：

| 指标 | 值 |
|---|---|
| 扫描参数组数 | 72 |
| 最佳年化 Sharpe | **5.00** |
| 总收益 | +29.4% |
| PSR（对比零） | **0.9544** |
| 72 次试验的期望最大 Sharpe | 4.35（年化） |
| **DSR（对比运气最大值）** | **0.5865** |

数据里没有任何可提取的结构，但最佳参数组给出年化 Sharpe 5.00。**PSR 会以 0.954 放行它**（超过 95% 门槛）；只有把搜索次数计入的 DSR 才判定它与运气无异。

### 三个统计量

按 Bailey 与 López de Prado：

- **PSR**（`probabilistic_sharpe`）— 真实 Sharpe 超过基准的概率，校正样本长度、偏度与峰度。交易收益既不正态也不独立，而裸 Sharpe 默默假设两者都成立。
- **E[max SR]**（`expected_max_sharpe`）— N 次试验中最好的那个仅凭运气能达到的 Sharpe。这是搜索赢家必须跨过的门槛。
- **DSR**（`deflated_sharpe`）— 以 E[max SR] 为基准的 PSR，即"赢家真的胜过运气本来就会产出的水平"的概率。**低于约 0.95 即无法与搜索运气区分。**

另有 `minimum_track_record_length`：回答"这段回测长度是否足以支撑这个结论"，而裸 Sharpe 本身从不提出这个问题。

所有函数都在收益的原始频率上工作。先年化再做检验会在不增加观测数的前提下抬高统计量，而这正是它们要抓的错误。

### Walk-forward 与禁运期

`walk_forward` 生成滚动或锚定的训练/测试折。**禁运期（embargo）不是可选的礼节**：测试窗第一根 K 线上的策略会读取一个回看窗口，该窗口伸进训练数据，于是"样本外"期间的开头带着部分样本内信息。禁运期长度必须**至少等于策略最长的回看窗口**——短于回看窗口的禁运期恰好留下了它本要堵上的漏洞。

`rolling` 与 `anchored` 回答的是不同问题（近期数据 vs 全部历史），因此模式是显式参数而非推断得出，混用会让结果不可比。

窗口切分用布尔掩码而非 `.loc` 标签切片，因为标签切片右端闭合，会把边界那根 K 线同时放进训练集和测试集。

### 用法

```python
from candlepilot.evaluate import run_walk_forward

wf = run_walk_forward(
    bars, DonchianBreakout,
    {"lookback": [60, 120, 240], "max_hold": [120, 480]},
    train="30D", test="10D", embargo="1D",
)
print(wf.folds)      # 每折选中的参数与样本外表现
print(wf.report())   # 拼接后的样本外 Sharpe、PSR、DSR
```

参数只在**训练**窗口上选择，汇报的结果只来自选择者未见过的**测试**窗口。每次试验的 Sharpe 都被保留而非只留赢家的，因为试验间的离散度正是 `expected_max_sharpe` 的输入——没有它就无从判断最佳结果凭运气本该有多好看。

## 基线测量

### 目的与方法

在开始规则搜索之前测一轮经典假设，**不做参数优化**。每个假设用一组事先选定的教科书默认参数（20 日动量、5 日反转、7 日资金费；2xATR(14) 止损），跑筛选后的标的池与完整成本扫描。

不优化是关键：基线的价值就在于它没有被拟合，单一固定参数组让多重检验校正保持可解释。一个基线失败意味着一个方向被排除，这是策略研究里最便宜的有用结果。

### 结果（2023-01..2026-06，日线，base 成本，年化 Sharpe）

在三个标的口径上分别测量。三者不是嵌套关系，山寨口径与主流币口径完全不重叠。

| 假设 | 山寨 58 标的 | 主流币 10 标的 | 全宇宙 169 标的 |
|---|---|---|---|
| 动量延续 | -0.67 | **+0.22** | -0.43 |
| 均值回复 | -1.56 | -1.15 | -1.52 |
| 资金费率套利 | **+0.41** | -0.32 | +0.05 |

全宇宙口径由 775 个已采集标的经流动性筛选后得到 172 个曾入选标的，实际载入 169 个。

**三个假设都不构成可用方向**，但失败方式不同：

**均值回复：决定性排除。** 三个互不重叠的标的口径、四档成本，全部强负（-1.15 至 -1.72），DSR 一律为 0。这是目前最稳健的结论。

**动量：全宇宙为负，仅在主流币上弱正，且被成本吃光。** 主流币口径总收益从 optimistic 的 +14.3% 降至 stress 的 -9.1%，Sharpe 从 +0.28 降到 +0.01——正好跨越盈亏平衡。DSR 0.25，不显著。存在标的类型差异（主流币不为负），但差异不足以覆盖交易成本。

**资金费率套利：净额为零，且不是 carry 效应。** 全宇宙分解：

| 来源 | 金额 |
|---|---|
| 资金费收入 | +985 |
| 价格盈亏 | -588 |
| 手续费 | -440 |
| 净额 | **-44** |

收到的资金费几乎恰好被"为收取它而承担的价格风险"加手续费抵消。**资金费率是对价格风险的补偿，不是免费收益**——这个方向上市场定价是有效的。空头 636 笔净赚 650，多头 187 笔净亏 694，两者近乎抵消。

山寨口径上那个 +0.41 / +18% 是样本特有的：它在主流币上翻转为 -0.32，在全宇宙上归零。这与该口径的分解一致——当时 86% 的利润来自 55 个标的中的 3 个，由占少数的多头贡献，实质是在急跌后赌反弹而非收 carry。

### 样本宽度对结论的影响

动量在四个口径上的读数：meme 小样本 8 标的 **+0.72**、山寨 58 标的 **-0.67**、主流币 10 标的 **+0.22**、全宇宙 169 标的 **-0.43**。同一策略、同一时间段、同一参数，仅标的池不同。

窄样本上的基线结论不可外推。全宇宙口径是唯一可作为方向排除依据的读数。

### 复现

```bash
# 全宇宙
python scripts/run_baselines.py --root data --interval 1d --start 2023-01 --end 2026-06
# 主流币子集
python scripts/run_baselines.py --root data --interval 1d --top 10 \
    --symbols BTCUSDT ETHUSDT SOLUSDT XRPUSDT BNBUSDT DOGEUSDT ADAUSDT AVAXUSDT LINKUSDT LTCUSDT
```

## 本地环境搭建

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
git config core.hooksPath .githooks
```

## 提交信息校验

### 规则

校验器 `scripts/check_commit_messages.py` 对每条 message 检查：

1. 标题符合 Conventional Commit 格式（`build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test`，可带 scope 与 `!`）。
2. 标题后必须是一个空行。
3. 标题与结尾 trailer 之间必须有非空的正文描述。
4. 最后一行必须是被认可的 trailer：
   - `Co-authored-by: GPT-<版本> <noreply@openai.com>`
   - `Co-authored-by: Claude <...> <noreply@anthropic.com>`
   - 或纯人工提交的 `Human-authored: true`
5. 不得用字面量 `\n` 代替真正的换行。该检查只针对**结构位置**，即字面量 `\n` 出现在行尾、连续出现（充当空行），或紧接一个 trailer（形如 `Xxx-yyy: `）之前。正文中作为普通文字提到 `\n`（例如说明转义规则）不会被拦截。

### 调用方式

```bash
python scripts/check_commit_messages.py --message-file <path>   # commit-msg hook 使用
python scripts/check_commit_messages.py --commit HEAD           # 提交后、推送前复验
python scripts/check_commit_messages.py --base <sha> --head <sha>  # CI 校验区间
```

退出码为 `0` 表示通过，`1` 表示存在违规并在 stderr 打印原因。

`core.hooksPath` 必须指向 `.githooks`，否则本地提交不会被校验。hook 优先使用 `.venv/bin/python`，缺失时回退到 `python3`；校验器只依赖标准库，无需安装第三方包。

## 测试

```bash
.venv/bin/python -m pytest
```

单元测试不访问网络：覆盖归档格式差异（有/无表头、时间戳单位断言）与采集周期规划（月度/日度边界、当前月不含当天）。涉及真实下载的路径通过实跑验证，不进单元测试。

## 验证流程

提交后、推送前执行 `python scripts/check_commit_messages.py --commit HEAD`，以 Git 实际解析出的 message 再次确认，然后推送到当前分支的远端上游。
