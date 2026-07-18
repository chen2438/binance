# DOCS

本文件是本仓库唯一权威的功能与架构文档。任何影响系统行为、接口、配置、验证方式或安全边界的变更，都必须在同一个提交中同步更新本文件。

## 项目目标

CandlePilot 用于研究 **Binance USDT-M 永续合约**：先用规则筛选标的，再在选出的标的上回测日内到周内级别的策略，杠杆上限 20 倍。

**当前范围仅限回测。** 代码不包含、也不应引入下单、撮合接入、API key 或任何账户操作；数据层只读取公开历史数据。

## 当前状态

已完成数据管线（本文档「数据管线」一节）。回测引擎与筛选层尚未实现。

## 目录结构

- `agents.md` — Agent 协作约定（提交粒度、提交信息格式、共同作者 trailer、推送要求）。
- `scripts/check_commit_messages.py` — 提交信息校验器，本地 hook 与 CI 共用。
- `.githooks/commit-msg` — 版本化的 `commit-msg` hook，提交时调用校验器。
- `src/candlepilot/data/` — 历史数据管线（下载、校验、解析、落盘）。
- `src/candlepilot/cli.py` — `candlepilot` 命令行入口。
- `tests/` — pytest 单元测试。
- `.gitignore` — 忽略 `.DS_Store`、`.venv/`、Python 编译产物及 `data/`（可从上游重建）。

## 数据管线

### 数据源

全部来自公开归档 `data.binance.vision`（`futures/um`），无需 API key。另外只读调用一次公开的 `fapi/v1/exchangeInfo`，用途仅为标记哪些标的已退市。

### 三个设计约束

管线的形态由三个回测正确性问题决定，它们都无法在事后补救：

1. **幸存者偏差** — 标的池取自 bucket 目录列表（曾经发布过数据的所有标的），而**不是** `exchangeInfo`（只含当前在交易的）。实测 787 个归档标的中有 262 个已退市，占 33%；只用存活标的回测会系统性高估收益。`universe.parquet` 的 `is_live` 列保留这一区分。
2. **资金费率** — funding 与 K 线同级采集（`fundingRate` 数据集），不做事后近似。日内到周内持仓下，8 小时一次的 funding 对损益的影响与手续费同量级。
3. **前视偏差** — 筛选层必须只使用当时可得的数据。数据层为此保留每根 K 线的原始 `open_time`，不做任何跨期填充或前向插值。

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

公共参数：`--root`（存储根目录，默认 `data/`）、`--interval`（默认 `1m`）、`--kinds`、`--workers`（默认 8）、`-v`。

标的在某周期未上市时归档不存在，这是正常情况，计入 `missing` 而非 `failed`。

### 读取

```python
from candlepilot.data.store import ParquetStore

store = ParquetStore("data")
klines = store.load_klines("BTCUSDT", "1m", start="2024-01", end="2024-06")
funding = store.load_funding("BTCUSDT")
```

返回的 DataFrame 以 UTC DatetimeIndex 索引，按时间排序且去重。

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
