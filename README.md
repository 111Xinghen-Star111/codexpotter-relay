# CodexPotter Relay

基于 [breezewish/CodexPotter](https://github.com/breezewish/CodexPotter) 二次开发的**全自动跨会话开发接力系统**：用户只提交一次开发目标，Codex 在上下文接近上限时自动保存进度、创建全新主会话并继续干活，直到任务真正完成。

## 它解决什么问题

单个 Codex 会话的上下文窗口是有限的。复杂、长时间的开发任务会在中途把上下文撑满，表现为：早期读过的代码被压缩掉、之前的技术决策被遗忘、用户被迫反复输入“继续”。

CodexPotter Relay 把这件事交给一个独立于会话之外运行的本地控制器：

- 上下文接近上限时自动写检查点、自动创建**全新主会话**接替，会话之间不共享历史，也不靠摘要续命；
- 原始开发目标在多次接力后保持一致（写在 `.relay/state.json` 与 `.relay/HANDOFF.md` 里，而不是留在某个会话的记忆里）；
- 全部任务完成且验收命令通过后自动停止并生成报告；
- 用户只需要提交一次 `relay start --goal ...`。

## 工作原理

```text
用户提交一次目标  relay start --goal "..." --project C:\work\my-project
        │
        ▼
Relay Controller（独立进程，不依赖任何 Codex 会话存活）
        │  1. 建工作链：chains 行 + 分支 relay/<chain_id> + .relay/ 台账
        ▼
主会话 #1（thread/start 创建，真正的全新线程）
        │  执行 turn：读 .relay/state.json → 写代码 → 提交 → 更新台账
        ▼
Context Monitor 采样上下文压力（真实 token 用量 / 模拟注入 / 回落信号）
        │  0.65 写检查点 │ 0.75 准备 │ 0.80 交接 │ 0.90 紧急交接
        ▼
交接状态机  PREPARE → START → VERIFY → COMMIT → CONTINUE
        │        │        │        │        │
        │        │        │        │        └─ 自动发送接管指令，要求新会话回报 ::relay(...) 标记
        │        │        │        └─ 把 active_session_id 切到新会话，旧会话标记 handed_off
        │        │        └─ 校验新线程 0 轮次、分支/HEAD 未变、台账未被替换
        │        └─ thread/start 创建主会话 #2（不是 resume，也不是 fork）
        └─ 写检查点并从磁盘重新读取校验
        │
        ▼
主会话 #2 读取 .relay/state.json + HANDOFF.md → 校验 git 状态 → 继续未完成任务
        │
        ▼
…… 主会话 #3、#4 ……（每次触发都重复上面的状态机）
        │
        ▼
全部任务完成 + 验收命令通过 → chain=complete → 自动停止并生成开发报告
```

**新会话是真正的全新主会话。** 控制器调用的是 app-server 的 `thread/start`，不是 `thread/resume`，也不是 `thread/fork`。`handoff.py` 在 `VERIFY` 阶段会强制检查两件事，任何一条不满足就放弃这个候选会话：该 thread id 在数据库里不存在（不是复用），且 `thread/read` 返回的 `turns` 为空（确实是干净的上下文）。

**只在安全边界交接。** 压力采样发生在一轮 turn 结束之后，控制器不会中断正在写入的操作；`urgent`（0.90）只是把交接级别记为紧急，动作仍然落在同一个边界上。

## 核心概念

| 概念 | 含义 | 落地位置 |
| --- | --- | --- |
| 工作链 chain | 一个用户目标从开始到完成的完整过程，可跨越多个主会话。id 形如 `ch_1a2b3c4d5e6f` | `chains` 表、分支 `relay/<chain_id>` |
| 主会话 session | 一次 `thread/start` 产生的 Codex 主线程，带主会话序号 1、2、3… | `sessions` 表（`role='main'`） |
| 检查点 checkpoint | 某一步的可恢复快照：git 分支/HEAD/脏标记 + 台账 + 交接文档，带 SHA-256 校验和 | `checkpoints` 表（版本号递增，不可变） |
| 交接 handoff | 写权限从一个主会话转移到另一个主会话的一次状态机执行，含重试与回滚 | `handoffs` 表（`sequence` 递增） |
| 台账 `.relay/state.json` | 机器可读的任务清单：任务状态、证据、决策、验收命令、下一个任务 | `<项目>/.relay/state.json`（被 git 忽略） |
| `HANDOFF.md` | 给下一个会话读的叙述式交接文档，沿用上游 CodexPotter 的章节结构 | `<项目>/.relay/HANDOFF.md`（被 git 忽略） |
| 事件 events | turn 结果、压力采样、检查点、交接、hooks、验收等不可变记录 | `events` 表（`event_key` 唯一，幂等） |

任务状态取值：`pending`、`in_progress`、`done`、`blocked`、`unverifiable`。会话结束时的最后一条消息需带一个标记：`::relay(progress)`（还有工作）、`::relay(done)`（全部完成）、`::relay(blocked)`（无法继续，链条转入 `paused` 等待人工决策）。

## 环境要求

| 项 | 要求 | 说明 |
| --- | --- | --- |
| 操作系统 | Windows 11（首选） | 代码中的非 Windows 分支可运行，但安装脚本与实测环境是 Windows |
| Python | 3.11+ | 使用 `tomllib`，因此 3.11 是硬下限 |
| Git | 任意近期版本 | 检查点依赖 `git status` / `git log`，Git for Windows 安装在 `C:\Program Files\Git` 也可 |
| Codex CLI | 需包含 `app-server` 子命令 | 本机实测版本 `0.167.0-alpha.3.2` |
| 账号 | ChatGPT 账号已登录 | `relay doctor` 会调用 `codex login status` 检查 |

Codex 可执行文件的查找顺序（`relay/codex_env.py`）：`controller.codex_executable` → 环境变量 `CODEXPOTTER_CODEX` → `PATH` 上的 `codex` → `%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe`（按体积取最新）→ `%LOCALAPPDATA%\Programs\codex\codex.exe`。

## 安装

仓库自带一键安装脚本 [`scripts\install.ps1`](scripts/install.ps1)，逐用户安装，不需要管理员权限。它依次做五件事：

1. 定位 Python 3.11+、git，并用 `python -m relay doctor` 验证 Codex 可用；
2. `pip install -e <仓库根目录>`，注册 `relay` 命令（`pyproject.toml` 中的 `[project.scripts]`）；
3. 把 Python 的 Scripts 目录加入**用户** PATH（`-SkipPath` 可跳过）；
4. `relay init` 创建 `relay.toml` 与 `relay.db`；
5. 可选：`-WithHooks` 时把托管 hooks 块写入 Codex 配置（默认**不写**，因为 Codex 0.167 不执行 `[hooks]` 配置项，状态采集走 app-server 通知）。

```powershell
# 在仓库根目录执行
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1

# 安装后新开一个终端，让 PATH 生效，然后自检
relay doctor
```

脚本参数：

| 参数 | 作用 |
| --- | --- |
| `-Python <path>` | 指定 python.exe，跳过自动探测 |
| `-Codex <path>` | 指定 codex.exe（等价于 `relay --codex <path> doctor`） |
| `-WithHooks` | 额外写入 Codex hooks 配置块（默认跳过） |
| `-SkipPath` | 不修改用户 PATH |

> Codex 不在 PATH 上时，全局参数 `--codex <path>` 可作用于任意子命令，例如
> `relay --codex "C:\Users\<you>\AppData\Local\OpenAI\Codex\bin\<hash>\codex.exe" doctor`；
> 也可以写入 `relay.toml` 的 `[controller] codex_executable` 或环境变量 `CODEXPOTTER_CODEX`。

手工安装等价步骤（不使用脚本时）：

```powershell
python -m pip install -e .
relay init
relay hooks install
# 若 relay 命令不在 PATH，可直接用模块方式调用：python -m relay <子命令>
```

## 快速开始

```powershell
# 1. 环境自检：Codex 路径/版本/登录状态、git 路径、python 路径
relay doctor

# 2. 初始化配置与数据库（重复执行是安全的，不会覆盖已有配置）
relay init

# 3. 提交一次开发目标。控制器在前台运行，直到链条停止
relay start --goal "给项目加 CSV 导出功能，并补齐单元测试" --project C:\work\my-project `
  --task "T1:实现 export_csv()" `
  --task "T2:为导出功能补测试" `
  --accept "python -m pytest -q"

# 输出示例：
# chain: ch_1a2b3c4d5e6f
# project: C:\work\my-project
# branch: relay/ch_1a2b3c4d5e6f

# 4. 后台运行（可选）：立即返回，控制器的 stdout/stderr 写到
#    %LOCALAPPDATA%\CodexPotterRelay\logs\controller-<chain_id>.out
relay start --goal "同上" --project C:\work\my-project --detach

# 5. 查看状态（不带 --chain 时显示最近创建的一条链）
relay status --chain ch_1a2b3c4d5e6f
relay status --chain ch_1a2b3c4d5e6f --events 20   # 附带最近 20 条事件

# 6. 暂停：控制器在下一个安全点停止，不再发起模型请求
relay pause --chain ch_1a2b3c4d5e6f

# 7. 恢复：resume 只改数据库状态，还需要重新启动控制器才会继续执行
relay resume --chain ch_1a2b3c4d5e6f
relay run --chain ch_1a2b3c4d5e6f

# 8. 查看报告（Markdown，写入 %LOCALAPPDATA%\CodexPotterRelay\reports\）
relay report --chain ch_1a2b3c4d5e6f
```

零额度自检：`relay test --mode mock` 完全不接触真实模型，可在正式跑目标之前确认状态机、数据库与检查点链路正常。

## 命令参考

所有子命令都接受三个全局参数：

| 全局参数 | 默认 | 作用 |
| --- | --- | --- |
| `--config PATH` | `%LOCALAPPDATA%\CodexPotterRelay\relay.toml` | 指定配置文件 |
| `--db PATH` | `%LOCALAPPDATA%\CodexPotterRelay\relay.db` | 覆盖数据库路径（`init/run/status/chains/checkpoint/handoff/pause/resume/stop/history/report` 使用） |
| `--json` | 关闭 | 输出机器可读 JSON 而不是文本 |

退出码：`0` 成功；`1` 运行失败或测试未通过；`2` 配置错误或参数错误。

### 命令总览

| 命令 | 作用 |
| --- | --- |
| `relay init` | 创建配置文件与数据库（已存在则不覆盖配置） |
| `relay doctor` | 检查 Codex、git、Python 前提条件 |
| `relay start` | 提交一个目标并启动工作链 |
| `relay run` | （内部）为已存在的工作链启动控制器 |
| `relay chains` | 列出工作链（最多 100 条） |
| `relay status` | 查看某条链的详细状态 |
| `relay checkpoint` | 立刻写一个检查点 |
| `relay handoff` | 立刻交接给新主会话 |
| `relay pause` / `relay resume` / `relay stop` | 暂停 / 恢复 / 永久停止 |
| `relay history` | 查看检查点、交接与事件历史 |
| `relay report` | 生成开发报告 |
| `relay recover` | 接管并继续被中断的工作链（崩溃恢复） |
| `relay test` | 运行指定测试层级 |
| `relay test-handoff` | 连续 N 次真实交接测试 |
| `relay test-report` | 重新汇总测试报告 |
| `relay hooks` | 安装 / 移除 / 查看 hooks 采集器 |
| `relay hook` | （内部）hooks 采集器入口 |

### 环境与初始化

| 命令 | 参数 | 默认 | 作用 |
| --- | --- | --- | --- |
| `relay init` | 无 | — | 写默认配置模板并迁移数据库，打印配置路径、数据库路径、schema 版本（`--json` 还包含 relay home） |
| `relay doctor` | 无 | — | 打印 `codex_executable`、`codex_version`、`codex_logged_in`、`codex_login_status`、`git_executable`、`python`、`relay_time`；缺 Codex 或 git 时退出码 1 |

### 启动与查看

| 命令 | 参数 | 默认 | 作用 |
| --- | --- | --- | --- |
| `relay start` | `--goal TEXT`（必填） | — | 开发目标，写入 `chains.goal` 与 `.relay/state.json` |
| | `--project PATH` | 当前工作目录 | 被开发的项目目录；不存在会被创建 |
| | `--task ID:TITLE`（可重复） | 单个任务 `T1`（标题=目标） | 任务清单；只写 `ID` 时标题与 ID 相同 |
| | `--accept CMD`（可重复） | 空 | 验收命令，写入 `.relay/state.json` 的 `acceptance.commands`，完成判定时执行 |
| | `--model NAME` | 配置 `relay.model` | 本次运行的模型覆盖 |
| | `--effort LEVEL` | 配置 `relay.effort` | 本次运行的推理强度覆盖（通过 `model_reasoning_effort` 传入） |
| | `--max-turns N` | 配置 `relay.max_turns` | 本次运行的 turn 上限 |
| | `--max-handoffs N` | 配置 `relay.max_handoffs` | 本次运行的交接次数上限 |
| | `--max-model-requests N` | 配置 `relay.max_model_requests` | 本次运行的模型请求上限 |
| | `--no-auto-handoff` | 关闭 | 只采集进度，不自动交接 |
| | `--detach` | 关闭 | 后台运行控制器，日志见 `logs/controller-<chain_id>.out` |
| `relay run` | `--chain ID`（必填） | — | （内部）为已存在的链启动控制器，用于崩溃后恢复或恢复暂停的链 |
| `relay recover` | 无 | 所有处于 `running` / `handoff` 的链 | 接管被中断的工作链：回收陈旧执行锁、校验台账并回退到最新有效检查点、把中断的交接标记为 `failed` 并退回 `running`，然后逐条继续执行。详见 `docs/恢复指南.md` |
| | `--chain ID` | 全部 | 只恢复指定链 |
| | `--dry-run` | 关闭 | 只列出待恢复的链与执行锁归属，不启动控制器 |
| | `--max-turns` / `--max-handoffs` / `--max-model-requests` | 配置文件 | 恢复时的预算覆盖 |
| `relay chains` | 无 | — | 列出最近 100 条链：ID / 状态 / 模式 / 项目路径 |
| `relay status` | `--chain ID` | 最近创建的一条链 | 打印状态、目标、项目与分支、活动会话、模型请求数、停止原因、会话列表、交接列表、检查点列表、任务列表；执行锁信息在 `--json` 输出中 |
| | `--all` | 关闭 | 保留参数，当前实现不改变输出内容 |
| | `--events N` | `0` | 额外打印最近 N 条事件 |

### 手动操作

| 命令 | 参数 | 默认 | 作用 |
| --- | --- | --- | --- |
| `relay checkpoint` | `--chain ID`（必填） | — | 立刻从磁盘台账 + 当前 git 状态写一个检查点 |
| | `--reason TEXT` | `manual` | 检查点原因，记录在 `checkpoints.reason` |
| `relay handoff` | `--chain ID`（必填） | — | 立刻执行一次完整交接；控制器正持有执行锁时会报错，提示先 `pause` |
| `relay pause` | `--chain ID`（必填） | — | 链转入 `paused`；控制器在下一个安全点停止，不再发起模型请求 |
| `relay resume` | `--chain ID`（必填） | — | `paused` → `running`（非 `paused` 状态会报错）；不启动控制器 |
| `relay stop` | `--chain ID`（必填） | — | 链转入 `stopped`；`complete`/`stopped`/`failed` 已是终态，再次执行不改变状态 |

### 历史与报告

| 命令 | 参数 | 默认 | 作用 |
| --- | --- | --- | --- |
| `relay history` | `--chain ID` | 最近 10 条链 | 按链打印检查点（版本/原因/HEAD 前 10 位/是否校验通过）与交接（序号/阶段/状态/from→to） |
| | `--limit N` | `20` | 每条链的检查点与事件条数上限 |
| | `--events` | 关闭 | 同时打印事件（时间/类型/载荷前 120 字符） |
| `relay report` | `--chain ID`（必填） | — | 生成 Markdown 开发报告，路径 `reports\<chain_id>-<时间戳>.md` |

### 测试

| 命令 | 参数 | 默认 | 作用 |
| --- | --- | --- | --- |
| `relay test` | `--mode mock\|real-handoff\|real-pressure`（必填） | — | 运行对应测试层级，全部写入测试库 `relay-test.db` |
| | `--keep-workspaces` | 关闭 | 保留临时测试项目（默认结束后删除） |
| `relay test-handoff` | `--count N` | `3` | 运行 N 次连续真实交接（即 Level 2，创建 N+1 个主会话） |
| | `--keep-workspaces` | 关闭 | 同上 |
| `relay test-report` | `--chain ID` | 测试库最近 20 条链 | 指定要汇总的链 |
| | `--mode ...` | `real-handoff` | 报告文件名与标题使用的模式名 |

### Hooks

| 命令 | 参数 | 作用 |
| --- | --- | --- |
| `relay hooks install` | — | 把托管块写入 `%USERPROFILE%\.codex\config.toml`（首次会备份为 `config.toml.relay-backup`），并用 `hooks/list` 回读 Codex 实际注册到的事件 |
| `relay hooks uninstall` | — | 移除托管块；配置块清空时恢复备份 |
| `relay hooks status` | — | 显示配置文件路径、托管块是否存在、实际观测到的 hook 事件（`--json` 还会给出请求注册的事件清单与 `hooks/list` 原始结果） |
| `relay hook` | `--event NAME`（必填）、`--db PATH` | （内部）从 stdin 读取 JSON 载荷并写入 `events` 表；始终退出 0，保证 hook 不会中断 turn |

## 配置

### 文件与格式

默认配置路径：`%LOCALAPPDATA%\CodexPotterRelay\relay.toml`（可用 `--config` 覆盖，或设置 `CODEXPOTTER_RELAY_HOME` 整体迁移状态目录）。

支持两种写法：TOML，或需求文档中那段嵌套两层的 YAML 子集（`key: value`、`#` 注释、引号字符串、布尔、整数、浮点）。YAML 子集**不支持**列表与锚点，遇到 `- ` 列表项会明确报错；只允许 `relay` / `testing` / `controller` 三段；未知段、未知键一律拒绝，避免拼写错误静默关掉安全限制。JSON 对象不被接受。

### `[relay]` 上下文阈值与循环上限

| 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `checkpoint_threshold` | `0.65` | 达到该占比后写检查点 |
| `prepare_threshold` | `0.75` | 达到该占比后进入准备级别（仍写检查点） |
| `context_threshold` | `0.80` | 达到该占比后在下一个安全边界执行交接 |
| `urgent_threshold` | `0.90` | 达到该占比后按紧急级别交接（动作边界相同） |
| `auto_handoff` | `true` | 关闭后只采集与写检查点，永不自动交接 |
| `max_handoffs` | `32` | 每条链的交接次数上限，用尽即 `stopped` |
| `max_turns` | `200` | 每条链的 turn 上限 |
| `max_model_requests` | `400` | 每条链的模型请求上限 |
| `token_budget` | 未设置（`null`） | 可选，整条链的 token 硬上限；设置时必须是正数 |
| `model` | 未设置 | 传给 `thread/start` 的模型；未设置时沿用 Codex 自身配置 |
| `effort` | 未设置 | 推理强度，作为 `model_reasoning_effort` 配置传入 |
| `max_handoff_retries` | `3` | 交接失败后的重试次数（最多尝试 `1 + 该值` 次，每次都是新线程） |
| `turn_timeout_seconds` | `7200` | 单轮 turn 的等待超时 |
| `fallback_turn_limit` | `40` | 无法取得可靠用量时，距上次检查点多少轮触发准备级别 |

校验规则：四个阈值必须落在 `(0, 1]` 且**严格递增**（`checkpoint < prepare < context < urgent`，相等也报错）；`max_handoffs`、`max_turns`、`max_model_requests`、`turn_timeout_seconds`、`fallback_turn_limit` 必须为正；`max_handoff_retries` 必须 ≥ 0。

### `[testing]` 测试设置

| 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `enabled` | `false` | 仅参与校验：为 `true` 时必须同时允许模拟或真实压力，否则拒绝启动一个永远不会触发的测试 |
| `simulate_context_pressure` | `false` | 标记允许注入模拟占比（Level 2 使用 `simulated_usage_ratio`） |
| `simulated_usage_ratio` | `0.80` | 注入的模拟占比，必须落在 `(0, 1]` |
| `max_handoffs` | `3` | 测试交接上限（当前 Level 执行器使用自己的预算，见「三层测试」） |
| `max_model_requests` | `16` | Level 2 的模型请求预算来源 |
| `max_turns` | `12` | 测试 turn 预算（当前 Level 执行器使用自己的预算） |
| `real_pressure_enabled` | `false`（模板中被注释） | 真实压力测试开关；`relay test --mode real-pressure` 会在测试内部强制打开 |
| `context_window_override` | 未设置（`null`） | 通过官方配置 `model_context_window` 覆盖上下文容量，不得小于 `1000` |
| `auto_compact_token_limit` | 未设置（`null`） | 通过官方配置 `model_auto_compact_token_limit` 覆盖压缩阈值，不得小于 `1000` |

`context_window_override` 与 `auto_compact_token_limit` 会以 `codex app-server ... -c key=value` 的形式传入，是本项目唯一改动上下文容量的手段，且仅用于测试链。

### `[controller]` 进程与连接

| 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `codex_executable` | 未设置 | 显式指定 codex.exe |
| `sandbox` | `workspace-write` | 沙箱模式，可选 `read-only`、`workspace-write`、`danger-full-access` |
| `approvals_reviewer` | `auto_review` | 审批复核者，可选 `user`、`auto_review`、`guardian_subagent` |
| `approval_policy` | `on-request` | 审批策略，可选 `untrusted`、`on-request`、`never` |
| `poll_interval_seconds` | `1.0` | 必须为正；当前控制器用通知驱动等待，该值仅作保留配置 |
| `startup_timeout_seconds` | `120` | app-server 启动与初始化握手超时 |
| `keep_app_server_stdout` | `false` | 保留配置，当前实现未读取 |

### 阈值含义

| 级别 | 触发条件 | 控制器动作 |
| --- | --- | --- |
| `ok` | 占比 < 0.65 | 继续下一轮 |
| `checkpoint` | 占比 ≥ 0.65 | 该轮 turn 结束后写检查点（`reason=pressure:checkpoint`），除非这一轮紧接着就要交接 |
| `prepare` | 占比 ≥ 0.75 | 同上，写检查点（`reason=pressure:prepare`）；完整交接包在真正交接的 `PREPARE` 阶段生成并校验 |
| `handoff` | 占比 ≥ 0.80 | 在下一个安全边界执行 `PREPARE → START → VERIFY → COMMIT → CONTINUE` |
| `urgent` | 占比 ≥ 0.90 | 同样在安全边界交接，级别与原因记为 urgent；不会打断正在进行的写入 |

`auto_handoff=false` 或交接次数已达 `max_handoffs` 时，不会发生交接；后者会把链置为 `stopped`（`handoff budget exhausted`）。

### 无法测量时的回落信号

比例只在**同时**拿到可靠的使用量与上下文容量时才计算：使用量取 `thread/tokenUsage/updated` 中 `last` 的 `inputTokens + cachedInputTokens`，容量取同一通知里的 `modelContextWindow`。两者缺一就不计算比例，**不会用任何方式伪造百分比**。

此时按可验证信号回落：

| 条件 | 判定 | 原因文本 |
| --- | --- | --- |
| 观测到压缩事件（`thread/compacted`） | `handoff` | `context compaction observed (Nx); treating the window as full without inventing a percentage` |
| 距上次检查点的轮次 ≥ `fallback_turn_limit`（默认 40） | `prepare` | `<N> turns since the last checkpoint (fallback limit 40)` |
| 其他 | `ok` | `no reliable context usage reported; relying on turn counting` |

模拟测试注入的比例在事件与报告中都带 `simulated` 标记（`source=simulated`、`simulated=1`），与真实用量记录分开统计，`relay report` 会明确写出「本链使用了模拟上下文压力」。

## 数据与目录

| 路径 | 内容 |
| --- | --- |
| `%LOCALAPPDATA%\CodexPotterRelay\relay.db` | 生产数据库（工作链、会话、事件、检查点、交接、任务、执行锁、模型请求计数） |
| `%LOCALAPPDATA%\CodexPotterRelay\relay-test.db` | 测试数据库，Level 1/2/3 与 `test-report` 专用，物理隔离 |
| `%LOCALAPPDATA%\CodexPotterRelay\relay.toml` | 配置文件 |
| `%LOCALAPPDATA%\CodexPotterRelay\logs\` | `controller.jsonl`、`test.jsonl`、`cli.jsonl`（JSON Lines 结构化日志）、`appserver-<chain_id>.log`、`manual-handoff-<chain_id>.log`、`controller-<chain_id>.out`（`--detach` 输出）、`test-real-handoff-appserver.log`、`test-real-pressure-appserver.log` |
| `%LOCALAPPDATA%\CodexPotterRelay\reports\` | `<chain_id>-<时间戳>.md` 开发报告、`test-<mode>-<时间戳>.md` 测试报告 |
| `%LOCALAPPDATA%\CodexPotterRelay\test-workspaces\` | Level 2/3 的临时测试项目（默认结束后删除） |
| `%LOCALAPPDATA%\CodexPotterRelay\chains\` | 预留给按链存放临时数据的目录，当前控制器未使用 |
| `<项目>\.relay\state.json` | 任务台账（权威来源） |
| `<项目>\.relay\HANDOFF.md` | 交接文档 |
| `<项目>\.gitignore` | 自动追加 `.relay/`，`.relay/**` 永不提交 |
| git 分支 `relay/<chain_id>` | 每条链独占的工作分支 |

环境变量：`CODEXPOTTER_RELAY_HOME` 迁移整个状态目录（测试套件用它避免污染真实安装）；`CODEX_HOME` 指定 Codex 配置目录；`CODEXPOTTER_CODEX` 指定 codex 可执行文件。

数据库实现要点：WAL 日志 + `busy_timeout`；每线程一个连接；前向迁移由 `schema_migrations` 表驱动，当前 `SCHEMA_VERSION = 1`；`events.event_key` 唯一，重复投递的 hook 事件被忽略；检查点存 SHA-256 校验和，损坏的行不会被用来恢复。

工作链状态机（非法转换直接抛错）：

```text
pending  → running | stopped | failed
running  → handoff | paused | stopped | complete | failed
handoff  → running | failed | stopped
paused   → running | stopped | failed
complete / stopped / failed  → 终态
```

## 安全与边界

- **沙箱**：默认 `workspace-write`，Codex 只能在项目工作区内写文件。可选 `read-only` / `danger-full-access`，但需要显式改配置。
- **审批**：`approval_policy = "on-request"`、`approvals_reviewer = "auto_review"`。控制器通过 `UnattendedPolicy` 应答 app-server 的服务器端请求：审批请求一律 `decline`，交互式提问以空答案返回，elicitation 同样拒绝——**不静默放宽权限**，正常工作依靠配置好的沙箱边界，而不是临时批准。
- **自主决策**：接管提示词要求会话不要向操作者提问，把决策写入 `state.json` 的 `decisions`，然后继续；遇到无法推进的情况回报 `::relay(blocked)`，链条转入 `paused` 等人工判断，而不是虚报完成。
- **只在工作链分支自动提交**：控制器把项目切到 `relay/<chain_id>`，仓库尚无提交时创建一条 `chore: initialise relay work chain`；之后的提交由会话按 `HANDOFF.md` 的规则在当前分支完成。
- **`.relay/**` 永不提交**：`ensure_scaffold()` 自动把 `.relay/` 写进项目 `.gitignore`。
- **单写者**：`execution_locks` 表保证一条链同时只有一个写者（记录 pid + hostname + 心跳）。控制器每轮刷新心跳，失去锁就停止（`lost the chain execution lock`）；同主机上进程已死的锁会被判定为陈旧并自动接管。
- **有界重试**：交接失败按 `max_handoff_retries` 有限次重试（每次换新线程）；同一会话连续 3 次 turn 失败则整条链置为 `failed`，不无限重试。
- **完成判定不信任自我声明**：`TaskValidator` 要求台账全部为 `done`/`unverifiable`、验收命令退出码为 0；没有配置验收命令或工作分支无提交时，会把对应事项列为「等待人工验收」而不是宣称完成。

## 三层测试

三层都在测试库 `relay-test.db` 与独立的临时项目里运行，正式数据不会被动到（`%LOCALAPPDATA%\CodexPotterRelay\test-workspaces\`）。

### Level 1：mock（零真实模型）

```powershell
relay test --mode mock
```

完全使用 `MockAgent` 与模拟用量，不创建 app-server，也不发起任何真实模型请求。包含四个场景与 12 项判定：

| 场景 | 判定项 |
| --- | --- |
| 正常路径（3 个任务，模拟占比 0.80） | 上下文预警逻辑产生占比与阈值判定；交接状态机走完 PREPARE/START/VERIFY/COMMIT/CONTINUE；SQLite 写入链路/会话/事件；检查点生成且校验和有效；新会话创建请求均为全新线程；新会话读取交接包并继续；原始目标完成后自动停止 |
| 回滚（新线程创建持续失败、重试上限 1） | 交接失败时保留旧检查点并回滚；交接失败后有界重试并终止 |
| 无限循环防护（任务永不完成、预算 4 turn / 2 交接） | 防止无限循环：budget 到顶即停 |
| 多项目隔离（两个项目、模拟占比 0.50） | 多项目隔离：工作链与台账互不干扰 |
| 汇总 | Level 1 未调用真实模型（真实请求数必须为 0） |

### Level 2：real-handoff（只模拟阈值，真实 A/B/C/D 会话）

```powershell
relay test --mode real-handoff
relay test-handoff --count 3     # 同一执行器，显式指定交接次数（创建 count+1 个主会话）
```

创建真实临时项目，启动真实 `codex app-server`，四个小任务分别给四个会话（默认 `add` / `sub` / `mul` / `div` + unittest），每次交接前注入 `simulated_usage_ratio`（默认 0.80），其余全部走真实路径。判定项：

- 真实创建了 `count+1` 个不同的 Codex 主会话，且 thread id 互不相同；
- 所有会话都实际执行了 turn（不是空会话）；
- 连续 `count` 次交接状态均为 `ok`；
- 新会话自动读取交接信息并继续（无需人工“继续”）；
- 全部任务完成且无重复开发；
- 验收命令通过（`python -m unittest discover -s tests -v`，使用运行测试时的解释器路径；链最终状态为 `complete`）；
- 模拟压力值已标记为 `simulated`，未混入真实用量记录。

预算：`max_turns = max(4, 任务数 + 1)`、`max_handoffs = count`、`max_model_requests = testing.max_model_requests`（默认 16），因此不会无限消耗额度。

### Level 3：real-pressure（真实上下文压力）

```powershell
relay test --mode real-pressure
```

用官方支持的 Codex 配置项把上下文压小：`model_context_window`（默认 `24000`，可用 `testing.context_window_override` 指定）与 `model_auto_compact_token_limit`（默认窗口的 75%），从而用**真实 token 用量**跨过阈值，不注入任何模拟占比、也不狂发无意义内容。判定项：取得真实 token 用量与上下文容量；真实占比达到阈值并触发交接；压缩前后事件被记录（若发生压缩）；新会话接管并继续实际开发。预算 `max_turns=6`、`max_handoffs=2`、`max_model_requests=8`。

该层消耗真实额度，只在显式执行 `relay test --mode real-pressure` 时才运行；生产控制器不会注入这套配置。

### 测试管理

```powershell
relay test-report                        # 汇总测试库最近 20 条链，模式默认 real-handoff
relay test-report --chain ch_xxx --mode mock
```

每次测试自动生成独立工作链、独立临时项目并保留日志；`--keep-workspaces` 可保留临时项目以便复盘。失败会立即停止，所有层级都受 turn / 交接 / 模型请求预算约束，不会无限重试。人工取消可直接中断进程，链上的中止记录仍留在测试库中。

## 测试结果

本项目**不在此处给出任何测试通过结论**。README 只描述如何得到结果：

1. 运行对应的测试层级，例如：

   ```powershell
   relay test --mode mock
   relay test --mode real-handoff
   relay test --mode real-pressure    # 消耗真实额度，需人工显式开启
   ```

2. 每层测试结束都会写出报告到 `%LOCALAPPDATA%\CodexPotterRelay\reports\`，文件名形如 `test-mock-20260101-120000.md`；交互式输出同时打印每一项 `[PASS]/[FAIL]` 与总结论。
3. 需要重新汇总时执行 `relay test-report --mode real-handoff`，它读取测试库、重新渲染报告，不需要重跑测试。
4. 当次的真实结果由开发者记录在 `docs/测试结果.md`。该文件**可能不存在**；若不存在，说明当次结果尚未记录，请以第 2 步新生成的报告为准，不要引用旧结论。

Level 2 与 Level 3 依赖真实 Codex 额度、真实账号登录和网络，结论会随环境变化；未记录或未运行的部分一律视为「待验证」，见 [docs/未验证与已知限制.md](docs/未验证与已知限制.md)。

## 故障恢复

场景一：控制器崩溃或被杀。

```powershell
# 1. 看链的状态、活动会话、最近检查点；执行锁的 pid/host 在 --json 输出里
relay status --chain ch_1a2b3c4d5e6f
relay status --chain ch_1a2b3c4d5e6f --json | Select-String -Pattern "owner_pid|owner_host"
relay status --chain ch_1a2b3c4d5e6f --events 30

# 2. 若链停在 running 或 handoff，且锁的持有进程已死，直接重新驱动
relay run --chain ch_1a2b3c4d5e6f
```

- 恢复时优先读 `<项目>\.relay\state.json`；如果台账缺失或损坏，就回退到**最新通过校验的检查点**，把 `HANDOFF.md` 与 `state.json` 重新写回磁盘，并记一条 `state_restored` / `ledger_repaired` 事件。
- 执行锁回收：`execution_locks` 里同主机、pid 已不存在的锁会被视为陈旧并自动替换；跨主机的锁不会被自动回收，需要用 SQL 删除该行后再 `relay run`（这属于多机写入保护，不建议绕过）。
- 崩溃发生在交接中途时，链停在 `handoff`，`active_session_id` 仍是旧会话；重新 `relay run` 会从旧会话继续并在需要时重新发起交接。失败的交接记录以 `failed` 或 `rolled_back` 保留在台账里。

场景二：人工暂停后继续。

```powershell
relay pause --chain ch_1a2b3c4d5e6f
# ……控制器在下一个安全点退出，不再发模型请求……
relay resume --chain ch_1a2b3c4d5e6f
relay run --chain ch_1a2b3c4d5e6f      # resume 只改状态，需要重新启动控制器
```

场景三：需要强制归档或放弃。

```powershell
relay checkpoint --chain ch_1a2b3c4d5e6f --reason before-experiment
relay stop --chain ch_1a2b3c4d5e6f
relay report --chain ch_1a2b3c4d5e6f
```

终态（`complete` / `stopped` / `failed`）不会再被自动执行；需要新工作请新建一条链。

## 卸载

```powershell
# 在仓库根目录执行：移除托管 hooks 块、卸载 pip 包、清理 PATH 条目
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1

# 连同数据库、日志、报告一起删除
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1 -Purge

# 只移除 hooks 配置块
relay hooks uninstall
```

参数：`-Python <path>` 指定解释器；`-Purge` 删除 `%LOCALAPPDATA%\CodexPotterRelay`（不加则保留，因为那是历史工作链的唯一记录）。

> 生成的 hooks 托管块里写着 “Remove with `relay uninstall`”，这是注释文案；当前 CLI 没有 `uninstall` 子命令，请使用 `relay hooks uninstall` 或上面的脚本。

项目侧残留：受管项目里会留下 `.relay/` 目录与 `.gitignore` 中的 `.relay/` 一行，以及 `relay/<chain_id>` 分支。是否删除由你决定，Relay 不会自动清理用户项目。

## 与上游 CodexPotter 的关系

- 上游原文保留在 [`README.upstream.md`](README.upstream.md)，许可证与版权归属不变。
- `skills/loop`、`skills/compact-kb`、`npm/` 均按上游原样保留、不做修改；`$loop` 在单个会话内部仍然可用，与 Relay 不冲突。
- Relay 是**新增的独立控制器**（Python 包 `relay/`），通过官方 `codex app-server` 的 JSON-RPC 协议驱动 Codex，不修改 Codex 私有代码，也不修改上游技能逻辑。
- 与上游共享的是理念与文件约定：`HANDOFF.md` 沿用上游的章节结构（Objective / Original User Request / Important Context / Task Ledger / Decisions / Acceptance / Rules / Done），因此一个由 Relay 接管的会话里依然可以正常使用 `$loop`。
- 差别在于：`$loop` 在会话内循环，上下文仍会累积；Relay 在会话之间接力，每次都是 `thread/start` 的全新上下文。

## 许可证

Apache-2.0，见 [LICENSE](LICENSE)。本项目基于 [breezewish/CodexPotter](https://github.com/breezewish/CodexPotter) 二次开发，上游版权归其作者所有；`skills/`、`npm/` 等上游文件的版权与许可声明保持原样。
