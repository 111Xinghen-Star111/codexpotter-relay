# 故障排查 / Troubleshooting

本文收录 13 类**真实出现过或被代码显式处理**的故障，每条按「现象 / 原因 / 处理 / 验证」四段写，
只描述代码里确实存在的分支与报错文本。测试结论只引用 [测试结果.md](测试结果.md)；
未验证的能力见 [未验证与已知限制.md](未验证与已知限制.md)；崩溃与锁的完整流程见 [恢复指南.md](恢复指南.md)；
模块分层与状态机见 [ARCHITECTURE.md](ARCHITECTURE.md)。

排查顺序：`relay status --chain <id>` → `relay status --chain <id> --events 30` → `relay history --chain <id> --events`
→ `%LOCALAPPDATA%\CodexPotterRelay\logs\` 里的 JSON Lines 与 app-server 日志。

## 1. 验收命令报 exit 9009（`codex` 或 `python` 不在 PATH）

**现象** — 任务全是 `done` 但链不完成，报告「验收 / Acceptance」段某条命令显示 `-> exit 9009`；
`validation` 事件里 `acceptance[].returncode = 9009`。

**原因** — Windows 用 `9009` 表示找不到可执行文件。验收命令由 `TaskValidator` 在项目目录用 `shell=True` 执行，子进程继承控制器的
`PATH`；控制器启动时补自己的解释器目录（`codex_env.ensure_python_on_path`），启动 app-server 前补 git 目录
（`checkpoint.ensure_git_on_path`），**不补** `node`、`npm`、venv 的 `Scripts`。早期实测就是 `python` 不在 `PATH`（[测试结果.md](测试结果.md) 第 7 节），修复后该场景应消失；
仍出现多半是命令名不是 `python`，或控制器用另一个解释器启动。

**处理** — 验收命令写绝对路径最省事：`--accept "\"<python 路径>\" -m pytest -q"`；或在启动控制器前把需要的目录放进当前终端
`PATH`（`$env:Path = "<目录>;$env:Path"`）。已开跑的链可以改台账 `.relay/state.json` 的 `acceptance.commands`，再
`relay checkpoint --chain <id> --reason fix-acceptance`；也可以用 `relay require --text "<新的验收方式>"` 交给会话。

**验证** — 手工在项目目录跑同一条命令确认退出码 0；再看最新 `validation` 事件的 `returncode` 与报告的验收段。

## 2. 插件装好了，Codex 会话里看不到 `relay_*` 工具

**现象** — `codex plugin list` 里有 `codexpotter-relay@codexpotter-relay-local  installed, enabled`，但会话里让 agent
「用 relay 看看有哪些工作链」时没有可调用工具。

**原因** — Codex 只在启动时加载插件及其 MCP server，安装动作不会热加载已在运行的会话。

**处理** — 完全退出 Codex（含托盘后台进程）再重新打开，新建会话。仍不可见时检查 `relay plugin status` 的
`registered`（否则见第 3 条）与 `%USERPROFILE%\.codex\config.toml` 里的
`[plugins."codexpotter-relay@codexpotter-relay-local"]` 段。

**验证** — 新会话让它调用 `relay_chains`，返回 `id | state | mode | project` 列表即通过。注意：会话内实际调用与
`$relay` 技能是否被加载**仍是人工验收项**（[未验证与已知限制.md](未验证与已知限制.md) 第 18 条）；MCP 协议层本身由
`tests/test_mcp.py` 覆盖，但这不等于「某个 Codex 版本一定会调用它」。

## 3. `relay plugin status` 显示 `registered: False`

**现象** — 输出最后两行是 `registered in Codex config: False`，命令**退出码 1**；可能 `rendered=True`（插件树已渲染）却没有注册段。

**原因** — 注册由 `codex plugin marketplace add` + `codex plugin add codexpotter-relay@codexpotter-relay-local` 完成，
`plugin_status()` 只认 Codex 配置里的那一段。最常见原因是 `codex` 命令找不到：插件模块调用**无参数**的
`find_codex_executable()`，**不读** `--codex`，也**不读** `relay.toml` 的 `controller.codex_executable`。

**处理** — ① `relay --json plugin install`，读 `steps` 里每步的 `returncode` 与 `output`（唯一能看到 Codex 原始报错的地方）；
② 让 `codex` 在 `PATH` 上，或设 `CODEXPOTTER_CODEX`，或把它放进 `%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe` /
`%LOCALAPPDATA%\Programs\codex\codex.exe`；③ `codex plugin marketplace list` 应含 `codexpotter-relay-local`；
④ 重跑 `relay plugin install`（渲染幂等，不会写坏已渲染文件）。

**验证** — `relay plugin status` 退出码 0 且 `registered in Codex config: True`，`codex plugin list` 见 `installed, enabled`，
再按第 2 条验证工具可见。

## 4. 交接失败或回滚（`handoffs.state` 为 `failed` / `rolled_back`）

**现象** — `relay status` 的 `交接:` 段出现 `#1 START failed` / `#1 COMMIT rolled_back`；`relay report` 的交接链 `Error` 列有原因；
链可能仍 `running`（回滚成功）或已 `failed`（回滚也失败）。手工 `relay handoff` 失败时退出码 1 并打印 `交接失败: <原因>`。

**原因** — `PREPARE → START → VERIFY → COMMIT → CONTINUE` 任一步失败都走同一条收尾路径：原会话仍可用就把链退回原会话
（`handoffs.state=rolled_back`），回滚也不可行时链转 `failed`（`state=failed`）。常见具体原因：`VERIFY` 发现新线程已有 turn（不是干净会话）；
分支或 HEAD 变了（`another process is writing to the project`）；`PREPARE` 的检查点校验和不通过或台账 `chain_id` 不匹配；
`thread/start` 连续失败（app-server 异常、额度、网络）。重试由 `max_handoff_retries`（默认 3）决定，最多尝试 4 次，每次换新线程。

**处理** — ① `relay history --chain <id> --events` 看该序号的 `phase`/`state`/`error`，先定位阶段；② 确认没有第二个进程在写同一项目
（见第 7 条）；③ 回滚成功（链仍 `running`、活动会话没变）：PREPARE 的检查点保留，让控制器重试或手工 `relay handoff --chain <id>`；
④ 链已 `failed`：修根因后 `relay resume` 或 `relay recover --chain <id>`；终态链不会再自动执行，必要时新建一条链。

**验证** — 成功后 `relay status` 出现 `#n CONTINUE ok` 且活动会话切到新 thread id；`relay report` 交接链表的
`Attempts`/`Simulated`/`Error` 能对上。真实环境制造交接失败成本很高，回滚路径目前只有 mock 验证
（[未验证与已知限制.md](未验证与已知限制.md) 第 16 条）。

## 5. 上下文占比显示 `n/a`

**现象** — 报告「上下文压力时间线」的 `Ratio` 为 `n/a`、`Source` 为 `unavailable`；`relay status` 会话行末尾 `ctx=-`；
会话仍在正常推进。

**原因** — 这是**回落语义，不是故障**。比例只在同时拿到可靠使用量与容量时才计算
（`thread/tokenUsage/updated` 的 `last.inputTokens + last.outputTokens` 与 `modelContextWindow`）；缺任一项就返回
`ratio=None`、`source=unavailable`，四个百分比阈值全部不参与判断，改按可验证信号回落：观测到压缩事件即按「窗口已满」交接
（原因文本写明不编造百分比）；距上次检查点达 `fallback_turn_limit`（默认 40）轮进入准备级别；否则维持 `ok`。
代码里没有任何估算或补零路径。

**处理** — 不要「修」它。先确认回落在工作：`relay history --chain <id> --events` 里 `context_pressure` 的 `source` 与 `level`，
以及该链是否有 `thread/compacted` 事件（压缩次数 > 0 会直接触发交接）。需要确切百分比做容量实验时，用
`[testing] context_window_override` 缩小窗口再观察（该开关只用于测试链）。

**验证** — 报告同一行的 `Turn`、`Compactions` 两列仍有值（采样在工作），仅 `Ratio` 为 `n/a`。详见
[未验证与已知限制.md](未验证与已知限制.md) 第 6 条。

## 6. 工作链停在 `running` 且没有任何进展

**现象** — `relay status` 显示 `状态: running`，但 `模型请求` 计数长时间不变、事件不再增长，终端也没有控制器输出。

**原因** — 控制器进程已退出（被关掉的终端带走、`taskkill`、休眠后被杀、app-server 崩溃后未恢复），而数据库里的链状态仍停在
`running`/`handoff`。链状态是「最后一个写者留下的意图」，不是「进程还活着」的证据。

**处理**

```powershell
relay recover --dry-run          # 先看哪些链要恢复、该链的 lock= 是 free 还是归某个 pid@host
relay recover --chain <id>       # 只恢复这一条
relay recover                    # 或逐条恢复全部 running/handoff 链
```

恢复会回收同主机上已死进程的锁、校验台账（损坏则回退到最新有效检查点）、把中断的交接标记为 `failed` 并退回 `running`，
然后继续执行；要换预算用 `relay recover --chain <id> --max-turns 60`。

**验证** — `relay status` 的 `模型请求` 开始增长、`relay history --events` 出现新的 `turn_completed`；必要时看
`logs\controller-<id>.out`。真实崩溃恢复演练结果见 [测试结果.md](测试结果.md) 第 5 节。

## 7. 执行锁被占用（`active writer`）

**现象** — 启动控制器报 `chain <id> already has a live writer: {...}`；手工交接报
`the controller currently owns this chain; use 'relay pause' first or let it hand off automatically`；
`relay recover` 拒绝接管某条链。`relay --json status --chain <id>` 的 `lock` 字段能看到 `owner_pid`/`owner_host`。

**原因** — `execution_locks` 保证一条链同时只有一个写者（pid + hostname + 心跳）。判定规则：同主机且该 pid 已不存在 → 陈旧、可接管；
**不同主机一律视为可能存活**（防止两台机器同时写同一项目）。因此「占用」有两种原因：真有控制器在跑，或锁被另一台机器的记录挡住。

**处理** — ① 同主机：`Get-Process -Id <pid>` 确认身份。是 relay 控制器 → 让它跑完，或 `relay pause --chain <id>` 等在安全点停；
已死 → `relay recover --chain <id>`（自动回收陈旧锁）；② 跨主机：确认那台机器的控制器已停后再删 `execution_locks` 对应行
（删前务必确认没有第二台机器写同一项目，见 [未验证与已知限制.md](未验证与已知限制.md) 第 9 条）；
③ 要手工交接先 `relay pause`，暂停链上的手动交接是被允许的（[测试结果.md](测试结果.md) 第 6.2 节）。

**验证** — `relay recover --dry-run` 里该链的 `lock=` 变成 `free`，随后控制器能启动并开始计数。

## 8. 台账 `state.json` 损坏

**现象** — 日志出现 `handoff package unreadable` 或 `ledger unreadable after turn`；
`<你的项目路径>\.relay\state.json` 不是合法 JSON；链继续运行，但报告里的任务状态「倒退」了。

**原因** — `.relay/state.json` 是权威台账，会话/编辑器/手工改错都可能写坏它。控制器不会在损坏状态下推进，而是回退到数据库里
**校验和仍然有效的最新检查点**，把 `HANDOFF.md` 与 `state.json` 写回磁盘，并记 `state_restored`（启动时）或
`ledger_repaired`（一轮之后）事件，然后**按检查点里的状态**继续。

**处理** — 通常无需干预，这是设计好的自愈路径。检查点之后完成但还没写进检查点的任务会退回 `pending` 并重做
（宁可重做一小步，也不基于可疑状态继续）；想减少回退损失就写得更勤：降低 `checkpoint_threshold`，或关键节点手工
`relay checkpoint --chain <id> --reason before-risky-edit`。

**验证** — `relay history --chain <id> --events` 找到 `state_restored`/`ledger_repaired`；`relay status` 的任务状态与数据库一致；
`Get-Content "<你的项目路径>\.relay\state.json" -TotalCount 5` 已是合法 JSON。真实复现（回退到 `task:T1` 检查点、0 次模型请求、
数据库同步回退）见 [测试结果.md](测试结果.md) 第 8 节。

## 9. 验收命令一直失败，链始终不完成

**现象** — 台账任务全是 `done` 但链不是 `complete`；报告验收段反复出现同一个 `failure`；每轮都在改同一处代码却没有结论。

**原因** — 完成判定不信任自我声明：`TaskValidator` 要求台账全部 `done`/`unverifiable`、每条验收命令退出码为 0
（工作分支没有提交时还会额外列一条「等待人工验收」）。任一不满足则 `ValidationReport.complete=False`，控制器把它记进内存里的
`_last_validation`，**下一轮提示词就变成修复提示**（`build_repair_prompt`：列出失败项、未完成任务、命令输出尾部，要求「修根因、
不许削弱检查」）。注意 `_last_validation` 只存在于当前控制器进程内，重启控制器会重新生成。

**处理** — ① 先读失败本身：报告验收段给每条命令的退出码，需要完整输出就手工在项目目录跑同一条命令；
② 分辨是**代码没修好**还是**验收命令本身有问题**（路径错、缺依赖、指向不存在的脚本）。前者交给会话继续，后者必须改命令：
编辑 `.relay/state.json` 的 `acceptance.commands` 后 `relay checkpoint --chain <id> --reason fix-acceptance`；
③ 想给更明确的指令：`relay require --chain <id> --text "验收失败是因为 X，请按 Y 修"`；
④ 反复无解别硬耗额度：`relay pause --chain <id>`，人工修好后 `relay resume` + `relay recover`。

**验证** — `logs\controller.jsonl` 里每轮有带 `command`/`returncode`/`duration` 的 `acceptance command finished` 记录；
修好后最新 `validation` 事件 `complete=true`，链转 `complete`。

## 10. git 报 `dubious ownership`（仓库属于其他账户）

**现象** — 控制器启动或写检查点失败，错误信息含 `detected dubious ownership in repository`；手工 `git status` 也报同样的错。

**原因** — Windows 上项目目录可能属于另一个账户（沙箱工具、另一个用户、一次以管理员身份运行的操作都会造成），git 默认拒绝操作。
Relay 对**受管项目**的处理是每次 git 调用都带 `-c safe.directory=<项目路径>`（集中在 `checkpoint.py` 一处，其他仓库不受影响），
这也是 [测试结果.md](测试结果.md) 第 4 节记录的真实缺陷之一。

**处理** — Relay 侧无需配置。你自己的 `git` 报错时，只给这一个仓库加白名单：

```powershell
git config --global --add safe.directory "<你的项目路径>"
```

不要用 `safe.directory = *` 图省事，那等于关掉这项保护。

**验证** — `git -C "<你的项目路径>" status` 正常；`relay checkpoint --chain <id>` 打印 `校验通过=True`；
`relay history --chain <id>` 里检查点 `verified=yes`。

## 11. 无 git 身份导致提交失败

**现象** — 会话里 `git commit` 报 `Author identity unknown` / `unable to auto-detect email address`；或链跑完后分支上没有提交，
报告出现「the work branch has no commits, so code changes cannot be verified from git」。

**原因** — 这台机器（或该账户）没配 `user.name`/`user.email`。控制器会兜底：`GitRepository.ensure_identity()` 缺失时写入一条
**仅本地**（仓库级）的默认身份，不改全局 git 配置；全新目录里还会 `git init --initial-branch=main` 并提交
`chore: initialise relay work chain`。

**处理** — 想用自己的身份：`git config --global user.name "<名字>"` 与 `git config --global user.email "<邮箱>"` 后继续；
不在意作者：无需处理，仓库级默认身份足够让提交成功；已经产生失败轮次时，修好身份后 `relay recover --chain <id>` 继续，不要重建链。

**验证** — `git -C "<你的项目路径>" config user.name` 有输出；`git -C "<你的项目路径>" log --oneline relay/<id>` 能看到提交；
报告任务台账里 `Verified=yes`（有 evidence 的任务才算 verified）。

## 12. `[hooks]` 配置写了却不生效

**现象** — `relay hooks install` 写入托管块成功，但 `relay hooks status` 里 `实际观测到的 hook 事件: 无`；`events` 表没有
`source='hook'` 记录；`hooks/list` 返回空。

**原因** — 本机实测的 Codex `0.167.0-alpha.3.2` **不执行用户配置里的 `[hooks]` 段**（信任/注册机制未对该版本开放），
传 `--dangerously-bypass-hook-trust` 也会被拒绝。这是环境限制，不是配置写法问题。状态采集的主通道是 **app-server 通知**：
`turn/started`、`turn/completed`、`thread/started`、`thread/compacted`、`thread/tokenUsage/updated`、`hook/*`、`error` 等由
`make_event_sink()` 写入 `events`（`source='appserver'`）；`relay hook --event ...` 采集器保留，等 hooks 可用时自动生效。

**处理** — 不要反复改 `config.toml`。默认安装本就不写 hooks 块（只有 `install.ps1 -WithHooks` 才写）；要清理用
`relay hooks uninstall`（会按需恢复 `config.toml.relay-backup`）。判断状态采集是否正常请看事件与通知，不看 hooks 是否「注册成功」。

**验证** — `relay hooks status` 区分「已写入」与「实际观测到」；`relay history --chain <id> --events` 有
`turn_completed`/`context_pressure`/`validation` 即采集正常；报告「Hooks 采集」段把两个通道分开统计并说明改由通知通道完成。
完整记录见 [未验证与已知限制.md](未验证与已知限制.md) 第 1 条。

## 13. 模型额度耗尽 / `usageLimitExceeded`

**现象** — 会话轮次连续失败，日志与 app-server 输出出现额度类错误（如 `usageLimitExceeded`、限流提示）；链可能以 `failed` 结束
（原因形如 `3 consecutive turn failures: ...`），也可能停在 `running` 而不再有请求。

**原因** — 本仓库**没有针对额度耗尽的专门分支**：额度错误以普通 turn 失败出现（`SessionError`/`AppServerError`），
同一会话连续 3 次失败即把链置为 `failed`（`MAX_TURN_FAILURES`），不无限重试；额度恢复后也不会自己「醒来」。

**处理** — ① 先确认是额度而不是别的错误：读 `%LOCALAPPDATA%\CodexPotterRelay\logs\appserver-<chain_id>.log` 尾部，以及
`relay history --chain <id> --events` 里失败轮次附近的 `error` 事件；② `codex login status` 确认账号有效（`relay doctor` 也报
`codex_logged_in`）；③ 额度恢复后把链拉回来：`running` 但无进程 → `relay recover --chain <id>`；已 `failed` → 修根因后按状态处理，
必要时用同一目标新建一条链（历史仍在数据库与报告里）；④ 避免再撞：把 `max_model_requests` 设成额度预算内的值（硬上限，用尽即
`stopped`），并把消耗额度的 `relay test --mode real-handoff` / `real-pressure` 安排在有余量时。

**验证** — `relay status --chain <id>` 的 `模型请求` 重新增长、链回到 `running` 并最终 `complete`/`stopped`；报告会话表出现新轮次。
额度相关的既有记录与边界见 [恢复指南.md](恢复指南.md) 第 3 节。

## 附：三条通用自查

```powershell
relay doctor                          # Codex / git / python 是否就绪、账号是否登录
relay --json status --chain <id>      # 含执行锁归属的机器可读快照
relay history --chain <id> --events   # 检查点、交接与事件的完整时间线
```

`%LOCALAPPDATA%\CodexPotterRelay\logs\` 里的 `controller.jsonl`、`cli.jsonl`、`appserver-<chain_id>.log` 是唯一能证明
「到底发生了什么」的原始材料；报告是给人看的结果，不是排查证据。
