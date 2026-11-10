# 变更日志 / Changelog

本文件记录本项目所有值得写入历史的变化。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

只写**真实发生过**的事：条目来自 `git log --oneline` 中实际存在的提交，验证结论来自
[docs/测试结果.md](docs/测试结果.md) 记录的当次运行。当前版本以 `relay/plugin.py` 的
`PLUGIN_VERSION`、`pyproject.toml` 的 `version`、`relay/__init__.py` 的 `__version__` 三处为准。

## [Unreleased]

<!-- 尚未发布的改动写在这里；发布时把它改名为 [x.y.z] 并补上实际日期。 -->

## [0.1.0]

首次发布。仓库未打 tag，因此不填写未经确认的发布日期；条目覆盖 `0.1.0` 版本号下的全部内容。

### 新增 / Added

- **独立控制器**：`relay/controller.py` 的 `RelayController` 以单链同步循环驱动开发目标，不依赖任何 Codex
  会话存活；具备预算（turn / 交接 / 模型请求 / token）、连续 turn 失败上限、运行中取消与报告生成。
- **SQLite 持久化**：`relay/db.py` 用八张表（`chains` / `sessions` / `events` / `checkpoints` / `handoffs` /
  `task_results` / `execution_locks` / `model_requests`）记录整条链；WAL + `busy_timeout`、每线程连接、
  前向迁移（`schema_migrations`）、事件幂等键、检查点 SHA-256 校验和。
- **交接状态机**：`relay/handoff.py` 的 `PREPARE → START → VERIFY → COMMIT → CONTINUE`，每次交接都用
  `thread/start` 创建真正全新的主会话，并校验新线程 `turns` 为空、分支与 HEAD 未漂移、台账 `chain_id` 未变；
  失败有界重试，最终失败退回旧会话或把链置 `failed`。
- **上下文压力监控**：`relay/context_monitor.py` 按 `thread/tokenUsage/updated` 的真实用量计算占比并按阈值分级
  （检查点 / 准备 / 交接 / 紧急）；无法测量时退回压缩事件与轮次计数，不给任何伪造百分比。
- **MCP 工具**：`relay/mcp.py` 的 `McpServer` / `RelayToolbox` 暴露 `relay_start`、`relay_status`、
  `relay_chains`、`relay_report`、`relay_add_requirement`、`relay_checkpoint`、`relay_pause`、
  `relay_resume`、`relay_stop`、`relay_recover`、`relay_doctor`；`relay_start` 以脱离进程方式拉起控制器后立即返回。
- **插件打包**：`relay/plugin.py` 渲染本地 marketplace、插件清单、`mcp.json` 与 `$relay` 技能，并用
  `codex plugin marketplace add` / `codex plugin add` 注册；`relay plugin install|uninstall|status` 为命令行入口。
- **命令行**：`relay/cli.py` 提供 `init`、`doctor`、`start`、`run`、`chains`、`status`、`require`、`checkpoint`、
  `handoff`、`pause`、`resume`、`stop`、`history`、`report`、`test`、`test-handoff`、`test-report`、`recover`、
  `mcp`、`plugin`、`hooks`、`hook`；全局参数统一在子命令之前。
- **三层测试**：`relay/tests_runner.py` 的 Level 1（mock，零真实请求）、Level 2（真实 A/B/C/D 会话，只模拟阈值）、
  Level 3（真实 token 压力，小窗口覆盖）各自独立建链、独立临时项目、独立报告；配套 109 项单元测试
  （`tests/`，unittest，零额度）。
- **崩溃恢复**：控制器重启后 `thread/resume` 重新挂载原线程（不新建会话）；中断的交接回滚为 `failed` 并把链退回
  `running`；陈旧执行锁可回收；台账损坏时回退到最新有效检查点并同步任务表（`relay recover` / `run`）。
- **会话可见性**：每个主会话调用 `thread/name/set` 命名为 `Relay <chain 短 id> #<序号> · <任务>`，
  并用 `thread/metadata/update` 写入分支/HEAD/远端信息，使其在 Codex 会话列表中可辨识。
- **中途追加要求**：`relay require` 与 MCP 工具 `relay_add_requirement` 把新要求写入台账 `decisions`、可追加任务、
  立即写 `reason=requirement` 的检查点，从下一轮或下一次会话起生效（不强插正在跑的那一轮）。
- **Hooks 采集器**：`relay/hooks.py` 提供 `relay hook --event <name>` 采集器与 `config.toml` 托管块安装/卸载，
  并回读 Codex 实际注册的 hook；同时明确记录：本机 Codex `0.167.0-alpha.3.2` 不执行 `[hooks]`，
  状态采集主通道是 app-server 通知。
- **安装与卸载脚本**：`scripts/install.ps1` 逐用户安装（定位 Python/git、`pip install -e`、写 PATH、
  `relay init`、`relay plugin install`），`scripts/uninstall.ps1` 反向清理；两者都不需要管理员权限。
- **文档**：`README.md`（功能、安装、命令参考、配置、数据目录、安全边界）、`docs/USAGE.md`（使用指南）、
  `docs/INSTALLATION.md`（安装指南）、`docs/恢复指南.md`、`docs/测试结果.md`、`docs/未验证与已知限制.md`、
  `docs/reports/`（Level 1/2/3、生产首跑、崩溃恢复的报告副本）、`docs/ARCHITECTURE.md`（分层、进程模型、
  状态机、数据模型、不变量）、`CONTRIBUTING.md`、本文件。

### 修复 / Fixed

- **上下文占比重复计入缓存 token**：原先用 `inputTokens + cachedInputTokens` 计算占比，而 `inputTokens` 已包含
  缓存前缀，导致占比虚高到 100%。现为 `last.inputTokens + last.outputTokens`，并由单元测试锁定。
  该缺陷由真实 Level 3 测试暴露（`23dc6ca`，见 [测试结果.md](docs/测试结果.md) 第 3 节）。
- **空仓库 git 快照失败**：在还没有任何提交的仓库上读取分支/HEAD 会抛错；现在 `GitSnapshot` 能处理未出生分支
  （`has_commits=false`），`relay start` 可以在全新目录里跑（`23dc6ca`）。
- **git 身份缺失导致提交失败**：机器上没有 `user.name`/`user.email` 时提交报 "Author identity unknown"；
  现在写入**仅本仓库**的默认身份，不改用户的全局配置（`c95296c`）。
- **`safe.directory` 与外部账户仓库**：仓库属于其他账户时 git 报 `dubious ownership`，所有命令失败；
  现在每个 git 命令都带 `-c safe.directory=<被管理项目>`，例外只作用于该路径（`c95296c`）。
- **`--detach` 从未真正生效**：`relay run` 缺少预算参数，脱离出的子进程启动即 `AttributeError` 退出，链停在
  `pending`；现在 `run` 接受预算参数并由 detach 子进程继承（`f2d972d`）。
- **未跨阈值的链没有检查点**：只有压力触发才写检查点，台账损坏就会丢进度；现在链启动即写 `initial` 检查点
  （`c95296c`），并在每完成一个任务时写 `task:<id>`（`f2d972d`）。
- **恢复后任务表未同步**：从检查点恢复台账后没有把任务状态写回数据库，`task_results` 仍显示旧状态；
  现在恢复路径会调用 `_sync_tasks` 对齐（`c7deb8c`）。
- **两条被状态机拒绝的合法迁移**：`pending → paused` 与 `paused → handoff` 原先被非法迁移检查拒绝，导致新建链
  无法在控制器启动前暂停、暂停中的链无法手动交接；两条边现已放开（`b9d8ba7`）。
- **恢复时新建会话与序号 1 冲突**：线程真丢失后新建会话会撞上已有的主会话序号；现在按已有最大 `ordinal` +1
  递增，并由 `sessions_main_ordinal` 唯一索引兜底（`f2d972d`）。
- **验收命令找不到 python（exit 9009）**：控制器从 PATH 里没有 Python 的 shell 启动时，`python -m ...` 类验收
  命令直接失败；现在启动时把解释器目录与 git 目录补进子进程 `PATH`（`9d3806d`）。
- **`install.ps1` 重复添加 PATH**：Python 安装器已有 `...\Scripts\`，脚本又加了一条不带反斜杠的同名条目；
  现按「忽略大小写与尾部分隔符」去重，卸载时只删安装器自己加的那条（`b9d8ba7`）。

### 验证 / Verification

测试结论不在本文件重述，全部以 [docs/测试结果.md](docs/测试结果.md) 的记录为准：

| 验证项 | 记录位置 |
| --- | --- |
| 单元测试（unittest，零额度） | 测试结果第 0 节 |
| Level 1（mock，零真实请求） | 测试结果第 1 节 |
| Level 2（真实 A/B/C/D 会话接力） | 测试结果第 2 节 |
| Level 3（真实 token 压力触发交接） | 测试结果第 3 节 |
| 生产首跑（真实项目、未触碰主分支） | 测试结果第 4 节 |
| 真实崩溃恢复（杀进程 → `relay recover`） | 测试结果第 5 节 |
| 生命周期命令与安装/卸载脚本 | 测试结果第 6 节 |
| 会话命名与 git 元数据 | 测试结果第 7 节 |
| 从检查点恢复（台账损坏） | 测试结果第 8 节 |
| 中途追加要求 | 测试结果第 9 节 |
| Codex 插件形态（MCP + 技能） | 测试结果第 10 节 |
| 结论汇总 | 测试结果第 11 节 |

复验方法（零额度即可复跑）：

```powershell
python -m compileall -q relay
python -m unittest discover -s tests -q
relay test --mode mock
```

`relay test --mode real-handoff` 与 `relay test --mode real-pressure` 会创建真实 Codex 会话并消耗真实额度，
不在默认验证范围内。

### 已知问题 / Known issues

未验证项、环境限制与行为差异统一记录在 [docs/未验证与已知限制.md](docs/未验证与已知限制.md)，
每条都带「现象 / 原因 / 当前替代方案 / 如何验证」。`0.1.0` 时点的主要条目：

- 第 1 条：状态采集主通道是 app-server 通知；Codex `0.167` 不执行 `config.toml` 中的 `[hooks]`。
- 第 2 条：不会自动弹窗/聚焦桌面版聊天窗口（会话已命名并可在列表中出现）。
- 第 5 条：`prepare` 阈值（0.75）不单独预生成交接包，交接时现写现校验。
- 第 6 条：无法测量用量时不做百分比判断，只用压缩事件与轮次计数回落。
- 第 9 条：执行锁回收只在同主机生效，跨主机锁需人工处理。
- 第 16 条：真实交接失败后的回滚仅有 mock 场景验证。
- 第 17 条：默认 475k 窗口下的「自然长跑压力」未实测（Level 3 用小窗口取得真实占比）。
- 第 18 条：插件在真实 Codex 会话内的工具调用与 `$relay` 技能加载仍待人工验收。

同期明确「不做」或「尚未接线」的行为（同样属于已知边界）：

- 子 Agent（subagent）不作为独立会话写入台账（第 11 条）。
- `relay status --all` 参数尚未接线（第 7 条）。
- `relay resume` 只改数据库状态，不启动控制器（第 8 条）。
- `relay_start` 的 `max_handoffs` / `max_model_requests` 参数在 schema 中被接受但未传给控制器，
  预算仍取自 `relay.toml`（见 [docs/USAGE.md](docs/USAGE.md) §1.2）。
