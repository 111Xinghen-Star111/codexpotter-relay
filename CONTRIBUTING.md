# 贡献指南 / Contributing

本文件说明如何在本仓库里开发、验证与提交改动。架构细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，
仓库级编码规范见 [AGENTS.md](AGENTS.md)（上游规范 + 本项目补充说明）。

原则只有一条：**改动必须能用零额度的命令证明它没坏**。任何依赖真实 Codex 额度才能验证的结论，
都不属于提交前必做项，见「测试策略」。

---

## 1. 开发环境准备

| 项 | 要求 | 说明 |
| --- | --- | --- |
| 操作系统 | Windows 10 / 11 | 安装与卸载脚本是 PowerShell；其他平台的代码路径可运行但未在本仓库验证 |
| Python | 3.11+ | 配置解析使用标准库 `tomllib`，这是硬下限 |
| Git | Git for Windows | 检查点要读分支/HEAD/status；`checkpoint.find_git_executable` 会兜底找 `C:\Program Files\Git\cmd\git.exe` |
| Codex CLI | 需包含 `app-server` 子命令 | `relay doctor` 与真实链路测试需要它；`compileall`、单元测试与 `relay test --mode mock` 不调用模型，也不依赖登录态 |

```powershell
# 1. 可编辑安装（注册 relay 命令；不产生第三方依赖）
python -m pip install -e .

# 2. 自检：Codex 路径/版本/登录、git、python
relay doctor

# 补充：若 relay 不在 PATH，等价写法：
python -m relay doctor
```

**开发时建议隔离状态目录**，避免污染真实数据库与真实工作链：

```powershell
$env:CODEXPOTTER_RELAY_HOME = "<开发用临时目录>"
```

`relay/paths.py` 用这个变量解析 relay home，测试套件本身也靠它把数据写到临时位置。
`CODEX_HOME` 可指向一个临时 Codex 配置目录，用于验证 app-server 的配置项是否被接受。

---

## 2. 代码风格

- **只用标准库**：`pyproject.toml` 的 `dependencies = []` 是硬约束，不要引入第三方运行时依赖。
- **显式优于隐式**：宁可多写两行清晰代码，也不要用元编程/魔法让读者猜；不做「看起来省事但语义模糊」的抽象。
- **单一职责**：一个函数只做一件事；模块级 docstring 说明「它负责什么、不负责什么」。
- **公开 API 必须有 docstring**，复杂逻辑与非显然常量（阈值含义、重试上限、锁的陈旧判定等）也要写清理由。
- 命名以「需要最少解释」为准：动词清晰、名词具体、带上下文；避免 `data`、`info`、`handle` 这类空名。
- 保持既有风格：4 空格缩进、`from __future__ import annotations`、类型注解、行长与现有代码一致。
- 错误处理要区分「可降级」与「必须失败」：装饰性动作（会话命名、元数据、报告生成）失败只记日志；
  安全相关动作（校验和、锁、预算、`VERIFY`）失败必须中止，不允许吞掉异常。
- 不要为了让日志更好看而改动行为；也不要为了通过测试而放宽断言。

---

## 3. 分支与提交约定

分支：

- 功能/修复从不直接落在 `main` 上；一条链一个分支（Relay 自己用 `relay/<chain_id>`），人工开发建议
  用描述性分支名，例如 `feat/add-mcp-tool`、`fix/checkpoint-checksum`。
- 不要用 `git` 丢弃或回滚**别人**未提交的改动（见 `AGENTS.md` 的 Workflow Principles）。

提交信息前缀（与仓库现有历史一致）：

| 前缀 | 用途 | 例 |
| --- | --- | --- |
| `feat:` | 新功能 | `feat(relay): name relay sessions and attach git metadata` |
| `fix:` | 修缺陷 | `fix(relay): sync task rows after checkpoint restore` |
| `docs:` | 只动文档 | `docs: record verified Level 1/2/3 results` |
| `test:` | 只动测试 | `test(relay): cover cached-token double counting` |
| `chore:` | 构建、脚本、清理 | `chore: drop probe directory` |

约定：

- 一条提交只做一件事，能单独回滚；格式化/重排不要混进功能提交。
- 正文里写清**为什么改**；涉及真实额度验证时，写明消耗（模型请求次数）与报告文件名。
- 不要提交 `.relay/`、数据库、日志、报告、`*.egg-info`、渲染出的插件目录；受管项目里 `.relay/**` 永久忽略。

---

## 4. 提交前检查清单

必做（全部零额度，CI 同款）：

```powershell
python -m compileall -q relay
python -m unittest discover -s tests -q
relay test --mode mock
```

按需（改了插件/配置/环境相关代码时）：

```powershell
relay doctor            # Codex、git、python 是否就绪
relay plugin status     # 插件是否已渲染并注册（registered / cache_present）
```

勾选清单：

- [ ] 三条零额度命令全部通过；新增/修改的逻辑有对应单元测试。
- [ ] 改阈值、交接状态机、链状态迁移时，同步更新了 `tests/`（`test_context_monitor.py`、`test_db.py`、
      `test_checkpoint.py`、`test_recovery.py`、`test_lifecycle.py` 等）。
- [ ] 没有削弱检查点校验和、单写者锁、预算、`VERIFY` 校验、`UnattendedPolicy` 的拒绝式应答。
- [ ] 没有删除或跳过测试来「让它变绿」。
- [ ] 公开 API 有 docstring；新增常量/魔法数字有注释。
- [ ] 文档同步（见第 7 节）：README 命令/配置表、`docs/USAGE.md`、`docs/ARCHITECTURE.md`、
      `docs/未验证与已知限制.md` 按改动范围更新。
- [ ] 没有提交 `.relay/`、数据库、日志、报告、渲染产物；提交信息符合第 3 节的前缀约定。
- [ ] 若本次改动了行为，`CHANGELOG.md` 的 `Unreleased` 段写下变更条目。

---

## 5. 测试策略

三层测试与单元测试的分工（命令细节见 [README「三层测试」](README.md#三层测试)）：

| 层级 | 命令 | 是否消耗额度 | 用途 |
| --- | --- | --- | --- |
| 单元测试 | `python -m unittest discover -s tests -q` | 否 | 配置、数据库/迁移/状态机/锁/预算、检查点与 git、压力判定、hooks、恢复机制、生命周期、MCP/插件渲染、会话命名、中途追加要求 |
| Level 1 | `relay test --mode mock` | **否**（`MockAgent`，`real_requests=0`） | 端到端：压力→交接状态机→数据库→检查点→自动继续→回滚→预算防护→多项目隔离 |
| Level 2 | `relay test --mode real-handoff` | **是**（真实会话，每次交接一轮真实请求） | 真实 A/B/C/D 会话接力，只模拟阈值 |
| Level 3 | `relay test --mode real-pressure` | **是**（真实 token 压力） | 用小的 `model_context_window` 让真实占比越过阈值 |

规则：

- **零额度优先**：能写成单元测试的逻辑就写成单元测试；能 mock 证明的就不要花额度。
- `real-handoff` / `real-pressure` **默认不跑**。需要真实链路冒烟时用 `relay test-handoff --count 1`，
  并在提交说明里写明消耗；不要把它塞进 CI，也不要让它成为「合并前必须通过」的门槛。
- 真实测试结论只引用 `docs/测试结果.md` 里记录的当次运行（含时间、thread id、模型请求次数、报告路径），
  不要把一次通过当成永远通过。
- 需要复跑报告时用 `relay test-report --mode <level>`，它只重新渲染测试库，不需要重跑测试。
- 三层测试一律写入测试库 `relay-test.db` 与独立临时项目（`test-workspaces/`）；`--keep-workspaces` 才会保留项目。

---

## 6. 如何新增功能

### 6.1 新增 MCP 工具（三处同步）

1. `relay/mcp.py`：`RelayToolbox.tools()` 增加工具描述（`_tool(...)`，含 `inputSchema` 与
   `readOnlyHint` / `destructiveHint` 注解）；`RelayToolbox.call` 的 `handlers` 接线；
   实现对应的 `_<name>` 方法，失败返回 `ToolResult(..., is_error=True)` 而不是抛异常。
2. `tests/test_mcp.py`：更新 `test_all_required_tools_are_exposed` 的期望集合；为参数校验和错误路径补用例。
3. `docs/USAGE.md` §1.2 的工具表（参数要与 `inputSchema` 一致）；若属于插件对外能力，同步
   `README.md` 的插件小节。

约束：`relay_start` 这类动作型工具必须保持「立即返回」——控制器用
`ops.spawn_detached_controller` 以脱离进程方式启动，绝不能在 MCP server 进程里同步跑链。

### 6.2 新增 CLI 子命令（三处同步）

1. `relay/cli.py`：`build_parser()` 加子解析器（参数尽量加 `help`）、`_dispatch()` 加分支、
   实现 `_cmd_<name>`；全局参数（`--config` / `--db` / `--codex` / `--json`）只在顶层定义。
2. `README.md` 的「命令参考」：命令总览 + 对应小节（参数、默认值、说明）。
3. `docs/USAGE.md`：若面向日常使用，补一段高频用法。

约定：退出码保持 `0` 成功 / `1` 运行失败或测试未通过 / `2` 配置或参数错误；
`--json` 输出必须与文本输出表达同一份信息；幂等命令（如 `stop` 对终态链）重复执行不得报错。

### 6.3 新增配置项（四处同步）

1. `relay/config.py`：加到对应 dataclass（`RelaySettings` / `TestingSettings` / `ControllerSettings`），
   在 `validate()` 里校验取值范围与相互约束。
2. 同一文件的 `DEFAULT_CONFIG_TEMPLATE`：写进默认模板并加一行注释说明用途。
3. `tests/test_config.py`：覆盖合法值、非法值、未知键拒绝。
4. 文档：`README.md` 的配置表（默认值 + 作用 + 校验规则）；影响阈值语义时同步
   `docs/ARCHITECTURE.md` 第 5 节。

注意：未知键会被拒绝（`_build_section` 检查 `unknown`），因此新增字段必须显式声明，不能靠默认字典兜底。

---

## 7. 文档同步要求

| 改动 | 必须同步的文档 |
| --- | --- |
| 新增/修改 CLI 子命令或参数 | `README.md`「命令参考」、`docs/USAGE.md` |
| 新增/修改 MCP 工具 | `docs/USAGE.md` §1.2、`README.md` 插件小节、`tests/test_mcp.py` 期望集合 |
| 新增/修改配置项或阈值语义 | `README.md` 配置表、`docs/ARCHITECTURE.md` 第 5 节 |
| 改进程/连接模型、状态机、数据表 | `docs/ARCHITECTURE.md`（分层表、时序图或状态图、数据模型） |
| 改插件清单/技能/版本号 | `relay/plugin.py`、`pyproject.toml`、`relay/__init__.py`，然后 `relay plugin install` |
| 新增已解决/未解决的限制 | `docs/未验证与已知限制.md`（按「现象/原因/当前替代方案/如何验证」四段写），必要时 `docs/测试结果.md` |
| 面向用户的可见行为变化 | `CHANGELOG.md` 的 `Unreleased` 段 |

文档里的路径一律用占位符（`<项目路径>`、`%LOCALAPPDATA%\CodexPotterRelay\...`），
不要写具体机器上的个人绝对路径。测试结论必须能指到 `docs/测试结果.md` 的章节或 `docs/reports/` 里的文件。

---

## 8. 报告问题的模板

```markdown
## 现象
- 期望：
- 实际：
- 影响范围（哪条链 / 哪个命令 / 哪个入口：CLI、MCP 工具、插件技能）：

## 环境
- 操作系统：
- Python 版本（`python --version`）：
- Codex 版本（`relay doctor` 的 `codex_version`）与登录状态：
- Relay 版本（`pyproject.toml` / `PLUGIN_VERSION`）与代码分支：
- 是否设置过 `CODEXPOTTER_RELAY_HOME` / `CODEX_HOME` / `CODEXPOTTER_CODEX`：
- 配置里的关键项（`[relay]` 阈值、`[controller]` sandbox/审批、`[testing]` 覆盖项）：

## 复现
1. 完整命令（含全局参数位置，例如 `relay --json status --chain <id>`）：
2. 可复现的最小项目（临时目录即可；不要附带私有代码）：
3. 复现次数 / 是否稳定：

## 日志与证据
- `relay --json status --chain <id>` 输出：
- `relay history --chain <id> --events` 输出：
- 控制器日志：`%LOCALAPPDATA%\CodexPotterRelay\logs\controller.jsonl`
- 控制器 stdout（`--detach` 时）：`%LOCALAPPDATA%\CodexPotterRelay\logs\controller-<chain_id>.out`
- app-server 日志：`%LOCALAPPDATA%\CodexPotterRelay\logs\appserver-<chain_id>.log`
- 报告：`%LOCALAPPDATA%\CodexPotterRelay\reports\<chain_id>-<时间戳>.md`
- 项目侧台账：`<项目路径>\.relay\state.json` 与 `HANDOFF.md`（可脱敏后附上）
- 若与交接相关，附 `handoffs` 的 `sequence / phase / state / error`：
```

提交问题时请尽量说明「是否消耗了真实额度」「是否只在 mock 下复现」，这两点决定了问题该记进
`docs/测试结果.md` 还是 `docs/未验证与已知限制.md`。
