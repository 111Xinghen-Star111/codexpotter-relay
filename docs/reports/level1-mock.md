# 测试报告 / Test Report — mock

- 生成时间 / Generated: 2026-11-10 12:42:00
- 工作链 / Chains: `ch_750129497034`, `ch_241cd3593419`, `ch_c8e113308ff3`, `ch_ffdc7a05d979`, `ch_4fde9fcd2724`

## 结论 / Verdict

- Level 1 完全使用模拟会话与模拟用量：真实模型请求 0 次，模拟请求 9 次。
- [PASS] 上下文预警逻辑产生真实占比与阈值判定 (pressure thresholds) — levels=['handoff']
- [PASS] 交接状态机走完 PREPARE/START/VERIFY/COMMIT/CONTINUE — handoffs=[(1, 'CONTINUE', 'ok'), (2, 'CONTINUE', 'ok')]
- [PASS] SQLite 写入链路/会话/事件 (database writes) — sessions=3
- [PASS] 检查点生成且校验和有效 (checkpoint integrity) — checkpoints=[2, 1]
- [PASS] 新会话创建请求均为全新线程 (new session requests) — threads=['mock-thread-7ed067-001', 'mock-thread-7ed067-002', 'mock-thread-7ed067-003']
- [PASS] 新会话读取交接包并继续 (automatic resume) — turns=3 state=complete
- [PASS] 原始目标完成后自动停止 (auto stop on completion) — state=complete reason=tasks complete, waiting for human acceptance
- [PASS] 交接失败时保留旧检查点并回滚 (rollback keeps checkpoint) — checkpoints=[1]
- [PASS] 交接失败后有界重试并终止 (bounded retry) — state=failed handoffs=[('failed', 2)]
- [PASS] 防止无限循环：budget 到顶即停 (no infinite loop) — turns=3 handoffs=2 state=stopped
- [PASS] 多项目隔离：工作链与台账互不干扰 (project isolation) — chains=['ch_ffdc7a05d979', 'ch_4fde9fcd2724']
- [PASS] Level 1 未调用真实模型 (no real model call) — real_requests=0, simulated_requests=9

# 开发报告 / Development Report — `ch_750129497034`

- 生成时间 / Generated: 2026-11-10 12:42:00
- 项目 / Project: `%LOCALAPPDATA%\CodexPotterRelay\test-workspaces\mock-happy-1794285709`
- 工作分支 / Work branch: `relay/ch_750129497034`
- 模式 / Mode: test (test level: mock)
- 状态 / State: **complete**
- 停止原因 / Stop reason: tasks complete, waiting for human acceptance
- 创建 / Created: 2026-11-10 12:41:50
- 完成 / Completed: 2026-11-10 12:41:52
- 模型请求数 / Model requests: 3
- 主会话数 / Main sessions: 3
- 交接次数 / Handoffs: 2
- 模拟压力 / Simulated pressure: YES

## 目标 / Objective

```text
mock happy path
```

## 主会话 / Main sessions (A, B, C, ... )

| # | Thread id | State | Turns | Input tok | Cached | Total tok | Context win |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `mock-thread-7ed067-001` | active | 1 | 6000 | 0 | 6512 | 20000 |
| 2 | `mock-thread-7ed067-002` | handed_off | 1 | 12000 | 0 | 13024 | 20000 |
| 3 | `mock-thread-7ed067-003` | active | 1 | 18000 | 0 | 19536 | 20000 |

## 交接链 / Handoff chain

| Seq | From | To | Phase | State | Attempts | Simulated | Error |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `-` | `mock-thread-7ed067-002` | CONTINUE | ok | 1 | YES |  |
| 2 | `mock-thread-7ed067-002` | `mock-thread-7ed067-003` | CONTINUE | ok | 1 | YES |  |

## 检查点 / Checkpoints

| Version | Reason | Git head | Branch | Dirty | Session | Verified |
| --- | --- | --- | --- | --- | --- | --- |
| 2 | handoff | `e304d99f8f` | `relay/ch_750129497034` | no | `mock-thread-7ed067-002` | yes |
| 1 | handoff | `666743538b` | `relay/ch_750129497034` | no | `-` | yes |

## 任务台账 / Task ledger

| Task | Title | Status | Verified | Evidence |
| --- | --- | --- | --- | --- |
| T1 | first mock task | done | yes | mock evidence for T1 at 1794285711 |
| T2 | second mock task | done | yes | mock evidence for T2 at 1794285711 |
| T3 | third mock task | done | yes | mock evidence for T3 at 1794285712 |

## 上下文压力时间线 / Context pressure timeline

| Time | Session | Ratio | Source | Used/Window | Turn | Compactions | Level |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-11-10 12:41:51 | `mock-thread-7ed067-001` | 80.0% | simulated | None/None | 1 | 0 | handoff |
| 2026-11-10 12:41:52 | `mock-thread-7ed067-002` | 80.0% | simulated | None/None | 1 | 0 | handoff |

## 验收 / Acceptance

- 2026-11-10 12:41:52: complete=True (tasks complete, waiting for human acceptance)
  - pending human acceptance: no automated acceptance commands were configured for this chain

## Hooks 采集 / Hook evidence

- 由 `relay hook` 收集 / collected via hook command: none
- 由 app-server 通知上报 / reported by app-server notifications: none
- 说明 / Note: 本机 Codex 未执行已注册的 hook 命令，状态采集改由 app-server 通知通道完成（`turn/completed`、`item/completed`、`thread/tokenUsage/updated`、`thread/compacted`）。

## 真实验证 vs 模拟 / What is real, what is simulated

- 上下文压力数据来源 / context pressure sources: simulated
- 本链使用了**模拟上下文压力**，因此阈值触发本身不是真实压力验证；真实压力验证见 `relay test --mode real-pressure` 的报告。
- 真实 token 压力样本数 / real-token samples: 0

## 事件摘要 / Event summary

- chain_created: 1
- checkpoint_created: 2
- context_pressure: 2
- handoff_finished: 2
- handoff_started: 2
- session_started: 1
- turn_completed: 3
- validation: 1


---

# 开发报告 / Development Report — `ch_241cd3593419`

- 生成时间 / Generated: 2026-11-10 12:42:00
- 项目 / Project: `%LOCALAPPDATA%\CodexPotterRelay\test-workspaces\mock-rollback-1794285712`
- 工作分支 / Work branch: `relay/ch_241cd3593419`
- 模式 / Mode: test (test level: mock)
- 状态 / State: **failed**
- 停止原因 / Stop reason: attempt 2: injected thread/start failure
- 创建 / Created: 2026-11-10 12:41:53
- 完成 / Completed: 2026-11-10 12:41:54
- 模型请求数 / Model requests: 1
- 主会话数 / Main sessions: 1
- 交接次数 / Handoffs: 1
- 模拟压力 / Simulated pressure: YES

## 目标 / Objective

```text
mock rollback
```

## 主会话 / Main sessions (A, B, C, ... )

| # | Thread id | State | Turns | Input tok | Cached | Total tok | Context win |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `mock-thread-936e7d-001` | active | 1 | 9000 | 0 | 9512 | 10000 |

## 交接链 / Handoff chain

| Seq | From | To | Phase | State | Attempts | Simulated | Error |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `-` | `-` | START | failed | 2 | YES | attempt 2: injected thread/start failure |

## 检查点 / Checkpoints

| Version | Reason | Git head | Branch | Dirty | Session | Verified |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | handoff | `f54942f128` | `relay/ch_241cd3593419` | no | `-` | no |

## 任务台账 / Task ledger

| Task | Title | Status | Verified | Evidence |
| --- | --- | --- | --- | --- |
| T1 | task one | done | yes | mock evidence for T1 at 1794285714 |
| T2 | task two | pending | no |  |

## 上下文压力时间线 / Context pressure timeline

| Time | Session | Ratio | Source | Used/Window | Turn | Compactions | Level |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-11-10 12:41:54 | `mock-thread-936e7d-001` | 85.0% | simulated | None/None | 1 | 0 | handoff |

## 验收 / Acceptance

(no validation run)

## Hooks 采集 / Hook evidence

- 由 `relay hook` 收集 / collected via hook command: none
- 由 app-server 通知上报 / reported by app-server notifications: none
- 说明 / Note: 本机 Codex 未执行已注册的 hook 命令，状态采集改由 app-server 通知通道完成（`turn/completed`、`item/completed`、`thread/tokenUsage/updated`、`thread/compacted`）。

## 真实验证 vs 模拟 / What is real, what is simulated

- 上下文压力数据来源 / context pressure sources: simulated
- 本链使用了**模拟上下文压力**，因此阈值触发本身不是真实压力验证；真实压力验证见 `relay test --mode real-pressure` 的报告。
- 真实 token 压力样本数 / real-token samples: 0

## 事件摘要 / Event summary

- chain_created: 1
- checkpoint_created: 1
- context_pressure: 1
- handoff_finished: 1
- handoff_started: 1
- session_started: 1
- turn_completed: 1


---

# 开发报告 / Development Report — `ch_c8e113308ff3`

- 生成时间 / Generated: 2026-11-10 12:42:00
- 项目 / Project: `%LOCALAPPDATA%\CodexPotterRelay\test-workspaces\mock-guard-1794285714`
- 工作分支 / Work branch: `relay/ch_c8e113308ff3`
- 模式 / Mode: test (test level: mock)
- 状态 / State: **stopped**
- 停止原因 / Stop reason: handoff budget exhausted (2)
- 创建 / Created: 2026-11-10 12:41:55
- 完成 / Completed: 2026-11-10 12:41:56
- 模型请求数 / Model requests: 3
- 主会话数 / Main sessions: 3
- 交接次数 / Handoffs: 2
- 模拟压力 / Simulated pressure: YES

## 目标 / Objective

```text
mock infinite loop guard
```

## 主会话 / Main sessions (A, B, C, ... )

| # | Thread id | State | Turns | Input tok | Cached | Total tok | Context win |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `mock-thread-7d55e7-001` | active | 1 | 9000 | 0 | 9512 | 10000 |
| 2 | `mock-thread-7d55e7-002` | handed_off | 1 | 10000 | 0 | 19024 | 10000 |
| 3 | `mock-thread-7d55e7-003` | active | 1 | 10000 | 0 | 28536 | 10000 |

## 交接链 / Handoff chain

| Seq | From | To | Phase | State | Attempts | Simulated | Error |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `-` | `mock-thread-7d55e7-002` | CONTINUE | ok | 1 | YES |  |
| 2 | `mock-thread-7d55e7-002` | `mock-thread-7d55e7-003` | CONTINUE | ok | 1 | YES |  |

## 检查点 / Checkpoints

| Version | Reason | Git head | Branch | Dirty | Session | Verified |
| --- | --- | --- | --- | --- | --- | --- |
| 3 | pressure:urgent | `ebe7140695` | `relay/ch_c8e113308ff3` | no | `mock-thread-7d55e7-003` | no |
| 2 | handoff | `ebe7140695` | `relay/ch_c8e113308ff3` | no | `mock-thread-7d55e7-002` | yes |
| 1 | handoff | `ebe7140695` | `relay/ch_c8e113308ff3` | no | `-` | yes |

## 任务台账 / Task ledger

| Task | Title | Status | Verified | Evidence |
| --- | --- | --- | --- | --- |
| T1 | never finishes | pending | no |  |

## 上下文压力时间线 / Context pressure timeline

| Time | Session | Ratio | Source | Used/Window | Turn | Compactions | Level |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-11-10 12:41:55 | `mock-thread-7d55e7-001` | 95.0% | simulated | None/None | 1 | 0 | urgent |
| 2026-11-10 12:41:56 | `mock-thread-7d55e7-002` | 95.0% | simulated | None/None | 1 | 0 | urgent |
| 2026-11-10 12:41:56 | `mock-thread-7d55e7-003` | 95.0% | simulated | None/None | 1 | 0 | urgent |

## 验收 / Acceptance

(no validation run)

## Hooks 采集 / Hook evidence

- 由 `relay hook` 收集 / collected via hook command: none
- 由 app-server 通知上报 / reported by app-server notifications: none
- 说明 / Note: 本机 Codex 未执行已注册的 hook 命令，状态采集改由 app-server 通知通道完成（`turn/completed`、`item/completed`、`thread/tokenUsage/updated`、`thread/compacted`）。

## 真实验证 vs 模拟 / What is real, what is simulated

- 上下文压力数据来源 / context pressure sources: simulated
- 本链使用了**模拟上下文压力**，因此阈值触发本身不是真实压力验证；真实压力验证见 `relay test --mode real-pressure` 的报告。
- 真实 token 压力样本数 / real-token samples: 0

## 事件摘要 / Event summary

- chain_created: 1
- checkpoint_created: 3
- context_pressure: 3
- handoff_finished: 2
- handoff_started: 2
- session_started: 1
- turn_completed: 3


---

# 开发报告 / Development Report — `ch_ffdc7a05d979`

- 生成时间 / Generated: 2026-11-10 12:42:00
- 项目 / Project: `%LOCALAPPDATA%\CodexPotterRelay\test-workspaces\mock-iso-a-1794285716`
- 工作分支 / Work branch: `relay/ch_ffdc7a05d979`
- 模式 / Mode: test (test level: mock)
- 状态 / State: **complete**
- 停止原因 / Stop reason: tasks complete, waiting for human acceptance
- 创建 / Created: 2026-11-10 12:41:57
- 完成 / Completed: 2026-11-10 12:41:59
- 模型请求数 / Model requests: 1
- 主会话数 / Main sessions: 1
- 交接次数 / Handoffs: 0
- 模拟压力 / Simulated pressure: YES

## 目标 / Objective

```text
mock isolation project 1
```

## 主会话 / Main sessions (A, B, C, ... )

| # | Thread id | State | Turns | Input tok | Cached | Total tok | Context win |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `mock-thread-3c334b-001` | active | 1 | 5000 | 0 | 5512 | 50000 |

## 交接链 / Handoff chain

| Seq | From | To | Phase | State | Attempts | Simulated | Error |
| --- | --- | --- | --- | --- | --- | --- | --- |

## 检查点 / Checkpoints

| Version | Reason | Git head | Branch | Dirty | Session | Verified |
| --- | --- | --- | --- | --- | --- | --- |

## 任务台账 / Task ledger

| Task | Title | Status | Verified | Evidence |
| --- | --- | --- | --- | --- |
| T1 | task of project 1 | done | yes | mock evidence for T1 at 1794285718 |

## 上下文压力时间线 / Context pressure timeline

(no pressure samples recorded)

## 验收 / Acceptance

- 2026-11-10 12:41:59: complete=True (tasks complete, waiting for human acceptance)
  - pending human acceptance: no automated acceptance commands were configured for this chain

## Hooks 采集 / Hook evidence

- 由 `relay hook` 收集 / collected via hook command: none
- 由 app-server 通知上报 / reported by app-server notifications: none
- 说明 / Note: 本机 Codex 未执行已注册的 hook 命令，状态采集改由 app-server 通知通道完成（`turn/completed`、`item/completed`、`thread/tokenUsage/updated`、`thread/compacted`）。

## 真实验证 vs 模拟 / What is real, what is simulated

- 上下文压力数据来源 / context pressure sources: none
- 本链使用了**模拟上下文压力**，因此阈值触发本身不是真实压力验证；真实压力验证见 `relay test --mode real-pressure` 的报告。
- 真实 token 压力样本数 / real-token samples: 0

## 事件摘要 / Event summary

- chain_created: 1
- session_started: 1
- turn_completed: 1
- validation: 1


---

# 开发报告 / Development Report — `ch_4fde9fcd2724`

- 生成时间 / Generated: 2026-11-10 12:42:00
- 项目 / Project: `%LOCALAPPDATA%\CodexPotterRelay\test-workspaces\mock-iso-b-1794285716`
- 工作分支 / Work branch: `relay/ch_4fde9fcd2724`
- 模式 / Mode: test (test level: mock)
- 状态 / State: **complete**
- 停止原因 / Stop reason: tasks complete, waiting for human acceptance
- 创建 / Created: 2026-11-10 12:41:57
- 完成 / Completed: 2026-11-10 12:42:00
- 模型请求数 / Model requests: 1
- 主会话数 / Main sessions: 1
- 交接次数 / Handoffs: 0
- 模拟压力 / Simulated pressure: YES

## 目标 / Objective

```text
mock isolation project 2
```

## 主会话 / Main sessions (A, B, C, ... )

| # | Thread id | State | Turns | Input tok | Cached | Total tok | Context win |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `mock-thread-ce48a9-001` | active | 1 | 5000 | 0 | 5512 | 50000 |

## 交接链 / Handoff chain

| Seq | From | To | Phase | State | Attempts | Simulated | Error |
| --- | --- | --- | --- | --- | --- | --- | --- |

## 检查点 / Checkpoints

| Version | Reason | Git head | Branch | Dirty | Session | Verified |
| --- | --- | --- | --- | --- | --- | --- |

## 任务台账 / Task ledger

| Task | Title | Status | Verified | Evidence |
| --- | --- | --- | --- | --- |
| T1 | task of project 2 | done | yes | mock evidence for T1 at 1794285719 |

## 上下文压力时间线 / Context pressure timeline

(no pressure samples recorded)

## 验收 / Acceptance

- 2026-11-10 12:42:00: complete=True (tasks complete, waiting for human acceptance)
  - pending human acceptance: no automated acceptance commands were configured for this chain

## Hooks 采集 / Hook evidence

- 由 `relay hook` 收集 / collected via hook command: none
- 由 app-server 通知上报 / reported by app-server notifications: none
- 说明 / Note: 本机 Codex 未执行已注册的 hook 命令，状态采集改由 app-server 通知通道完成（`turn/completed`、`item/completed`、`thread/tokenUsage/updated`、`thread/compacted`）。

## 真实验证 vs 模拟 / What is real, what is simulated

- 上下文压力数据来源 / context pressure sources: none
- 本链使用了**模拟上下文压力**，因此阈值触发本身不是真实压力验证；真实压力验证见 `relay test --mode real-pressure` 的报告。
- 真实 token 压力样本数 / real-token samples: 0

## 事件摘要 / Event summary

- chain_created: 1
- session_started: 1
- turn_completed: 1
- validation: 1


---
