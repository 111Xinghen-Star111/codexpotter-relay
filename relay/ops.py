"""Operational helpers used by the CLI outside a running controller.

These functions keep the CLI honest: a manual checkpoint writes exactly the same
artefacts as an automatic one, and a manual handoff runs the same state machine
instead of re-implementing a shortcut.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .agent_session import CodexAgent
from .appserver import AppServer
from .checkpoint import (
    GitRepository,
    HandoffPackage,
    build_handoff_markdown,
    ensure_scaffold,
    read_package,
    write_package,
)
from .codex_env import codex_login_status, codex_version, find_codex_executable
from .config import RelayConfig
from .db import Checkpoint, Chain, Database
from .handoff import HandoffManager, HandoffResult
from .paths import log_dir


def capture_checkpoint(
    database: Database,
    chain: Chain,
    *,
    reason: str,
    logger: Any = None,
    git_executable: str | None = None,
    context_notes: str = "",
) -> Checkpoint:
    """Write a checkpoint from the current on-disk ledger plus git state."""
    project_root = Path(chain.project_root)
    ensure_scaffold(project_root)
    package = read_package(project_root)
    git = GitRepository(project_root, git_executable=git_executable)
    snapshot = git.snapshot()
    handoff_md = build_handoff_markdown(
        chain_id=chain.id,
        goal=chain.goal,
        original_request=chain.goal,
        context_notes=context_notes,
        state=package.state,
        git_snapshot=snapshot,
        handoff_sequence=len(database.list_handoffs(chain.id)),
    )
    write_package(project_root, HandoffPackage(handoff_md=handoff_md, state=package.state))
    checkpoint = database.create_checkpoint(
        chain_id=chain.id,
        reason=reason,
        handoff_md=handoff_md,
        state_json=json.dumps(package.state, ensure_ascii=False, indent=2, sort_keys=True),
        git_head=snapshot.head,
        git_branch=snapshot.branch,
        git_dirty=snapshot.dirty,
        git_status_text=snapshot.status_text,
        session_id=chain.active_session_id,
    )
    database.append_event(
        event_key=f"{chain.id}:checkpoint:{checkpoint.id}",
        kind="checkpoint_created",
        source="controller",
        payload={
            "checkpoint_id": checkpoint.id,
            "version": checkpoint.version,
            "reason": reason,
            "git_head": snapshot.head,
            "manual": True,
        },
        chain_id=chain.id,
        session_id=chain.active_session_id,
    )
    if logger is not None:
        logger.info(
            "manual checkpoint written",
            extra={"relay_fields": {"chain_id": chain.id, "checkpoint_id": checkpoint.id}},
        )
    return checkpoint


def manual_handoff(
    config: RelayConfig,
    database: Database,
    chain: Chain,
    *,
    logger: Any,
) -> HandoffResult:
    """Run one handoff outside the controller loop (used by ``relay handoff``)."""
    if not database.acquire_lock(chain.id):
        raise RuntimeError(
            "the controller currently owns this chain; use `relay pause` first or "
            "let it hand off automatically"
        )
    codex = find_codex_executable(config.controller.codex_executable)
    project_root = Path(chain.project_root)
    server = AppServer(
        codex,
        config_overrides=_overrides(config),
        cwd=project_root,
        request_timeout=config.relay.turn_timeout_seconds,
        startup_timeout=config.controller.startup_timeout_seconds,
        stderr_path=log_dir() / f"manual-handoff-{chain.id}.log",
        logger=logger,
    )
    try:
        server.start()
        agent = CodexAgent(server, project_root=project_root, logger=logger)
        git = GitRepository(project_root, git_executable=None)
        manager = HandoffManager(
            database=database,
            chain=chain,
            driver=agent,
            git=git,
            config=config,
            project_root=project_root,
            write_checkpoint=lambda reason: capture_checkpoint(
                database, chain, reason=reason, logger=logger
            ),
            logger=logger,
            simulated=False,
        )
        sequence = len(database.list_handoffs(chain.id)) + 1
        return manager.run(sequence=sequence)
    finally:
        server.stop()
        database.release_lock(chain.id)


def _overrides(config: RelayConfig) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if config.testing.context_window_override:
        overrides["model_context_window"] = config.testing.context_window_override
    if config.testing.auto_compact_token_limit:
        overrides["model_auto_compact_token_limit"] = config.testing.auto_compact_token_limit
    return overrides


def pause_chain(database: Database, chain: Chain, reason: str = "paused by operator") -> Chain:
    """Pause a chain so the controller stops issuing model requests."""
    if chain.state in ("complete", "stopped", "failed"):
        return chain
    return database.transition_chain(chain.id, "paused", reason=reason)


def resume_chain(database: Database, chain: Chain) -> Chain:
    """Resume a paused chain."""
    if chain.state != "paused":
        raise RuntimeError(f"chain {chain.id} is {chain.state}, not paused")
    return database.transition_chain(chain.id, "running")


def stop_chain(database: Database, chain: Chain, reason: str = "stopped by operator") -> Chain:
    """Stop a chain permanently."""
    if chain.state in ("complete", "stopped", "failed"):
        return chain
    return database.transition_chain(chain.id, "stopped", reason=reason)


def doctor(config: RelayConfig) -> dict[str, Any]:
    """Collect environment facts the operator needs before relying on the Relay."""
    report: dict[str, Any] = {}
    try:
        executable = find_codex_executable(config.controller.codex_executable)
        report["codex_executable"] = str(executable)
        report["codex_version"] = codex_version(executable)
        logged_in, status = codex_login_status(executable)
        report["codex_logged_in"] = logged_in
        report["codex_login_status"] = status
    except Exception as error:  # noqa: BLE001 - doctor reports rather than raises
        report["codex_executable"] = None
        report["codex_error"] = str(error)

    try:
        git = GitRepository(Path.cwd())
        report["git_executable"] = git._git  # noqa: SLF001 - doctor needs the resolved path
    except Exception as error:  # noqa: BLE001
        report["git_executable"] = None
        report["git_error"] = str(error)

    report["python"] = str(Path(__import__("sys").executable))
    report["relay_time"] = time.time()
    return report
