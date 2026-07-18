# DOCS

本文件是本仓库唯一权威的功能与架构文档。任何影响系统行为、接口、配置、验证方式或安全边界的变更，都必须在同一个提交中同步更新本文件。

## 当前状态

仓库处于初始化阶段，尚未包含业务代码。目前只有协作规范与提交信息校验工具链。

## 目录结构

- `agents.md` — Agent 协作约定（提交粒度、提交信息格式、共同作者 trailer、推送要求）。
- `scripts/check_commit_messages.py` — 提交信息校验器，本地 hook 与 CI 共用。
- `.githooks/commit-msg` — 版本化的 `commit-msg` hook，提交时调用校验器。
- `.gitignore` — 忽略 `.DS_Store`、`.venv/` 及 Python 编译产物。

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
5. message 中不得包含字面量 `\n`。

### 调用方式

```bash
python scripts/check_commit_messages.py --message-file <path>   # commit-msg hook 使用
python scripts/check_commit_messages.py --commit HEAD           # 提交后、推送前复验
python scripts/check_commit_messages.py --base <sha> --head <sha>  # CI 校验区间
```

退出码为 `0` 表示通过，`1` 表示存在违规并在 stderr 打印原因。

## 本地环境搭建

```bash
python3 -m venv .venv
git config core.hooksPath .githooks
```

`core.hooksPath` 必须指向 `.githooks`，否则本地提交不会被校验。hook 优先使用 `.venv/bin/python`，缺失时回退到 `python3`；校验器只依赖标准库，无需安装第三方包。

## 验证流程

提交后、推送前执行 `python scripts/check_commit_messages.py --commit HEAD`，以 Git 实际解析出的 message 再次确认，然后推送到当前分支的远端上游。
