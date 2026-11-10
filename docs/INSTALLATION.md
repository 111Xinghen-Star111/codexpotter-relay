# 安装指南 / Installation

本文只回答两件事：**怎么把 CodexPotter Relay 装到这台 Windows 机器上**，以及**装完怎么证明它真的能用**。
命令与参数以代码为准（`scripts/install.ps1`、`scripts/uninstall.ps1`、`relay/cli.py`、`relay/plugin.py`、`relay/codex_env.py`、`relay/paths.py`）。
架构、阈值含义、数据目录的完整说明见 [README](../README.md) 与 [ARCHITECTURE.md](ARCHITECTURE.md)；装完想直接开跑见 [USAGE.md](USAGE.md)；出错见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)。

## 1. 环境要求

| 项 | 要求 | 为什么 |
| --- | --- | --- |
| 操作系统 | Windows 10 / 11 | 安装、卸载脚本是 PowerShell；实测环境见 [测试结果.md](测试结果.md) 记录（Windows 11） |
| Python | 3.11+ | 配置解析使用标准库 `tomllib`，3.11 是硬下限（`pyproject.toml` 的 `requires-python`） |
| Git | Git for Windows（任意近期版本） | 检查点要读 `git status` / `git log` / 分支与 HEAD；安装器也会探测 `C:\Program Files\Git\cmd\git.exe` |
| Codex CLI | 必须包含 `app-server` 子命令 | 控制器通过 `codex app-server --listen stdio://` 驱动 Codex |
| 账号 | ChatGPT 账号已登录（`codex login status` 正常） | 未登录时 `relay doctor` 会给出 `codex_logged_in: False` |

安装**不需要管理员权限**：包、配置、数据库都在当前用户下。

## 2. 先解决「codex 在哪」

Codex 常常随桌面版安装、不在 `PATH` 上，所以 Relay 按固定顺序查找可执行文件（`relay/codex_env.py` 的 `find_codex_executable`）：

1. 显式覆盖：命令行全局参数 `--codex <path>`，或配置项 `[controller] codex_executable`（`--codex` 优先于配置文件）；
2. 环境变量 `CODEXPOTTER_CODEX`；
3. `PATH` 上的 `codex`；
4. `%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe`（多个时取体积最大的一个）；
5. `%LOCALAPPDATA%\Programs\codex\codex.exe`。

三种指定方式的写法：

```powershell
# 方式 A：一次性，用全局参数（必须写在子命令之前）
relay --codex "<codex.exe 路径>" doctor

# 方式 B：写进配置（长期生效），然后照常用 relay doctor
#   %LOCALAPPDATA%\CodexPotterRelay\relay.toml
#   [controller]
#   codex_executable = "<codex.exe 路径，正斜杠或双反斜杠>"

# 方式 C：当前会话的环境变量
$env:CODEXPOTTER_CODEX = "<codex.exe 路径>"
relay doctor
```

两个容易踩的点：

- `--codex`、`--config`、`--db`、`--json` 都是**顶层参数**，写在子命令后面会被 argparse 拒绝（退出码 2）。正确：`relay --codex X doctor`；错误：`relay doctor --codex X`。
- `relay plugin install` / `uninstall` / `status` 调用 `codex` 时**只走第 2～5 步**（插件模块里调用的是无参数的 `find_codex_executable()`），不看 `--codex` 也不看配置文件。也就是说：想让插件能注册，`codex` 必须在 `PATH` 上、在 `CODEXPOTTER_CODEX` 里，或位于上面那两个安装目录。

## 3. 安装方式一：`scripts\install.ps1`（推荐）

在**仓库根目录**执行（安装器自己按脚本位置推仓库根目录，但放在根目录最不容易出错）：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

脚本依次做这些事，任何一步失败都会停下并打印原因：

1. 定位 Python 3.11+（跳过 0 字节的 Microsoft Store 占位文件，`python` / `python3` / `%LOCALAPPDATA%\Programs\Python\Python311|312|313\python.exe` 都会试）；
2. 定位 git（`PATH`，否则 `C:\Program Files\Git\cmd\git.exe`）；
3. 用 `python -m relay doctor` 验证 Codex 可用（`-Codex` 会以 `--codex <path> doctor` 的形式传进去）；
4. `python -m pip install -e <仓库根目录>`，注册 `relay` 命令；
5. 把 Python 的 `Scripts` 目录加入**用户** `PATH`（按「忽略大小写与尾部分隔符」去重，不会重复添加）；
6. `python -m relay init` 写默认配置并迁移数据库；
7. `python -m relay plugin install` 注册 Codex 插件（可用 `-SkipPlugin` 跳过）。

参数：

| 参数 | 作用 |
| --- | --- |
| `-Python <path>` | 指定 `python.exe`，跳过自动探测 |
| `-Codex <path>` | 指定 `codex.exe`；脚本拼成 `python -m relay --codex <path> doctor` |
| `-SkipPath` | 不修改用户 `PATH`（自己保证 `relay` 可调用） |
| `-SkipPlugin` | 不安装 Codex 插件，只装命令行 |
| `-WithHooks` | 额外写入托管 hooks 配置块（默认**不写**；Codex 0.167 实测不执行 `[hooks]`，见 [未验证与已知限制.md](未验证与已知限制.md) 第 1 条） |

装完脚本会提示「开一个新终端让 PATH 生效」，并列出下一步命令。

## 4. 安装方式二：手工 pip 安装 + `relay init`

不想跑脚本时，等价的手工步骤（在仓库根目录执行）：

```powershell
python -m pip install -e .
python -m relay init
python -m relay doctor
```

- `pip install -e .` 依据 `pyproject.toml` 的 `[project.scripts]` 注册 `relay` 命令；可编辑安装便于改代码后立即生效。
- 若 `relay` 命令提示找不到，那是 Python 的 `Scripts` 目录不在 `PATH` 上：用
  `python -c "import sysconfig; print(sysconfig.get_path('scripts'))"` 查出目录后手工加入用户 `PATH`，或继续用 `python -m relay`。
- `relay init` 是**幂等**的：配置文件已存在时不会覆盖；数据库会迁移到当前 schema 并打印版本号。
- 需要插件时再执行 `relay plugin install`（见第 6 节）。

## 5. 安装方式三：不安装，只用 `python -m relay`

完全不写 `PATH`、不注册命令也能用全部命令行功能：在仓库根目录下把 `relay` 换成 `python -m relay`。

```powershell
cd <仓库根目录>
python -m relay doctor
python -m relay init
python -m relay test --mode mock
python -m relay start --goal "..." --project "<你的项目路径>"
```

- `relay/__main__.py` 直接调用 `relay.cli.main`，行为与 `relay` 脚本完全一致。
- 后台控制器由 `python -m relay run --chain ...` 拉起，因此这条路径同样支持 `--detach`。
- 唯一区别是没有 `relay` 这个短命令；如果你要装插件，插件清单里已经写死了解释器路径与 `PYTHONPATH`，`python -m relay plugin install` 一样能渲染并注册。

## 6. 安装后验证清单

按顺序逐条执行，每条给出「命令」和「应当看到什么」。任何一条不符合，先看 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)，不要继续往下装。

### 6.1 `relay doctor`

```powershell
relay doctor
```

预期：第一行 `CodexPotter Relay doctor`，之后每行一个 `  键: 值`，键包含
`codex_executable`、`codex_version`、`codex_logged_in`、`codex_login_status`、`git_executable`、`python`、`relay_time`。
**退出码 0 的条件只有两个：`codex_executable` 与 `git_executable` 都非空**。缺 Codex 时还会多一行 `codex_error`。
`codex_logged_in: False` 时命令行仍可用，但真实工作链跑不动，需先 `codex login`。

### 6.2 `relay init`

```powershell
relay init
```

预期：打印三行——`配置: <relay.toml 路径>`、`数据库: <relay.db 路径>`、`Schema: <数字>`（迁移列表当前到 2；数字随版本递增）。
默认位置是 `%LOCALAPPDATA%\CodexPotterRelay\relay.toml` 与 `%LOCALAPPDATA%\CodexPotterRelay\relay.db`。
重复执行安全；想换位置用 `relay --config <路径> init` 或设置 `CODEXPOTTER_RELAY_HOME`。

### 6.3 `relay plugin status`

```powershell
relay plugin status
```

预期输出形如（值是示例形态，不是本机实测值）：

```text
plugin id: codexpotter-relay@codexpotter-relay-local
marketplace: %LOCALAPPDATA%\CodexPotterRelay\plugin-marketplace (rendered=True)
plugin dir: %LOCALAPPDATA%\CodexPotterRelay\plugin-marketplace\plugins\codexpotter-relay
registered in Codex config: True
installed in cache: True (%USERPROFILE%\.codex\plugins\cache\codexpotter-relay-local)
```

判定依据是 `registered in Codex config: True`（命令退出码 0）。它读的是 Codex 配置目录里的 `config.toml` 是否含
`[plugins."codexpotter-relay@codexpotter-relay-local"]` 段（`CODEX_HOME` 会改变这个目录）。
`installed in cache` 反映 Codex 是否已复制插件缓存，仅供排查，不作为成功判据。

### 6.4 `codex plugin list`

```powershell
codex plugin list
```

预期：出现 marketplace `codexpotter-relay-local` 以及一行
`codexpotter-relay@codexpotter-relay-local  installed, enabled`（该形态来自 [测试结果.md](测试结果.md) 第 10 节的实测记录；
不同 Codex 版本的列样式可能不同，关键是能看到 `installed, enabled`）。

### 6.5 `relay test --mode mock`

```powershell
relay test --mode mock
```

预期：`测试模式: mock`，随后逐条 `[PASS] <判定项>` / 失败则为 `[FAIL]`，末尾 `结论: 通过`、`模型请求数: N`、`报告: ...reports\test-mock-<时间戳>.md`。
这一层**不接触真实模型**（判定项里「未调用真实模型」必须 PASS），可以在正式跑目标前放心执行。
逐项判定的历史记录见 [测试结果.md](测试结果.md) 第 1 节；本文件不复述其数字。

## 7. 启用插件（可选，但会话内使用必需）

```powershell
relay plugin install     # 渲染 marketplace + codex plugin marketplace add + codex plugin add
# 然后：完全退出并重启 Codex
# 新会话里验证：说「用 relay 看看现在有哪些工作链」，agent 应调用 relay_chains
```

- `relay plugin install` 会把插件树渲染到 `%LOCALAPPDATA%\CodexPotterRelay\plugin-marketplace\`，其中 `mcp.json` 固定了
  当前解释器与 `-m relay.mcp`，并把包根目录写进 `PYTHONPATH`，所以不需要你手改路径。
- 插件进程随 Codex 会话生灭，因此 `relay_start` 用「脱离进程」方式拉起控制器，关闭会话不会中断工作链。
- 已注册 ≠ 已加载：**必须重启 Codex**，否则会话里看不到任何 `relay_*` 工具。
- 会话内调用工具与 `$relay` 技能是否被加载，属于人工验收项，目前状态见
  [未验证与已知限制.md](未验证与已知限制.md) 第 18 条，不要在文档里把它当作已证明的能力。

## 8. 卸载

```powershell
# 在仓库根目录：卸载插件、移除托管 hooks 块、pip 卸载包、清理安装器加的 PATH 条目
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1

# 连数据库、日志、报告一起删（不可恢复）
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1 -Purge
```

- 不加 `-Purge` 时工作数据保留在 `%LOCALAPPDATA%\CodexPotterRelay`，因为那是历史工作链的唯一记录。
- 只卸插件、不动命令行与数据：`relay plugin uninstall`（执行 `codex plugin remove`、`codex plugin marketplace remove`，并删除渲染目录）。
- 只撤 hooks 配置块：`relay hooks uninstall`（会按需恢复 `config.toml.relay-backup`）。
- **卸载不会中止运行中的工作链。** 控制器是一个独立的 `python -m relay run --chain ...` 进程，不由插件或 `relay` 命令行持有；
  卸载只会删掉命令、插件和数据目录（`-Purge` 时）。已经 detach 的链会继续跑；重装后仍能用 `relay recover` 接管。
  想主动停下请用 `relay stop --chain <id>`（见 [USAGE.md](USAGE.md)）。
- 项目侧残留（`.relay/` 目录、`.gitignore` 里的 `.relay/` 一行、`relay/<chain_id>` 分支）不会被自动清理。

## 9. 常见安装失败与处理

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `Python 3.11+ not found`，而机器上明明装了 Python | `PATH` 上的 `python.exe` 是 Microsoft Store 的 0 字节占位文件，脚本会主动跳过 | 用 `-Python "<真实 python.exe 路径>"`，或 `winget install --id Python.Python.3.11 -e` 后重开终端 |
| `pip install failed` | 网络/代理/受限环境；可编辑安装需要仓库目录可写 | 手工执行 `python -m pip install -e .` 看完整报错后逐条处理 |
| `git not found` | 未安装 Git for Windows，且不在 `PATH` 也不在 `C:\Program Files\Git` | `winget install --id Git.Git -e`，或把 git 加入 `PATH` 后重跑 |
| `Codex CLI is not usable`（安装器第 3 步） | Codex 不在 `PATH`、登录失效、或版本缺 `app-server` | 先 `codex --version` 与 `codex login status` 自查；再用 `-Codex "<codex.exe 路径>"` 重跑；仍失败见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md) 第 1 条 |
| `relay plugin install` 未完成，但命令行可用 | 插件注册调用 `codex plugin ...`，而 `codex` 不在 `PATH`/`CODEXPOTTER_CODEX`/桌面版安装目录（插件不读 `--codex` 与配置） | 把 `codex.exe` 所在目录加入 `PATH` 或设置 `CODEXPOTTER_CODEX`，再 `relay plugin install` |
| 写入 hooks 后 Codex 启动异常 | 托管块让 `config.toml` 不可解析 | `relay hooks install` 会在写入后用 `codex app-server --print-startup-config` 校验并自动回滚；若仍异常，`relay hooks uninstall`，或把 `%USERPROFILE%\.codex\config.toml.relay-backup` 复制回去 |
| 装完 `relay` 命令找不到 | Python 的 `Scripts` 目录不在当前终端的 `PATH` 上 | 新开终端；或继续用 `python -m relay ...` 全功能替代 |

安装脚本本身的行为与参数就是上面这些，没有隐藏步骤。测试结论只引用 [测试结果.md](测试结果.md)，本文件不给出任何未记录的"应该通过"。

## 相关文档

- [README](../README.md) — 架构、阈值、数据目录、配置字段
- [ARCHITECTURE.md](ARCHITECTURE.md) — 模块分层、状态机与不变量
- [USAGE.md](USAGE.md) — 三种入口与常用操作
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — 13 类真实故障
- [恢复指南.md](恢复指南.md) — 崩溃、交接中断、锁冲突的恢复流程
- [未验证与已知限制.md](未验证与已知限制.md) — 哪些能力还没被验证
