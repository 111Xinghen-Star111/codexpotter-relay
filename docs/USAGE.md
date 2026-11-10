# 使用指南 / Usage

本文讲**怎么用**：三种入口、首次完整流程、常用操作、一个真实场景演示、后台运行与日志、预算与阈值调优。
安装与验证见 [INSTALLATION.md](INSTALLATION.md)；故障处理见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)；
架构与配置字段的完整说明见 [README](../README.md)，模块分层与状态机见 [ARCHITECTURE.md](ARCHITECTURE.md)。

约定：`<id>` 指工作链 id（形如 `ch_1a2b3c4d5e6f`），`<你的项目路径>` 指被开发的仓库绝对路径。
所有子命令共用四个**顶层**参数，且必须写在子命令之前：`relay --config X --db Y --codex Z --json status`。
退出码：`0` 成功；`1` 运行失败、测试未通过，或链最终不是 `complete`（被 `pause`/`stop`、预算用尽等都会返回 1，这是正常的「没跑完」而不是崩溃）；`2` 配置/参数错误。

## 1. 三种入口

| 入口 | 适合 | 形态 |
| --- | --- | --- |
| 命令行 `relay` | 首次跑、脚本化、排障 | 一条命令一条链，输出在终端 |
| Codex 会话内的 MCP 工具 | 在对话里提交/查看/控制链 | 插件暴露 11 个 `relay_*` 工具 |
| `$relay` 技能 | 让 agent 按技能说明自己选工具 | 插件自带 `skills/relay/SKILL.md` |

### 1.1 命令行

`relay start` 提交一次目标并启动控制器；控制器是独立进程，不依赖任何 Codex 会话存活。其余命令都是对既有链的观察与控制。完整命令表见 [README 命令参考](../README.md#命令参考)，本文件只讲高频用法。

### 1.2 Codex 会话内（MCP 工具，共 11 个）

插件装载后，会话里可用下列工具。参数名与 `relay/mcp.py` 中的 `inputSchema` 一致，没有别的隐藏参数。

| 工具 | 参数 | 作用 |
| --- | --- | --- |
| `relay_start` | `goal`（必填）、`project`（必填，**绝对路径**）、`tasks`（字符串数组，每项 `T1:标题`）、`accept`（字符串数组，验收命令）、`max_handoffs`、`max_model_requests` | 创建链并以脱离进程方式启动控制器，立即返回 chain id、分支、控制器 pid 与日志路径 |
| `relay_status` | `chain`（可选，省略取最新一条） | 状态、目标、项目与分支、活动会话、模型请求数、停止原因、会话/交接/检查点/任务列表 |
| `relay_chains` | 无 | 列出最近 50 条链：`id / state / mode / project` |
| `relay_report` | `chain`（必填） | 写 Markdown 报告并返回路径与任务完成率摘要 |
| `relay_add_requirement` | `chain`、`text`（必填）、`tasks`（可选） | 中途追加要求：写台账 `decisions`、可追加任务、立刻写检查点 |
| `relay_checkpoint` | `chain`（必填）、`reason`（可选，缺省 `manual`） | 立刻从磁盘台账 + git 状态写检查点 |
| `relay_pause` | `chain`（必填） | 暂停：下一个安全点停止发模型请求 |
| `relay_resume` | `chain`（必填） | `paused → running`，**不启动控制器** |
| `relay_stop` | `chain`（必填） | 永久停止 |
| `relay_recover` | `chain`（可选） | 接管被中断的链并以脱离进程方式继续 |
| `relay_doctor` | 无 | 打印 Codex / git / python 体检结果 |

两点必须知道：

- `relay_start` 的 `project` 必须是绝对路径，否则工具直接返回错误而不建链。
- `relay_start` 传入的 `max_handoffs` / `max_model_requests` 会作为 `--max-handoffs` / `--max-model-requests` 交给脱离进程的控制器；不传则用 `relay.toml` 的值
  的 `[relay]` 段。要用别的预算，请用命令行 `--max-handoffs` / `--max-model-requests`，或改配置。

### 1.3 `$relay` 技能

插件目录里的 `skills/relay/SKILL.md` 会在你显式说 `$relay`、「接力开发」，或要求一个目标无人值守跑到底时被使用。
它只做一件事：告诉 agent 先 `relay_doctor`、再 `relay_start`，并且**有测试/构建命令时一定要带 `accept`**。
技能是否被你的 Codex 版本加载属于人工验收项，见 [未验证与已知限制.md](未验证与已知限制.md) 第 18 条。

## 2. 首次运行完整流程

### 第 1 步：提交目标

```powershell
relay start --goal "给 orders 模块加 GET /orders/{id} 接口，并补充接口测试" `
            --project "<你的项目路径>" `
            --task "T1:实现 GET /orders/{id} 路由与序列化" `
            --task "T2:补 tests/test_orders_detail.py" `
            --accept "python -m pytest -q"
```

- 不写 `--task` 时会生成单个任务 `T1`，标题就是目标本身。
- `--accept` 可重复，全部命令退出码为 0 才算通过验收；不写则完成判定只能靠台账 + git，报告里会列「等待人工验收」。
- 默认在**当前终端前台**运行控制器，直到链停止；加 `--detach` 见第 5 节。

预期输出（形态，数值随机器变化）：

```text
chain: ch_1a2b3c4d5e6f
project: <你的项目路径>
branch: relay/ch_1a2b3c4d5e6f
```

控制器会把工作区切到 `relay/<chain_id>` 分支（不改动你原有分支的内容），并在项目里生成 `.relay/state.json` 与
`.relay/HANDOFF.md`，同时把 `.relay/` 写进 `.gitignore`；这些台账文件永不提交。

### 第 2 步：观察

```powershell
relay status --chain <id>              # 一次性快照
relay status --chain <id> --events 20  # 附带最近 20 条事件
relay history --chain <id>             # 检查点与交接列表
```

`relay status` 的文本输出固定是这些段：`工作链 <id>`、`状态`、`目标`、`项目 (分支)`、`活动会话`、`模型请求`、`停止原因`，
然后是 `会话:`（每行 `#序号 id 状态 turns=N tokens=N ctx=窗口或 -`）、`交接:`（`#序号 阶段 状态`）、
`检查点:`（`v版本 原因 head=前 10 位`）、`任务:`（`T1: 状态 — 标题`）。
执行锁归属只在 `--json` 输出里（`lock` 字段）。

### 第 3 步：出报告

```powershell
relay report --chain <id>
```

预期：`报告已生成: %LOCALAPPDATA%\CodexPotterRelay\reports\<id>-<时间戳>.md`。报告含目标、主会话表、交接链、检查点表、任务台账、
上下文压力时间线、验收记录、hooks 证据、「真实验证 vs 模拟」小节。它每次都是从数据库重新渲染的，随时可重出。

## 3. 常用操作

| 目的 | 命令 | 关键参数 | 预期输出形态 |
| --- | --- | --- | --- |
| 列所有链 | `relay chains` | 无 | 表头 `ID STATE MODE PROJECT` + 每链一行；无链时 `没有工作链 / no chains` |
| 查看状态 | `relay status` | `--chain <id>`（省略取最新）、`--events N` | 见上一节段落结构 |
| 机器可读状态 | `relay --json status --chain <id>` | 全局 `--json` 在前 | JSON：`chain`/`sessions`/`handoffs`/`checkpoints`/`tasks`/`lock`/`model_requests` |
| 暂停 | `relay pause --chain <id>` | — | `<id>: paused`；控制器在下一个安全点退出，不再发模型请求 |
| 恢复 | `relay resume --chain <id>` | — | `<id>: running`；**只改数据库状态，不启动控制器**，还要 `relay recover --chain <id>` 或 `relay run --chain <id>` |
| 永久停止 | `relay stop --chain <id>` | — | `<id>: stopped`；终态再执行不改变状态 |
| 手动检查点 | `relay checkpoint --chain <id>` | `--reason <文本>`（默认 `manual`） | `检查点 vN 已写入（校验通过=True）` |
| 手动交接 | `relay handoff --chain <id>` | — | 成功：`交接成功: <新会话 thread id>`；失败：`交接失败: <原因>`（退出码 1） |
| 中途追加要求 | `relay require --chain <id> --text "<要求>"` | `--task T5:标题`（可重复） | `已记录新要求（检查点 vN）；下一轮或下一次会话起生效。` |
| 崩溃恢复 | `relay recover` | `--chain <id>`、`--dry-run`、三个 `--max-*` | 先列 `待恢复工作链 / chains to recover:` 与每条的 `lock=` 归属，再逐条 `--- resuming <id>` |
| 出报告 | `relay report --chain <id>` | — | `报告已生成: <路径>` |
| 历史 | `relay history --chain <id>` | `--limit N`（默认 20）、`--events` | `== <id> [状态] <项目>` + `checkpoint vN …` / `handoff #n 阶段 状态 from -> to` |
| 零额度自检 | `relay test --mode mock` | `--keep-workspaces` | 逐条 `[PASS]`/`[FAIL]` + `结论: 通过/未通过` |

关于「手动交接」的两条边界：

- 控制器正持有执行锁时它会拒绝，并提示先 `relay pause`（避免双写）。
- 它只建线程、校验、切换写权限，**不发模型请求**；链状态会从 `paused` 回到 `running`。

关于「中途追加要求」的生效时机：写入台账 `decisions` 并立刻写一个 `reason=requirement` 的检查点；
**当前会话的下一轮**或**交接后的新会话**才生效，正在跑的那一轮不会被中断注入。

## 4. 真实场景演示：给接口补测试

场景：`<你的项目路径>` 是一个 Python 项目，需要新增一个查询接口并补测试。命令全部真实可用，项目与数字仅为示例。

```powershell
# 0. 先确认环境（Codex 在不在、登录有没有、git 有没有）
relay doctor

# 1. 提交目标：两个任务 + 一个验收命令
relay start `
  --goal "为 orders 模块新增 GET /orders/{id}，返回 200/404，并补接口测试" `
  --project "<你的项目路径>" `
  --task "T1:实现 GET /orders/{id} 路由与 404 分支" `
  --task "T2:新增 tests/test_orders_detail.py 覆盖 200 与 404" `
  --accept "python -m pytest -q"
# → chain: ch_1a2b3c4d5e6f / project: <你的项目路径> / branch: relay/ch_1a2b3c4d5e6f
```

控制器会：创建链 → 切到 `relay/<id>` 分支 → 起主会话 #1 → 按 `.relay/state.json` 逐个任务推进 → 每完成一个任务写一个
`task:<id>` 检查点 → 上下文逼近阈值时交接给全新主会话 → 台账全部 `done` 后执行验收命令。

```powershell
# 2. 另开一个终端观察（前台控制器仍在跑，这是只读命令）
relay status --chain ch_1a2b3c4d5e6f
# → 会话: #1 <id> active turns=… ctx=…
# → 任务: T1: done — … / T2: pending — …

# 3. 想让它顺带把文档也改了（不打断正在跑的那一轮）
relay require --chain ch_1a2b3c4d5e6f --text "接口文档里补上 404 的返回体示例" --task "T3:更新接口文档"
# → 已记录新要求（检查点 vN）；下一轮或下一次会话起生效。

# 4. 中途想停
relay pause --chain ch_1a2b3c4d5e6f
# → ch_1a2b3c4d5e6f: paused

# 5. 回填预算后继续（resume 只改状态，recover 才真正起控制器）
relay resume --chain ch_1a2b3c4d5e6f
relay recover --chain ch_1a2b3c4d5e6f --max-turns 60

# 6. 收尾
relay report --chain ch_1a2b3c4d5e6f
# → 报告已生成: %LOCALAPPDATA%\CodexPotterRelay\reports\ch_1a2b3c4d5e6f-<时间戳>.md
```

看结果时优先看三处：报告里的「任务台账 / Task ledger」（任务是否 `done` 且 `verified`）、「验收 / Acceptance」
（每个 `--accept` 命令的退出码）、`git -C "<你的项目路径>" log --oneline relay/<id>`（实际提交）。
**不要**只看链状态是 `complete`：完成判定本身也不信任 agent 的自我声明，验收命令失败时链不会完成。

## 5. 后台运行与日志

```powershell
relay start --goal "..." --project "<你的项目路径>" --detach
# 或对已存在的链：
relay run --chain <id> --detach
```

预期输出：`controller detached, pid=<pid>, log=<日志路径>`，命令**立即返回**；`--detach` 会把 `--max-turns` / `--max-handoffs` /
`--max-model-requests` 继承给子进程。控制器是脱离进程（Windows 上 `DETACHED_PROCESS` + 新进程组），关闭终端或重启 Codex 都不会终止它。
注意：命令行 `relay recover` **不是**脱离方式——它在当前终端里逐条恢复；通过 MCP 工具 `relay_recover` 接管时才是脱离进程。

日志都在 `%LOCALAPPDATA%\CodexPotterRelay\logs\`：

| 文件 | 内容 |
| --- | --- |
| `controller-<chain_id>.out` | `--detach` 子进程的 stdout/stderr（命令行的终止输出也在这里） |
| `controller.jsonl` | 控制器结构化日志（每行一个 JSON，含 chain_id/session_id 等字段） |
| `cli.jsonl` | 命令行侧动作（手动检查点、要求追加等） |
| `appserver-<chain_id>.log` | 该链的 `codex app-server` stderr（协议握手、登录/额度类报错在这里） |
| `manual-handoff-<chain_id>.log` | `relay handoff` 单独起的 app-server 日志 |
| `test.jsonl` | `relay test` 三层测试的日志 |

## 6. 预算与阈值调优

配置在 `%LOCALAPPDATA%\CodexPotterRelay\relay.toml` 的 `[relay]` 段（字段含义见 [README](../README.md#relay-上下文阈值与循环上限)）。
命令行 `--max-turns` / `--max-handoffs` / `--max-model-requests` 只覆盖**本次运行**，不写回配置。

| 字段 | 默认 | 调优建议 |
| --- | --- | --- |
| `max_turns` | 200 | 单链最多多少轮。任务多或每轮改动小 → 调大；想早点收口 → 调到「任务数 × 2~3」 |
| `max_handoffs` | 32 | 最多交接几次。用尽即 `stopped`（`handoff budget exhausted`）。默认上下文窗口下很少用到 32（实测窗口数值见 [测试结果.md](测试结果.md)）；用缩小窗口做实验时要一起调小它来省额度 |
| `max_model_requests` | 400 | 硬上限，含每次交接后的续跑轮次。它是**防跑飞**的兜底，通常保持大于 `max_turns + max_handoffs` |
| `token_budget` | 未设置 | 整链 token 硬上限，设置时必须为正。只在按额度预算运行时设置 |
| `checkpoint_threshold` | 0.65 | 达到即写检查点。想更频繁留后路 → 调低（如 0.55） |
| `prepare_threshold` | 0.75 | 达到即写 `pressure:prepare` 检查点（不额外生成交接包，见限制文档第 5 条） |
| `context_threshold` | 0.80 | 达到即在下一个安全边界交接。想更早换会话 → 调低（如 0.70） |
| `urgent_threshold` | 0.90 | 紧急级别；动作边界与 0.80 相同，只是原因记为 urgent |
| `fallback_turn_limit` | 40 | 拿不到可靠用量时，距上次检查点多少轮进入准备级别 |
| `turn_timeout_seconds` | 7200 | 单轮等待上限，长任务要调大 |
| `max_handoff_retries` | 3 | 交接失败重试次数（最多尝试 `1 + 该值` 次，每次换新线程） |

三个硬约束（违反会被 `relay init`/加载时直接拒绝，不是警告）：

1. 四个阈值必须落在 `(0, 1]` 且**严格递增**：`checkpoint < prepare < context < urgent`（相等也报错）；
2. `max_turns`、`max_handoffs`、`max_model_requests`、`turn_timeout_seconds`、`fallback_turn_limit` 必须为正；
3. 未知段、未知键一律报错——这是为了防止拼写错误静默关掉某个安全限制。

调优的一般顺序：先固定 `--accept`（没有验收就等于没有完成标准），再用 `max_model_requests` 兜住额度，最后才动阈值。
阈值改动的效果只能用真实运行观察 `relay report` 的「上下文压力时间线」来确认；本项目不预置任何运行结论，
已有的实测记录只在 [测试结果.md](测试结果.md) 里，引用前先看它的记录时间与代码版本。

## 7. 需要记住的边界

- `relay resume` 不启动控制器；`relay resume` 后必须 `recover` 或 `run`（见限制文档第 8 条）。
- `relay status --all` 目前不改变输出；要列全部链用 `relay chains`（见限制文档第 7 条）。
- 上下文占比显示 `n/a` 不是故障，是「无法可靠测量」的回落语义（见限制文档第 6 条与 [TROUBLESHOOTING.md](TROUBLESHOOTING.md) 第 5 条）。
- 执行锁只在同主机上按 PID 判定陈旧；跨主机锁不会被自动回收（见限制文档第 9 条）。
