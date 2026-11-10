"""Command line interface for CodexPotter Relay.

Every operational action the requirements list has a command here:
install/init, start/stop controller, create a chain, inspect status, write a
checkpoint, hand off manually, pause/resume, query history, run the three test
levels and regenerate reports.

The CLI is the only supported entry point for a chain: ``relay start`` submits
the objective once, and the controller keeps going on its own afterwards.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import paths
from .checkpoint import GitRepository, CheckpointError, read_package
from .codex_env import find_codex_executable
from .config import ConfigError, RelayConfig, load_config, write_default_config
from .db import Database, canonical_json
from .hooks import ALL_EVENTS, HookInstaller
from .logging_setup import configure_logging
from .ops import (
    capture_checkpoint,
    doctor,
    make_event_sink,
    manual_handoff,
    pause_chain,
    resume_chain,
    stop_chain,
)
from .report import write_chain_report
from .tests_runner import LEVEL1, LEVEL2, LEVEL3, TestOutcome, run_level1, run_level2, run_level3


def production_database() -> Database:
    """Return the migrated production database."""
    database = Database(paths.database_path("production"))
    database.migrate()
    return database


def test_database() -> Database:
    """Return the migrated test database."""
    database = Database(paths.database_path("test"))
    database.migrate()
    return database


def _logger(name: str) -> logging.Logger:
    return configure_logging(name, to_console=True)


def build_parser() -> argparse.ArgumentParser:
    """Build the full CLI parser."""
    parser = argparse.ArgumentParser(
        prog="relay",
        description="CodexPotter Relay — automatic cross-session Codex handoff",
    )
    parser.add_argument("--config", type=Path, default=None, help="configuration file path")
    parser.add_argument("--db", type=Path, default=None, help="database path override")
    parser.add_argument(
        "--codex", default=None, metavar="PATH", help="Codex CLI executable (overrides detection)"
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="create the configuration file and database")
    subparsers.add_parser("doctor", help="check Codex, git and Relay prerequisites")

    start = subparsers.add_parser("start", help="submit one objective and start the chain")
    start.add_argument("--goal", required=True, help="the development objective")
    start.add_argument("--project", type=Path, default=None, help="project directory (default: cwd)")
    start.add_argument(
        "--task",
        action="append",
        default=[],
        metavar="ID:TITLE",
        help="task entry, repeatable (default: a single task derived from --goal)",
    )
    start.add_argument("--accept", action="append", default=[], metavar="CMD", help="acceptance command")
    start.add_argument("--model", default=None, help="model override for relay sessions")
    start.add_argument("--effort", default=None, help="reasoning effort override")
    start.add_argument("--max-turns", type=int, default=None)
    start.add_argument("--max-handoffs", type=int, default=None)
    start.add_argument("--max-model-requests", type=int, default=None)
    start.add_argument("--no-auto-handoff", action="store_true", help="collect only, never hand off")
    start.add_argument("--detach", action="store_true", help="run the controller detached")

    run = subparsers.add_parser("run", help="run the controller for an existing chain")
    run.add_argument("--chain", required=True)
    run.add_argument("--max-turns", type=int, default=None)
    run.add_argument("--max-handoffs", type=int, default=None)
    run.add_argument("--max-model-requests", type=int, default=None)
    run.add_argument("--detach", action="store_true", help="run the controller detached")

    subparsers.add_parser("chains", help="list work chains")
    status = subparsers.add_parser("status", help="show chain status")
    status.add_argument("--chain", default=None)
    status.add_argument("--all", action="store_true")
    status.add_argument("--events", type=int, default=0, help="also print the last N events")

    checkpoint = subparsers.add_parser("checkpoint", help="write a checkpoint now")
    checkpoint.add_argument("--chain", required=True)
    checkpoint.add_argument("--reason", default="manual")

    handoff = subparsers.add_parser("handoff", help="hand off to a new main session now")
    handoff.add_argument("--chain", required=True)

    pause = subparsers.add_parser("pause", help="pause automatic execution")
    pause.add_argument("--chain", required=True)
    resume = subparsers.add_parser("resume", help="resume a paused chain")
    resume.add_argument("--chain", required=True)
    stop = subparsers.add_parser("stop", help="stop a chain permanently")
    stop.add_argument("--chain", required=True)

    history = subparsers.add_parser("history", help="show checkpoints, handoffs and events")
    history.add_argument("--chain", default=None)
    history.add_argument("--limit", type=int, default=20)
    history.add_argument("--events", action="store_true")

    report = subparsers.add_parser("report", help="write the development report")
    report.add_argument("--chain", required=True)

    test = subparsers.add_parser("test", help="run a test level")
    test.add_argument("--mode", required=True, choices=[LEVEL1, LEVEL2, LEVEL3])
    test.add_argument("--keep-workspaces", action="store_true")

    test_handoff = subparsers.add_parser("test-handoff", help="run N consecutive real handoffs")
    test_handoff.add_argument("--count", type=int, default=3)
    test_handoff.add_argument("--keep-workspaces", action="store_true")

    test_report = subparsers.add_parser("test-report", help="regenerate the latest test report")
    test_report.add_argument("--chain", default=None)
    test_report.add_argument("--mode", default=LEVEL2, choices=[LEVEL1, LEVEL2, LEVEL3])

    recover = subparsers.add_parser(
        "recover", help="resume chains that were left mid-flight by a stopped controller"
    )
    recover.add_argument("--chain", default=None)
    recover.add_argument("--dry-run", action="store_true")
    recover.add_argument("--max-turns", type=int, default=None)
    recover.add_argument("--max-handoffs", type=int, default=None)
    recover.add_argument("--max-model-requests", type=int, default=None)

    subparsers.add_parser("mcp", help="(internal) run the MCP server used by the Codex plugin")

    plugin = subparsers.add_parser("plugin", help="install the Relay as a Codex plugin")
    plugin.add_argument("action", choices=["install", "uninstall", "status"])

    hooks = subparsers.add_parser("hooks", help="install or remove the hook collector")
    hooks.add_argument("action", choices=["install", "uninstall", "status"])

    hook = subparsers.add_parser("hook", help="(internal) hook collector entry point")
    hook.add_argument("--event", required=True)
    hook.add_argument("--db", type=Path, default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    _configure_console_encoding()
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        return _dispatch(parser, arguments)
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    except (CheckpointError, RuntimeError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def _configure_console_encoding() -> None:
    """Force UTF-8 console output so Chinese text survives the Windows code page."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            continue


def _resolve_config(arguments: argparse.Namespace) -> RelayConfig:
    config = load_config(arguments.config)
    codex_override = getattr(arguments, "codex", None)
    if codex_override:
        from dataclasses import replace

        config = replace(
            config,
            controller=replace(config.controller, codex_executable=codex_override),
        )
        config.validate()
    return config


def _resolve_database(arguments: argparse.Namespace) -> Database:
    if arguments.db is not None:
        database = Database(arguments.db)
        database.migrate()
        return database
    return production_database()


def _dispatch(parser: argparse.ArgumentParser, arguments: argparse.Namespace) -> int:
    command = arguments.command
    if command == "init":
        return _cmd_init(arguments)
    if command == "doctor":
        return _cmd_doctor(arguments)
    if command == "start":
        return _cmd_start(arguments)
    if command == "run":
        return _cmd_run(arguments)
    if command == "chains":
        return _cmd_chains(arguments)
    if command == "status":
        return _cmd_status(arguments)
    if command == "checkpoint":
        return _cmd_checkpoint(arguments)
    if command == "handoff":
        return _cmd_handoff(arguments)
    if command in ("pause", "resume", "stop"):
        return _cmd_lifecycle(arguments)
    if command == "history":
        return _cmd_history(arguments)
    if command == "report":
        return _cmd_report(arguments)
    if command == "test":
        return _cmd_test(arguments)
    if command == "test-handoff":
        return _cmd_test_handoff(arguments)
    if command == "test-report":
        return _cmd_test_report(arguments)
    if command == "recover":
        return _cmd_recover(arguments)
    if command == "mcp":
        return _cmd_mcp(arguments)
    if command == "plugin":
        return _cmd_plugin(arguments)
    if command == "hooks":
        return _cmd_hooks(arguments)
    if command == "hook":
        return _cmd_hook(arguments)
    parser.error(f"unknown command: {command}")
    return 2


# ----------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------
def _cmd_init(arguments: argparse.Namespace) -> int:
    config_file = write_default_config(arguments.config)
    database = _resolve_database(arguments)
    payload = {
        "config_file": str(config_file),
        "database": str(database.path),
        "schema_version": database.schema_version(),
        "relay_home": str(paths.relay_home()),
    }
    _print(arguments, payload, f"配置: {config_file}\n数据库: {database.path}\nSchema: {database.schema_version()}")
    return 0


def _cmd_doctor(arguments: argparse.Namespace) -> int:
    config = _resolve_config(arguments)
    report = doctor(config)
    _print(arguments, report, _format_doctor(report))
    ok = bool(report.get("codex_executable")) and bool(report.get("git_executable"))
    return 0 if ok else 1


def _cmd_start(arguments: argparse.Namespace) -> int:
    from .controller import Budget, RelayController

    config = _resolve_config(arguments)
    if arguments.no_auto_handoff:
        from dataclasses import replace

        config = replace(config, relay=replace(config.relay, auto_handoff=False))
    if arguments.model:
        from dataclasses import replace

        config = replace(config, relay=replace(config.relay, model=arguments.model))
    if arguments.effort:
        from dataclasses import replace

        config = replace(config, relay=replace(config.relay, effort=arguments.effort))

    project = (arguments.project or Path.cwd()).expanduser().resolve()
    tasks = [_parse_task(spec) for spec in arguments.task]
    database = _resolve_database(arguments)
    chain = RelayController.create_chain(
        database=database,
        project_root=project,
        goal=arguments.goal,
        tasks=tasks,
        acceptance_commands=list(arguments.accept),
        config=config,
        mode="production",
    )
    print(f"chain: {chain.id}")
    print(f"project: {chain.project_root}")
    print(f"branch: {chain.work_branch}")

    if arguments.detach:
        return _spawn_detached(arguments, chain.id)
    return _run_controller(config, database, chain.id, arguments)


def _cmd_run(arguments: argparse.Namespace) -> int:
    config = _resolve_config(arguments)
    database = _resolve_database(arguments)
    if arguments.detach:
        return _spawn_detached(arguments, arguments.chain)
    return _run_controller(config, database, arguments.chain, arguments)


def _cmd_chains(arguments: argparse.Namespace) -> int:
    database = _resolve_database(arguments)
    chains = database.list_chains(limit=100)
    payload = [
        {
            "id": chain.id,
            "state": chain.state,
            "mode": chain.mode,
            "test_level": chain.test_level,
            "project": chain.project_root,
            "branch": chain.work_branch,
            "created_at": chain.created_at,
            "stop_reason": chain.stop_reason,
        }
        for chain in chains
    ]
    lines = ["ID              STATE      MODE        PROJECT"]
    for chain in chains:
        lines.append(f"{chain.id:<15} {chain.state:<10} {chain.mode:<11} {chain.project_root}")
    _print(arguments, payload, "\n".join(lines) if chains else "没有工作链 / no chains")
    return 0


def _cmd_status(arguments: argparse.Namespace) -> int:
    database = _resolve_database(arguments)
    chain_id = arguments.chain
    if chain_id is None:
        active = database.list_chains(limit=20)
        if not active:
            print("没有工作链 / no chains")
            return 0
        chain_id = active[0].id
    chain = database.get_chain(chain_id)
    sessions = database.list_sessions(chain.id)
    handoffs = database.list_handoffs(chain.id)
    checkpoints = database.list_checkpoints(chain.id, limit=10)
    tasks = database.list_tasks(chain.id)
    lock = database.lock_owner(chain.id)
    payload = {
        "chain": {
            "id": chain.id,
            "state": chain.state,
            "goal": chain.goal,
            "project": chain.project_root,
            "branch": chain.work_branch,
            "active_session": chain.active_session_id,
            "stop_reason": chain.stop_reason,
        },
        "sessions": [
            {
                "ordinal": session.ordinal,
                "id": session.id,
                "state": session.state,
                "requests": session.request_count,
                "tokens": session.token_total,
                "context_window": session.context_window,
            }
            for session in sessions
        ],
        "handoffs": [
            {"sequence": handoff.sequence, "state": handoff.state, "phase": handoff.phase}
            for handoff in handoffs
        ],
        "checkpoints": [
            {"version": checkpoint.version, "reason": checkpoint.reason, "head": checkpoint.git_head}
            for checkpoint in checkpoints
        ],
        "tasks": tasks,
        "lock": lock,
        "model_requests": database.count_model_requests(chain.id),
    }
    if arguments.events:
        payload["events"] = database.list_events(chain_id=chain.id, limit=arguments.events)
    _print(arguments, payload, _format_status(payload))
    return 0


def _cmd_checkpoint(arguments: argparse.Namespace) -> int:
    database = _resolve_database(arguments)
    logger = _logger("cli")
    chain = database.get_chain(arguments.chain)
    checkpoint = capture_checkpoint(database, chain, reason=arguments.reason, logger=logger)
    payload = {
        "chain": chain.id,
        "checkpoint_id": checkpoint.id,
        "version": checkpoint.version,
        "git_head": checkpoint.git_head,
        "verified": checkpoint.is_intact(),
    }
    _print(arguments, payload, f"检查点 v{checkpoint.version} 已写入（校验通过={checkpoint.is_intact()}）")
    return 0


def _cmd_handoff(arguments: argparse.Namespace) -> int:
    config = _resolve_config(arguments)
    database = _resolve_database(arguments)
    logger = _logger("cli")
    chain = database.get_chain(arguments.chain)
    result = manual_handoff(config, database, chain, logger=logger)
    payload = {
        "chain": chain.id,
        "success": result.success,
        "new_session": result.new_session_id,
        "attempts": result.attempts,
        "error": result.error,
        "rolled_back": result.rolled_back,
    }
    _print(
        arguments,
        payload,
        "交接成功: " + str(result.new_session_id)
        if result.success
        else f"交接失败: {result.error}",
    )
    return 0 if result.success else 1


def _cmd_lifecycle(arguments: argparse.Namespace) -> int:
    database = _resolve_database(arguments)
    chain = database.get_chain(arguments.chain)
    if arguments.command == "pause":
        chain = pause_chain(database, chain)
    elif arguments.command == "resume":
        chain = resume_chain(database, chain)
    else:
        chain = stop_chain(database, chain)
    _print(arguments, {"chain": chain.id, "state": chain.state}, f"{chain.id}: {chain.state}")
    return 0


def _cmd_history(arguments: argparse.Namespace) -> int:
    database = _resolve_database(arguments)
    chain_id = arguments.chain
    chains = [database.get_chain(chain_id)] if chain_id else database.list_chains(limit=10)
    lines: list[str] = []
    payload: list[dict[str, Any]] = []
    for chain in chains:
        lines.append(f"== {chain.id} [{chain.state}] {chain.project_root}")
        checkpoints = database.list_checkpoints(chain.id, limit=arguments.limit)
        for checkpoint in checkpoints:
            lines.append(
                f"  checkpoint v{checkpoint.version:<3} {checkpoint.reason:<24} "
                f"{(checkpoint.git_head or '-')[:10]} verified={'yes' if checkpoint.verified_at else 'no'}"
            )
        handoffs = database.list_handoffs(chain.id)
        for handoff in handoffs:
            lines.append(
                f"  handoff #{handoff.sequence} {handoff.phase:<9} {handoff.state:<11} "
                f"{handoff.from_session_id} -> {handoff.to_session_id}"
            )
        payload.append(
            {
                "chain": chain.id,
                "checkpoints": [c.version for c in checkpoints],
                "handoffs": [
                    {"sequence": h.sequence, "phase": h.phase, "state": h.state} for h in handoffs
                ],
            }
        )
        if arguments.events:
            for event in database.list_events(chain_id=chain.id, limit=arguments.limit):
                lines.append(
                    f"  event {time.strftime('%H:%M:%S', time.localtime(event['occurred_at']))} "
                    f"{event['kind']:<24} {json.dumps(event['payload'], ensure_ascii=False)[:120]}"
                )
    _print(arguments, payload, "\n".join(lines))
    return 0


def _cmd_report(arguments: argparse.Namespace) -> int:
    database = _resolve_database(arguments)
    path = write_chain_report(database, arguments.chain)
    _print(arguments, {"report": str(path)}, f"报告已生成: {path}")
    return 0


def _cmd_test(arguments: argparse.Namespace) -> int:
    config = _resolve_config(arguments)
    logger = _logger("test")
    if arguments.mode == LEVEL1:
        outcome = run_level1(config=config, logger=logger, keep_workspaces=arguments.keep_workspaces)
    elif arguments.mode == LEVEL2:
        outcome = run_level2(config=config, logger=logger, keep_workspaces=arguments.keep_workspaces)
    else:
        outcome = run_level3(config=config, logger=logger, keep_workspaces=arguments.keep_workspaces)
    return _print_outcome(arguments, outcome)


def _cmd_test_handoff(arguments: argparse.Namespace) -> int:
    config = _resolve_config(arguments)
    logger = _logger("test")
    count = max(1, arguments.count)
    tasks = [
        {
            "id": f"T{index}",
            "title": f"Add feature_{index}() returning {index} to features.py with a unit test",
        }
        for index in range(1, count + 2)
    ]
    outcome = run_level2(
        config=config,
        logger=logger,
        keep_workspaces=arguments.keep_workspaces,
        tasks=tasks,
        handoff_count=count,
    )
    return _print_outcome(arguments, outcome)


def _cmd_test_report(arguments: argparse.Namespace) -> int:
    database = test_database()
    if arguments.chain:
        chain_ids = [arguments.chain]
    else:
        chains = database.list_chains(mode="test", limit=20)
        chain_ids = [chain.id for chain in chains]
    if not chain_ids:
        print("测试数据库中没有工作链 / no chains in the test database")
        return 1
    from .report import write_test_report

    path = write_test_report(database=database, chain_ids=chain_ids, mode=arguments.mode)
    _print(arguments, {"report": str(path)}, f"测试报告已生成: {path}")
    return 0


def _cmd_recover(arguments: argparse.Namespace) -> int:
    """Resume chains that a previous controller left in ``running``/``handoff``."""
    database = _resolve_database(arguments)
    if arguments.chain:
        candidates = [database.get_chain(arguments.chain)]
    else:
        candidates = database.chains_needing_recovery()
    if not candidates:
        print("没有需要恢复的工作链 / nothing to recover")
        return 0
    lines = ["待恢复工作链 / chains to recover:"]
    for chain in candidates:
        lock = database.lock_owner(chain.id)
        lines.append(f"  {chain.id}  state={chain.state:<8} lock={lock or 'free'}")
    print("\n".join(lines))
    if arguments.dry_run:
        return 0
    config = _resolve_config(arguments)
    failures = 0
    for chain in candidates:
        print(f"--- resuming {chain.id}")
        code = _run_controller(config, database, chain.id, arguments)
        if code != 0:
            failures += 1
    return 0 if failures == 0 else 1


def _cmd_mcp(arguments: argparse.Namespace) -> int:
    """Run the MCP stdio server (started by Codex for the plugin)."""
    from .mcp import McpServer, RelayToolbox

    toolbox = RelayToolbox(config_path=arguments.config, db_path=arguments.db)
    return McpServer(toolbox).serve()


def _cmd_plugin(arguments: argparse.Namespace) -> int:
    from .plugin import install_plugin, plugin_status, uninstall_plugin

    if arguments.action == "install":
        result = install_plugin()
        _print(
            arguments,
            result,
            "Codex 插件安装"
            + ("成功" if result["ok"] else "未完成")
            + f"\n  plugin: {result['plugin_root']}\n  registered: {result['registered']}",
        )
        return 0 if result["ok"] else 1
    if arguments.action == "uninstall":
        result = uninstall_plugin()
        _print(arguments, result, f"插件已卸载（registered={result['registered']}）")
        return 0
    status = plugin_status()
    _print(arguments, status, _format_plugin_status(status))
    return 0 if status["registered"] else 1


def _format_plugin_status(status: dict[str, object]) -> str:
    return (
        f"plugin id: {status['plugin_id']}\n"
        f"marketplace: {status['marketplace_root']} (rendered={status['rendered']})\n"
        f"plugin dir: {status['plugin_root']}\n"
        f"registered in Codex config: {status['registered']}\n"
        f"installed in cache: {status['cache_present']} ({status['cache_dir']})"
    )


def _cmd_hooks(arguments: argparse.Namespace) -> int:
    installer = HookInstaller(database_path=paths.database_path("production"))
    if arguments.action == "install":
        result = installer.install()
        result["registration"] = installer.verify_registration()
        _print(
            arguments,
            result,
            "hooks 已写入: "
            + str(result["config_file"])
            + f"\nCodex 实际注册的 hook: {result['registration'].get('registered')}",
        )
        return 0
    if arguments.action == "uninstall":
        result = installer.uninstall()
        _print(arguments, result, f"hooks 移除: {result}")
        return 0
    from .hooks import hooks_observed

    observed = hooks_observed(production_database())
    payload = {
        "config_file": str(installer.config_file),
        "installed": installer.config_file.exists()
        and "CodexPotter Relay hooks" in installer.config_file.read_text(encoding="utf-8"),
        "observed_events": observed,
        "requested_events": list(ALL_EVENTS),
    }
    _print(arguments, payload, _format_hooks_status(payload))
    return 0


def _cmd_hook(arguments: argparse.Namespace) -> int:
    from .hooks import run_hook_command

    database = Database(arguments.db) if arguments.db else production_database()
    stdin_text = ""
    if not sys.stdin.isatty():
        try:
            stdin_text = sys.stdin.read()
        except OSError:
            stdin_text = ""
    return run_hook_command(event=arguments.event, database=database, stdin_text=stdin_text)


# ----------------------------------------------------------------------
# controller execution
# ----------------------------------------------------------------------
def _run_controller(
    config: RelayConfig,
    database: Database,
    chain_id: str,
    arguments: argparse.Namespace,
) -> int:
    from .controller import Budget, RelayController
    from .agent_session import CodexAgent
    from .appserver import AppServer
    from .report import write_chain_report

    logger = _logger("controller")
    chain = database.get_chain(chain_id)
    budget = Budget(
        max_turns=getattr(arguments, "max_turns", None) or config.relay.max_turns,
        max_handoffs=getattr(arguments, "max_handoffs", None) or config.relay.max_handoffs,
        max_model_requests=(
            getattr(arguments, "max_model_requests", None) or config.relay.max_model_requests
        ),
        max_token_total=config.relay.token_budget,
    )
    codex = find_codex_executable(config.controller.codex_executable)
    server = AppServer(
        codex,
        config_overrides=_session_overrides(config),
        cwd=Path(chain.project_root),
        request_timeout=config.relay.turn_timeout_seconds,
        startup_timeout=config.controller.startup_timeout_seconds,
        stderr_path=paths.log_dir() / f"appserver-{chain.id}.log",
        logger=logger,
    )
    server.start()
    try:
        agent = CodexAgent(
            server,
            project_root=Path(chain.project_root),
            logger=logger,
            event_sink=make_event_sink(database, chain.id),
        )
        controller = RelayController(
            config=config,
            database=database,
            chain_id=chain.id,
            driver=agent,
            logger=logger,
            budget=budget,
            report_writer=lambda target: write_chain_report(database, target),
        )
        result = controller.run()
    finally:
        server.stop()
    payload = {
        "chain": result.chain_id,
        "state": result.state,
        "reason": result.reason,
        "turns": result.turns,
        "handoffs": result.handoffs,
        "model_requests": result.model_requests,
        "sessions": result.sessions,
        "duration_seconds": result.duration_seconds,
        "report": str(result.report_path) if result.report_path else None,
    }
    _print(arguments, payload, _format_run_result(payload))
    return 0 if result.completed else 1


def _spawn_detached(arguments: argparse.Namespace, chain_id: str) -> int:
    """Start the controller for ``chain_id`` in a detached child process."""
    from .ops import spawn_detached_controller

    budgets = {
        flag: getattr(arguments, flag, None)
        for flag in ("max_turns", "max_handoffs", "max_model_requests")
    }
    pid, log_file = spawn_detached_controller(
        chain_id,
        config_path=arguments.config,
        db_path=arguments.db,
        budgets=budgets,
    )
    print(f"controller detached, pid={pid}, log={log_file}")
    return 0


def _session_overrides(config: RelayConfig) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if config.testing.context_window_override:
        overrides["model_context_window"] = config.testing.context_window_override
    if config.testing.auto_compact_token_limit:
        overrides["model_auto_compact_token_limit"] = config.testing.auto_compact_token_limit
    return overrides


# ----------------------------------------------------------------------
# formatting helpers
# ----------------------------------------------------------------------
def _print(arguments: argparse.Namespace, payload: Any, text: str) -> None:
    if getattr(arguments, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(text)


def _parse_task(spec: str) -> dict[str, Any]:
    if ":" in spec:
        task_id, title = spec.split(":", 1)
        return {"id": task_id.strip(), "title": title.strip(), "status": "pending"}
    return {"id": spec.strip(), "title": spec.strip(), "status": "pending"}


def _format_doctor(report: dict[str, Any]) -> str:
    lines = ["CodexPotter Relay doctor", ""]
    for key, value in report.items():
        lines.append(f"  {key}: {value}")
    return "\n".join(lines)


def _format_status(payload: dict[str, Any]) -> str:
    chain = payload["chain"]
    lines = [
        f"工作链 {chain['id']}",
        f"  状态: {chain['state']}",
        f"  目标: {chain['goal']}",
        f"  项目: {chain['project']} ({chain['branch']})",
        f"  活动会话: {chain['active_session'] or '-'}",
        f"  模型请求: {payload['model_requests']}",
        f"  停止原因: {chain['stop_reason'] or '-'}",
        "",
        "会话:",
    ]
    for session in payload["sessions"]:
        lines.append(
            f"  #{session['ordinal'] or '-'} {session['id'][:16]:<16} {session['state']:<10} "
            f"turns={session['requests']:<3} tokens={session['tokens']:<8} ctx={session['context_window'] or '-'}"
        )
    lines.append("交接:")
    for handoff in payload["handoffs"]:
        lines.append(f"  #{handoff['sequence']} {handoff['phase']:<9} {handoff['state']}")
    lines.append("检查点:")
    for checkpoint in payload["checkpoints"]:
        lines.append(
            f"  v{checkpoint['version']} {checkpoint['reason']} head={(checkpoint['head'] or '-')[:10]}"
        )
    lines.append("任务:")
    for task in payload["tasks"]:
        lines.append(f"  {task['task_id']}: {task['status']} — {task['title']}")
    return "\n".join(lines)


def _format_run_result(payload: dict[str, Any]) -> str:
    return (
        f"工作链 {payload['chain']} 结束: {payload['state']} ({payload['reason']})\n"
        f"  turns={payload['turns']} handoffs={payload['handoffs']} "
        f"model_requests={payload['model_requests']}\n"
        f"  sessions={payload['sessions']}\n"
        f"  report={payload['report']}"
    )


def _format_hooks_status(payload: dict[str, Any]) -> str:
    return (
        f"配置文件: {payload['config_file']}\n"
        f"已写入 hooks 块: {payload['installed']}\n"
        f"实际观测到的 hook 事件: {payload['observed_events'] or '无'}"
    )


def _print_outcome(arguments: argparse.Namespace, outcome: TestOutcome) -> int:
    payload = {
        "mode": outcome.mode,
        "passed": outcome.passed,
        "checks": [
            {"name": check.name, "passed": check.passed, "detail": check.detail}
            for check in outcome.checks
        ],
        "chains": outcome.chain_ids,
        "model_requests": outcome.model_requests,
        "duration_seconds": outcome.duration_seconds,
        "report": str(outcome.report_path) if outcome.report_path else None,
        "notes": outcome.notes,
    }
    lines = [f"测试模式: {outcome.mode}", ""]
    lines.extend(outcome.summary_lines())
    lines.append("")
    lines.append(f"结论: {'通过' if outcome.passed else '未通过'}")
    lines.append(f"模型请求数: {outcome.model_requests}")
    if outcome.report_path:
        lines.append(f"报告: {outcome.report_path}")
    _print(arguments, payload, "\n".join(lines))
    return 0 if outcome.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
