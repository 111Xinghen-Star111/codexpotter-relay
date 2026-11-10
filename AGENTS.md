# Repository Guidelines

## Workflow Principles

- **NEVER** use git to discard or revert any changes not made by you - they are proposely changed by user and you should live with that.

## Engineering Rules

This project requires extremely high code quality and maintainability. Best engineering practices must be followed at all times.

The rules below are some typical principles. They are not exhaustive, and you must always use your best judgment to **write the cleanest code possible**. You must always clean up and refactor immediately when you see opportunities or any violations of these engineering rules.

### Core Principles: Simplicity & Readability

- Boring Code - Obvious, self-explanatory > clever, minimize cognitive load
- Single Responsibility - One function, one job
- Explicit over Implicit - Clear is better than concise
- Meaningful Abstractions - Only when they reduce cognitive load
- Keep DRY - Only if it does not conflict with the above principles

### Better Maintainability

- Keep the public API surface minimal; prefer reusing existing methods
- Don't treat "looks similar" as "equivalent"
- Abstractions must be meaningful
- Keep certainty, single source of truth - e.g. don't introduce "optional" unless absolutely necessary
- Always add clear, concise and explicit doc comments for public APIs, complex logic, and non-obvious code

### Naming Symbols

- Choosing the name that needs the least explanation, consider: verb clarity, noun specificity, context

## CodexPotter Relay — 开发补充说明

以上是上游 CodexPotter 的仓库规范，继续有效。以下是本项目（CodexPotter Relay）特有的开发约束，
与上游规则冲突时以更严格的一条为准。

### 1. 项目结构与关键入口

```text
relay/                 Python 包（唯一实现，全部标准库）
tests/                 单元测试（unittest，零额度）
scripts/               install.ps1 / uninstall.ps1（逐用户安装，不需要管理员权限）
skills/, npm/, etc/    上游 CodexPotter 资产，保持原样
docs/                  架构、使用、安装、恢复、测试结果、未验证与已知限制
```

| 入口 | 位置 | 作用 |
| --- | --- | --- |
| `relay` 命令 | `relay/cli.py` 的 `main` | 命令行唯一入口，`build_parser` 定义子命令，`_dispatch` 分发 |
| 控制循环 | `relay/controller.py` 的 `RelayController` | 单链：预算、重连、压力采样、检查点、交接、报告 |
| 交接状态机 | `relay/handoff.py` 的 `HandoffManager` | `PREPARE → START → VERIFY → COMMIT → CONTINUE` 与回滚 |
| 插件 MCP server | `relay/mcp.py` 的 `McpServer`、`RelayToolbox` | stdio JSON-RPC；`relay_start` 用 `ops.spawn_detached_controller` 拉起控制器 |
| 插件打包 | `relay/plugin.py` 的 `render_plugin` / `install_plugin` / `plugin_status` | 渲染 marketplace、插件清单、`mcp.json`、`$relay` 技能并注册到 Codex |

### 2. 核心架构与模块职责（简明版）

四个进程角色：Codex 客户端 → 插件 MCP server（随会话生灭）→ **脱离进程的控制器** → 控制器 spawn 的
`codex app-server`（线程存储与桌面版共享 `~/.codex`）。控制器绝不寄生在插件进程里。

状态分三层：SQLite（`db.py`，权威）、项目内 `.relay/HANDOFF.md` + `.relay/state.json`（交接包）、git 快照
（`checkpoint.py`）。判定层是 `context_monitor.py`（压力）与 `validator.py`（完成），呈现层是 `report.py`。

完整分层图、模块职责表、Mermaid 时序图/状态图、数据模型、阈值算法、不变量与安全边界见
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

### 3. 构建 / 测试 / 验证命令

CI 上必须只用**零额度**命令（不接触模型、不消耗 Codex 额度）：

```powershell
python -m compileall -q relay                      # 语法/字节码编译
python -m unittest discover -s tests -q            # 单元测试（约 109 项，零额度）
relay test --mode mock                             # Level 1：MockAgent + 模拟用量，零真实请求
relay doctor                                       # Codex / git / python 体检
relay plugin status                                # 插件是否已渲染并注册到 Codex
```

- 若当前 shell 里没有 `relay`（PATH 未生效），等价写法是 `python -m relay <子命令>`。
- `--config` / `--db` / `--codex` / `--json` 是**顶层参数**，必须写在子命令之前：
  `relay --json status --chain <id>`；写成 `relay status --json` 会被 argparse 拒绝（退出码 2）。
- **不要默认运行** `relay test --mode real-handoff` 与 `relay test --mode real-pressure`：它们会创建真实
  Codex 主会话并消耗真实额度。需要冒烟真实链路时显式执行 `relay test-handoff --count 1`，并在提交说明
  或 PR 里写清消耗（模型请求次数）。
- 测试结论只认 `docs/测试结果.md` 里记录的那一次运行；不要凭「命令跑完了」就写「通过」，也不要把
  模拟通过写成真实验证。

### 4. 代码修改约束

- **只用标准库，不引入第三方依赖**：`pyproject.toml` 的 `dependencies = []` 是硬约束；新增依赖需要
  先改需求与安装文档，不能只为省几行代码。
- **Python 3.11+**：配置解析用 `tomllib`，这是硬下限；不要用 3.12+ 语法。
- 公开 API 必须有 docstring；复杂逻辑与非显然的常量同样要有说明（与上游「Always add clear, concise
  and explicit doc comments」一致）。
- 改阈值语义、交接状态机、链状态迁移时，**必须同步更新 `tests/`**（`test_context_monitor.py`、
  `test_db.py`、`test_checkpoint.py`、`test_recovery.py`、`test_lifecycle.py` 等）。
- **不得削弱安全机制**：检查点校验和（`checkpoint_checksum` / `is_intact`）、单写者锁（`execution_locks`）、
  预算（`Budget` / `_check_budgets`）、交接 `VERIFY` 的三项校验、`UnattendedPolicy` 的拒绝式应答。
- **不得为了方便通过测试而删除或跳过测试**；也不要把断言改成恒真。
- 修改 `.relay/state.json` 的结构或 `events.event_key` 语义时，先考虑兼容性：已有链的台账与数据库必须
  仍能读取（见第 7 节）。

### 5. 重要兼容性

Codex 的 `app-server` 协议字段会随版本变化（本机实测 `0.167.0-alpha.3.2`）。本项目依赖的消息名：

| 方向 | 消息 | 用途 |
| --- | --- | --- |
| 客户端→服务端 | `initialize`、`initialized` | 握手（`AppServer.start`） |
| 客户端→服务端 | `thread/start` | **唯一**的新主会话创建方式（交接 `START`） |
| 客户端→服务端 | `thread/resume` | 控制器重启后挂载已存在线程（不是交接） |
| 客户端→服务端 | `thread/read` | 读取线程快照，`VERIFY` 用它证明 `turns` 为空 |
| 客户端→服务端 | `turn/start`、`turn/interrupt` | 发起一轮 / 人工取消时中断 |
| 客户端→服务端 | `thread/name/set`、`thread/metadata/update` | 会话命名与 git 元数据（纯装饰，失败不影响链） |
| 客户端→服务端 | `hooks/list` | 回读 Codex 实际注册的 hook（诊断用） |
| 服务端→客户端 | `thread/tokenUsage/updated` | 上下文占比的唯一真实来源 |
| 服务端→客户端 | `turn/completed` | 一轮结束、最终消息与 `::relay(...)` 标记 |
| 服务端→客户端 | `item/completed` | 增量补全 agent 文本 |
| 服务端→客户端 | `thread/compacted`、`thread/compact/completed` | 压缩事件（无法测量时按「窗口已满」交接） |
| 服务端→客户端 | `turn/started`、`thread/started`、`hook/started`、`hook/completed`、`error`、`thread/error` | 事件采集（`ops.make_event_sink`） |
| 服务端→客户端请求 | `requestApproval` 类、`request_user_input`/`requestUserInput`、`elicitation/create` | 由 `UnattendedPolicy` 拒绝/空答 |

兼容性要点：

- **字段缺失即降级**：没有 `modelContextWindow` 就不算占比；`thread/start` 没有 thread id 直接报错；
  不要为了「跑通」编造默认值。
- **`[hooks]` 在 Codex 0.167 不执行**：`relay hooks install` 写入的托管块不会被注册成可执行 hook，
  状态采集的主通道是上面的通知。不要因此删掉 hooks 采集器，也不要在文档里声称 hooks 已生效
  （见 `docs/未验证与已知限制.md` 第 1 条）。
- **`thread/start` 与 `thread/resume` 不可互换**：`thread/start` 产生全新上下文（交接），
  `thread/resume` 只是把旧线程挂到新 app-server 进程（恢复），用错会破坏「每次交接都是全新会话」这一
  不变量，也会让 `VERIFY` 的 `turns` 为空的检查失去意义。
- **`safe.directory`**：`GitRepository._command` 对每个 git 命令都加 `-c safe.directory=<被管理项目>`，
  以兼容「仓库属于其他账户」的 `dubious ownership`。不要改成全局 `--global safe.directory '*'`。
- 升级 Codex 后先跑 `relay doctor` 与 `relay test --mode mock`，再按需手工冒烟真实链路。

### 6. 安全维护与扩展方式

**新增 MCP 工具**（三处同步，缺一不可）：

1. `relay/mcp.py`：在 `RelayToolbox.tools()` 里加工具描述（含 `inputSchema` 与 `annotations`），
   在 `RelayToolbox.call` 的 `handlers` 里接线，并实现对应的 `_<name>` 方法；
2. `tests/test_mcp.py`：更新 `test_all_required_tools_are_exposed` 的期望集合（否则测试会失败）；
3. `docs/USAGE.md` §1.2 的工具表（以及必要时 `README.md` 的插件小节）。

**新增 CLI 子命令**（三处同步）：

1. `relay/cli.py`：`build_parser()` 加子解析器、`_dispatch()` 加分支、实现 `_cmd_<name>`；
2. `README.md` 的「命令参考」（命令总览 + 对应小节，含参数与默认值）；
3. `docs/USAGE.md` 的高频用法段落（若该命令面向日常使用）。

**改插件内容**（清单、`mcp.json`、`SKILL.md`、版本号）：改完必须执行 `relay plugin install`，
然后**重启 Codex**。原因是 Codex 把插件**拷贝**到自己的缓存目录（`~/.codex/plugins/cache/<marketplace>`），
渲染目录里的改动不会自动同步；`relay plugin status` 会同时显示 `registered` 与 `cache_present`。
改 `PLUGIN_VERSION` 时同步 `pyproject.toml` 的 `version` 与 `relay/__init__.py` 的 `__version__`。

### 7. 避免破坏已实现功能

改动前后都要跑：

```powershell
python -m compileall -q relay
python -m unittest discover -s tests -q
relay test --mode mock
```

另外守住以下兼容性：

- **台账与数据库兼容**：`.relay/state.json` 有 `STATE_SCHEMA = 1`，`validate_state` 会拒绝未知
  schema；改结构必须升版本并在 `read_package` 侧兼容旧文件，不能让已有链突然读不出来。台账损坏时
  `latest_valid_checkpoint` 的回退路径必须仍然可用。
- **迁移只增不改**：`db.MIGRATIONS` 只能追加新版本号，已发布的语句一个字都不要改；生产库已经过
  版本 1、2，回退或修改历史迁移会让老库停在半新半旧的状态。
- **单写者不变量**：不要绕过 `acquire_lock` / `heartbeat_lock` 直接写库；不要因为「测试里只有一个进程」
  就删掉锁检查。人工命令（`relay handoff`）在控制器持锁时会主动报错，这是预期行为。
- **台账目录 `.relay/` 永不提交**：新增文件到 `.relay/` 时不要顺手 `git add`；`ensure_scaffold` 负责把 `.relay/`
  写进项目 `.gitignore`。
- **只在 `relay/<chain_id>` 分支提交**：控制器只在该分支工作，交接 `VERIFY` 会校验分支未漂移；
  新增的自动提交动作必须复用 `GitRepository` 的现有方法，不要自己拼 git 命令。
- **保留 `simulated` 标记**：任何模拟数据（压力、模型请求）都必须在事件、数据库列与报告里区分开，
  否则「真实验证」与「模拟通过」会被混为一谈。
