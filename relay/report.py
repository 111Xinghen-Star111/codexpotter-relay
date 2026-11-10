"""Human-readable development reports.

A report is generated from the database, never from in-memory state, so it can
be regenerated for any chain at any time (including after a controller crash).
It states plainly which evidence was real and which was simulated.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .db import Database
from .hooks import app_server_hook_events, hooks_observed, summarize_event_sources
from .paths import relay_home


def _format_ratio(ratio: float | None) -> str:
    return "n/a" if ratio is None else f"{ratio:.1%}"


def _format_ts(value: float | None) -> str:
    if not value:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def build_chain_report(database: Database, chain_id: str) -> str:
    """Render the markdown report for one chain."""
    chain = database.get_chain(chain_id)
    sessions = database.list_sessions(chain_id)
    handoffs = database.list_handoffs(chain_id)
    checkpoints = database.list_checkpoints(chain_id, limit=100)
    tasks = database.list_tasks(chain_id)
    events = database.list_events(chain_id=chain_id, limit=400)

    lines: list[str] = [
        f"# 开发报告 / Development Report — `{chain.id}`",
        "",
        f"- 生成时间 / Generated: {_format_ts(time.time())}",
        f"- 项目 / Project: `{chain.project_root}`",
        f"- 工作分支 / Work branch: `{chain.work_branch}`",
        f"- 模式 / Mode: {chain.mode}"
        + (f" (test level: {chain.test_level})" if chain.test_level else ""),
        f"- 状态 / State: **{chain.state}**",
        f"- 停止原因 / Stop reason: {chain.stop_reason or '-'}",
        f"- 创建 / Created: {_format_ts(chain.created_at)}",
        f"- 完成 / Completed: {_format_ts(chain.completed_at)}",
        f"- 模型请求数 / Model requests: {database.count_model_requests(chain_id)}",
        f"- 主会话数 / Main sessions: {len([s for s in sessions if s.role == 'main'])}",
        f"- 交接次数 / Handoffs: {len(handoffs)}",
        f"- 模拟压力 / Simulated pressure: {'YES' if chain.simulated else 'no'}",
        "",
        "## 目标 / Objective",
        "",
        "```text",
        chain.goal.strip(),
        "```",
        "",
        "## 主会话 / Main sessions (A, B, C, ... )",
        "",
        "| # | Thread id | State | Turns | Input tok | Cached | Total tok | Context win |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    main_sessions = [session for session in sessions if session.role == "main"]
    for session in main_sessions:
        lines.append(
            f"| {session.ordinal or '-'} | `{session.id}` | {session.state} | "
            f"{session.request_count} | {session.token_input} | {session.token_cached} | "
            f"{session.token_total} | {session.context_window or '-'} |"
        )

    lines.extend(
        [
            "",
            "## 交接链 / Handoff chain",
            "",
            "| Seq | From | To | Phase | State | Attempts | Simulated | Error |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for handoff in handoffs:
        details = json.loads(handoff.details_json or "{}")
        error = (handoff.error or "").replace("|", "\\|")
        if len(error) > 90:
            error = error[:87] + "..."
        lines.append(
            f"| {handoff.sequence} | `{handoff.from_session_id or '-'}` | "
            f"`{handoff.to_session_id or '-'}` | {handoff.phase} | {handoff.state} | "
            f"{handoff.attempt} | {'YES' if handoff.simulated else 'no'} | {error} |"
        )
        if details:
            lines.append("")

    lines.extend(
        [
            "",
            "## 检查点 / Checkpoints",
            "",
            "| Version | Reason | Git head | Branch | Dirty | Session | Verified |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for checkpoint in checkpoints:
        lines.append(
            f"| {checkpoint.version} | {checkpoint.reason} | "
            f"`{(checkpoint.git_head or '-')[:10]}` | `{checkpoint.git_branch or '-'}` | "
            f"{'yes' if checkpoint.git_dirty else 'no'} | `{checkpoint.session_id or '-'}` | "
            f"{'yes' if checkpoint.verified_at else 'no'} |"
        )

    lines.extend(
        [
            "",
            "## 任务台账 / Task ledger",
            "",
            "| Task | Title | Status | Verified | Evidence |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for task in tasks:
        evidence = (task.get("evidence") or "").replace("|", "\\|")
        if len(evidence) > 120:
            evidence = evidence[:117] + "..."
        lines.append(
            f"| {task['task_id']} | {task['title']} | {task['status']} | "
            f"{'yes' if task['verified'] else 'no'} | {evidence} |"
        )

    lines.extend(["", "## 上下文压力时间线 / Context pressure timeline", ""])
    pressure_events = [event for event in events if event["kind"] == "context_pressure"]
    if pressure_events:
        lines.append("| Time | Session | Ratio | Source | Used/Window | Turn | Compactions | Level |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for event in reversed(pressure_events[-40:]):
            payload = event["payload"]
            lines.append(
                f"| {_format_ts(event['occurred_at'])} | `{event['session_id'] or '-'}` | "
                f"{_format_ratio(payload.get('ratio'))} | {payload.get('source')} | "
                f"{payload.get('context_used_tokens')}/{payload.get('context_window')} | "
                f"{payload.get('turn_count')} | {payload.get('compaction_events')} | "
                f"{payload.get('level')} |"
            )
    else:
        lines.append("(no pressure samples recorded)")

    lines.extend(["", "## 验收 / Acceptance", ""])
    validation_events = [event for event in events if event["kind"] == "validation"]
    if not validation_events:
        lines.append("(no validation run)")
    for event in reversed(validation_events[-5:]):
        payload = event["payload"]
        lines.append(f"- {_format_ts(event['occurred_at'])}: complete={payload.get('complete')} "
                     f"({payload.get('summary')})")
        for failure in payload.get("failures") or []:
            lines.append(f"  - failure: {failure}")
        for pending in payload.get("pending_human") or []:
            lines.append(f"  - pending human acceptance: {pending}")
        for acceptance in payload.get("acceptance") or []:
            lines.append(
                f"  - `{acceptance.get('command')}` -> exit {acceptance.get('returncode')} "
                f"({'pass' if acceptance.get('passed') else 'FAIL'})"
            )

    lines.extend(["", "## Hooks 采集 / Hook evidence", ""])
    hook_observed = hooks_observed(database, chain_id=chain_id)
    notification_observed = app_server_hook_events(database, chain_id=chain_id)
    lines.append(f"- 由 `relay hook` 收集 / collected via hook command: {summarize_event_sources(hook_observed)}")
    lines.append(
        "- 由 app-server 通知上报 / reported by app-server notifications: "
        f"{summarize_event_sources(notification_observed)}"
    )
    if not hook_observed:
        lines.append(
            "- 说明 / Note: 本机 Codex 未执行已注册的 hook 命令，状态采集改由 app-server "
            "通知通道完成（`turn/completed`、`item/completed`、`thread/tokenUsage/updated`、"
            "`thread/compacted`）。"
        )

    lines.extend(["", "## 真实验证 vs 模拟 / What is real, what is simulated", ""])
    sources = {
        event["payload"].get("source")
        for event in pressure_events
        if isinstance(event["payload"], dict)
    }
    lines.append(
        "- 上下文压力数据来源 / context pressure sources: "
        + (", ".join(sorted(source for source in sources if source)) or "none")
    )
    if chain.simulated or "simulated" in sources:
        lines.append(
            "- 本链使用了**模拟上下文压力**，因此阈值触发本身不是真实压力验证；"
            "真实压力验证见 `relay test --mode real-pressure` 的报告。"
        )
    real_pressure_events = [
        event
        for event in pressure_events
        if (event["payload"] or {}).get("source") == "real"
        and (event["payload"] or {}).get("ratio") is not None
    ]
    lines.append(
        f"- 真实 token 压力样本数 / real-token samples: {len(real_pressure_events)}"
    )

    lines.extend(["", "## 事件摘要 / Event summary", ""])
    counts: dict[str, int] = {}
    for event in events:
        counts[event["kind"]] = counts.get(event["kind"], 0) + 1
    for kind in sorted(counts):
        lines.append(f"- {kind}: {counts[kind]}")

    return "\n".join(lines) + "\n"


def write_chain_report(
    database: Database, chain_id: str, *, output_dir: Path | None = None
) -> Path:
    """Write the report to disk and return its path."""
    directory = output_dir or (relay_home() / "reports")
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    path = directory / f"{chain_id}-{timestamp}.md"
    path.write_text(build_chain_report(database, chain_id), encoding="utf-8")
    return path


def write_test_report(
    *,
    database: Database,
    chain_ids: list[str],
    mode: str,
    output_dir: Path | None = None,
    extra_notes: list[str] | None = None,
) -> Path:
    """Write a combined report for one test run (Level 1/2/3)."""
    directory = output_dir or (relay_home() / "reports")
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    path = directory / f"test-{mode}-{timestamp}.md"
    lines = [
        f"# 测试报告 / Test Report — {mode}",
        "",
        f"- 生成时间 / Generated: {_format_ts(time.time())}",
        f"- 工作链 / Chains: {', '.join(f'`{chain_id}`' for chain_id in chain_ids)}",
        "",
    ]
    if extra_notes:
        lines.append("## 结论 / Verdict")
        lines.append("")
        lines.extend(f"- {note}" for note in extra_notes)
        lines.append("")
    for chain_id in chain_ids:
        lines.append(build_chain_report(database, chain_id))
        lines.append("\n---\n")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
