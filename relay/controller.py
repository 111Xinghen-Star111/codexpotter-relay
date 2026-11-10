"""The Relay Controller: the loop that owns a work chain end to end.

Responsibilities, in order of importance:

1. Keep exactly one writer per chain (database execution lock + heartbeat).
2. Keep the ledger and checkpoints truthful: a checkpoint is written from the
   files on disk plus the live git state, never from memory alone.
3. Hand off to a brand new main session when context pressure demands it, at a
   safe boundary (between turns, never mid-write).
4. Stop for the right reason: objective complete, operator pause, budget
   exhausted, repeated failure, or cancellation.
5. Leave enough in the database that a restarted controller can resume the
   chain from its newest valid checkpoint.

The controller is deliberately synchronous: one chain, one active turn at a
time. Concurrency lives one level down, in the app-server reader thread.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .agent_session import SessionError, TurnOutcome
from .appserver import AppServerError
from .checkpoint import (
    CheckpointError,
    evidence_to_text,
    GitRepository,
    GitSnapshot,
    HandoffPackage,
    all_tasks_done,
    build_handoff_markdown,
    default_state,
    ensure_scaffold,
    next_pending_task,
    read_package,
    write_package,
)
from .config import RelayConfig
from .context_monitor import ContextMonitor, PressureDecision, PressureSample
from .db import Checkpoint, Chain, Database, canonical_json
from .handoff import HandoffManager, HandoffResult, SessionDriver, parse_marker
from .ops import capture_checkpoint
from .prompts import (
    build_continue_prompt,
    build_initial_prompt,
    build_repair_prompt,
    build_resume_prompt,
)
from .validator import TaskValidator, ValidationReport

TERMINAL_CHAIN_STATES = frozenset({"complete", "stopped", "failed"})

#: How many consecutive turn failures are tolerated on one session before the
#: controller declares the chain failed instead of retrying forever.
MAX_TURN_FAILURES = 3


class BudgetExceeded(RuntimeError):
    """Raised when a configured budget is exhausted."""


@dataclass(frozen=True)
class Budget:
    """Hard limits for one controller run."""

    max_turns: int
    max_handoffs: int
    max_model_requests: int
    max_token_total: int | None = None


@dataclass
class ChainRunResult:
    """What happened during one controller run."""

    chain_id: str
    state: str
    reason: str
    turns: int
    handoffs: int
    model_requests: int
    sessions: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    validation: ValidationReport | None = None
    report_path: Path | None = None

    @property
    def completed(self) -> bool:
        """Return True when the chain reached the ``complete`` state."""
        return self.state == "complete"


class RelayController:
    """Drives one chain to completion (or to a documented stop)."""

    def __init__(
        self,
        *,
        config: RelayConfig,
        database: Database,
        chain_id: str,
        driver: SessionDriver,
        logger: Any,
        simulated_ratio: float | None = None,
        cancel_event: threading.Event | None = None,
        budget: Budget | None = None,
        git_executable: str | None = None,
        report_writer: Callable[[str], Path] | None = None,
    ) -> None:
        self.config = config
        self.db = database
        self.chain_id = chain_id
        self.driver = driver
        self._logger = logger
        self._cancel = cancel_event or threading.Event()
        self.git_executable = git_executable
        self._report_writer = report_writer

        relay = config.relay
        self.budget = budget or Budget(
            max_turns=relay.max_turns,
            max_handoffs=relay.max_handoffs,
            max_model_requests=relay.max_model_requests,
            max_token_total=relay.token_budget,
        )
        self.monitor = ContextMonitor(
            relay,
            simulated_ratio=simulated_ratio,
            fallback_turn_limit=relay.fallback_turn_limit,
        )
        self._turns_run = 0
        self._turns_since_checkpoint = 0
        self._state: dict[str, Any] | None = None
        self._last_validation: ValidationReport | None = None
        self._started_at = time.monotonic()
        #: True only for the mock driver; a real session always costs quota.
        self._driver_is_mock = bool(getattr(driver, "is_mock", False))
        #: Task ids already observed as ``done``; used to checkpoint progress.
        self._done_task_ids: set[str] = set()
        #: Session labels already pushed to Codex, and sessions that got metadata.
        self._session_labels: dict[str, str] = {}
        self._metadata_sent: set[str] = set()

    # ------------------------------------------------------------------
    # chain construction
    # ------------------------------------------------------------------
    @classmethod
    def create_chain(
        cls,
        *,
        database: Database,
        project_root: Path,
        goal: str,
        tasks: list[dict[str, Any]],
        acceptance_commands: list[str],
        config: RelayConfig,
        mode: str = "production",
        test_level: str | None = None,
        work_branch: str | None = None,
        simulated: bool = False,
    ) -> Chain:
        """Create the chain row and its initial ledger before any session exists."""
        if not tasks:
            tasks = [{"id": "T1", "title": goal.strip(), "status": "pending"}]
        chain_id = f"ch_{uuid.uuid4().hex[:12]}"
        branch = work_branch or f"relay/{chain_id}"
        project_root.mkdir(parents=True, exist_ok=True)
        ensure_scaffold(project_root)
        config_snapshot = {
            "relay": {
                "checkpoint_threshold": config.relay.checkpoint_threshold,
                "prepare_threshold": config.relay.prepare_threshold,
                "context_threshold": config.relay.context_threshold,
                "urgent_threshold": config.relay.urgent_threshold,
                "auto_handoff": config.relay.auto_handoff,
                "model": config.relay.model,
                "effort": config.relay.effort,
            },
            "tasks": tasks,
            "acceptance": acceptance_commands,
            "simulated": simulated,
        }
        chain = database.create_chain(
            chain_id=chain_id,
            project_root=project_root,
            goal=goal,
            mode=mode,
            work_branch=branch,
            config_json=canonical_json(config_snapshot),
            test_level=test_level,
            simulated=simulated,
        )
        state = default_state(
            chain_id=chain_id,
            goal=goal,
            tasks=tasks,
            acceptance_commands=acceptance_commands,
        )
        write_package(
            project_root,
            HandoffPackage(
                handoff_md=build_handoff_markdown(
                    chain_id=chain_id,
                    goal=goal,
                    original_request=goal,
                    context_notes="Initial session: no previous session exists yet.",
                    state=state,
                    git_snapshot=GitSnapshot(
                        branch=branch,
                        head=None,
                        dirty=False,
                        status_text="",
                        has_commits=False,
                    ),
                    handoff_sequence=0,
                ),
                state=state,
            ),
        )
        database.append_event(
            event_key=f"{chain_id}:chain-created",
            kind="chain_created",
            source="controller",
            payload={"goal": goal, "mode": mode, "test_level": test_level},
            chain_id=chain_id,
            simulated=simulated,
        )
        return chain

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
    def run(self) -> ChainRunResult:
        """Execute the chain until it stops for a recorded reason."""
        chain = self.db.get_chain(self.chain_id)
        if not self.db.acquire_lock(self.chain_id):
            owner = self.db.lock_owner(self.chain_id)
            raise RuntimeError(f"chain {self.chain_id} already has a live writer: {owner}")
        try:
            return self._run_locked(chain)
        finally:
            self.db.release_lock(self.chain_id)

    def request_stop(self, *, reason: str = "cancelled by operator") -> None:
        """Ask the loop to stop at the next safe point."""
        self._cancel.set()
        self._stop_reason = reason

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _run_locked(self, chain: Chain) -> ChainRunResult:
        project_root = Path(chain.project_root)
        git = GitRepository(project_root, git_executable=self.git_executable)
        self._prepare_environment(git, chain)

        chain = self._recover_inflight_handoff(chain)
        if chain.state == "pending":
            chain = self.db.transition_chain(chain.id, "running")

        self._state = self._load_or_restore_state(chain)
        if self.db.latest_checkpoint(chain.id) is None:
            # A chain that never crosses a pressure threshold would otherwise
            # finish without any checkpoint. Writing one at the start gives
            # crash recovery and the report a git-anchored baseline.
            self._write_checkpoint(chain, git, reason="initial")

        if chain.active_session_id:
            chain = self._reattach_active_session(chain)
        sessions_seen: set[str] = {session.id for session in self.db.main_sessions(chain.id)}
        stop_reason = "loop finished"
        validation: ValidationReport | None = None
        turn_failures = 0

        while True:
            chain = self.db.get_chain(self.chain_id)
            if chain.state in TERMINAL_CHAIN_STATES:
                stop_reason = chain.stop_reason or chain.state
                break
            if chain.state == "paused":
                stop_reason = chain.stop_reason or "paused by operator"
                break
            if self._cancel.is_set():
                self.db.transition_chain(chain.id, "stopped", reason="cancelled by operator")
                stop_reason = "cancelled by operator"
                break
            try:
                self._check_budgets(chain)
            except BudgetExceeded as error:
                self.db.transition_chain(chain.id, "stopped", reason=str(error))
                stop_reason = str(error)
                break
            if not self.db.heartbeat_lock(self.chain_id):
                stop_reason = "lost the chain execution lock"
                break

            session_id = chain.active_session_id
            prompt = ""
            try:
                if session_id is None:
                    session_id = self._start_initial_session(chain)
                sessions_seen.add(session_id)
                prompt = self._build_prompt(chain, session_id)
                outcome = self._run_work_turn(chain, session_id, prompt)
            except (SessionError, AppServerError) as error:
                turn_failures += 1
                self._log(
                    "error",
                    "work turn failed",
                    chain_id=chain.id,
                    session_id=session_id,
                    failures=turn_failures,
                    error=str(error),
                )
                if turn_failures >= MAX_TURN_FAILURES:
                    self.db.transition_chain(
                        chain.id,
                        "failed",
                        reason=f"{turn_failures} consecutive turn failures: {error}",
                    )
                    stop_reason = f"turn failures: {error}"
                    break
                time.sleep(min(2.0 * turn_failures, 10.0))
                continue
            turn_failures = 0
            self._turns_run += 1
            self._turns_since_checkpoint += 1

            self._state = self._reload_state(chain)
            self._apply_session_label(chain, session_id, self._state, git=git)
            marker = parse_marker(outcome.final_text)
            self._record_turn_event(chain, session_id, outcome, marker)

            if marker == "blocked":
                self.db.transition_chain(
                    chain.id,
                    "paused",
                    reason="agent reported a blocker; a human decision is required",
                )
                stop_reason = "blocked by agent, awaiting human decision"
                break

            validation = self._validate_if_finished(chain, self._state, git)
            if validation is not None and validation.complete:
                reason = validation.summary()
                self.db.transition_chain(chain.id, "complete", reason=reason)
                stop_reason = reason
                break

            decision = self._sample_pressure(chain, session_id)
            handoffs_done = len(self.db.list_handoffs(chain.id))
            will_handoff = (
                decision.needs_handoff
                and self.config.relay.auto_handoff
                and handoffs_done < self.budget.max_handoffs
            )
            newly_done = self._take_newly_done_tasks(self._state)
            if newly_done and not will_handoff:
                # Anchor each finished task in a checkpoint so a crash together
                # with a damaged ledger cannot roll completed work back.
                self._write_checkpoint(chain, git, reason=f"task:{newly_done[-1]}")
                self._turns_since_checkpoint = 0
            if (
                decision.at_least_checkpoint
                and self._turns_since_checkpoint > 0
                and not will_handoff
            ):
                self._write_checkpoint(chain, git, reason=f"pressure:{decision.level}")
                self._turns_since_checkpoint = 0

            if decision.needs_handoff and self.config.relay.auto_handoff:
                if handoffs_done >= self.budget.max_handoffs:
                    self.db.transition_chain(
                        chain.id,
                        "stopped",
                        reason=f"handoff budget exhausted ({self.budget.max_handoffs})",
                    )
                    stop_reason = "handoff budget exhausted"
                    break
                result = self._do_handoff(chain, git, decision)
                if not result.success:
                    stop_reason = f"handoff failed: {result.error}"
                    break
                if result.new_session_id:
                    sessions_seen.add(result.new_session_id)

        chain = self.db.get_chain(self.chain_id)
        report_path = self._write_report(chain)
        return ChainRunResult(
            chain_id=chain.id,
            state=chain.state,
            reason=chain.stop_reason or stop_reason,
            turns=self._turns_run,
            handoffs=len(self.db.list_handoffs(chain.id)),
            model_requests=self.db.count_model_requests(chain.id),
            sessions=sorted(sessions_seen),
            duration_seconds=time.monotonic() - self._started_at,
            validation=validation,
            report_path=report_path,
        )

    # ------------------------------------------------------------------
    # recovery
    # ------------------------------------------------------------------
    def _recover_inflight_handoff(self, chain: Chain) -> Chain:
        """Roll a crash-interrupted handoff back so the chain can continue.

        A controller can die between START and COMMIT. Write access then still
        belongs to the previous session, so the safe recovery is to mark the
        unfinished handoff as failed and return the chain to ``running``; the
        checkpoint from PREPARE stays untouched.
        """
        if chain.state != "handoff":
            return chain
        latest = self.db.latest_handoff(chain.id)
        if latest is not None and latest.state == "pending":
            self.db.update_handoff(
                latest.id,
                state="failed",
                error="controller restarted while the handoff was in flight",
                finished=True,
            )
            self.db.append_event(
                event_key=f"{chain.id}:handoff-recovered:{latest.id}",
                kind="handoff_recovered",
                source="controller",
                payload={"sequence": latest.sequence, "phase": latest.phase},
                chain_id=chain.id,
            )
            self._log(
                "warning",
                "rolled back an interrupted handoff",
                chain_id=chain.id,
                sequence=latest.sequence,
                phase=latest.phase,
            )
        return self.db.transition_chain(
            chain.id, "running", reason="recovered from interrupted handoff"
        )

    def _reattach_active_session(self, chain: Chain) -> Chain:
        """Load the chain's live thread into the current app-server process.

        A restarted controller gets a fresh app server that has never seen the
        existing thread, so every ``turn/start`` would fail. Resuming keeps the
        same session (no new context). If the thread is really gone, the chain
        continues on a brand new session instead of silently replanning from
        stale state.
        """
        session_id = chain.active_session_id
        if not session_id:
            return chain
        attach = getattr(self.driver, "attach_thread", None)
        if attach is None:
            return chain
        try:
            attach(
                session_id,
                cwd=Path(chain.project_root),
                sandbox=self.config.controller.sandbox,
                approval_policy=self.config.controller.approval_policy,
                approvals_reviewer=self.config.controller.approvals_reviewer,
                config_overrides=self._session_config_overrides() or None,
            )
        except Exception as error:  # noqa: BLE001 - a lost thread is recoverable
            self._log(
                "warning",
                "active session could not be reattached; starting a new one",
                chain_id=chain.id,
                session_id=session_id,
                error=str(error),
            )
            self.db.update_session(
                session_id, state="failed", ended_at=time.time(), touch=False
            )
            self.db.append_event(
                event_key=f"{chain.id}:session-lost:{session_id}",
                kind="session_lost",
                source="controller",
                payload={"session_id": session_id, "error": str(error)},
                chain_id=chain.id,
                session_id=session_id,
            )
            self.db.update_chain(chain.id, active_session_id=None)
            return self.db.get_chain(chain.id)
        self._log(
            "info",
            "active session reattached",
            chain_id=chain.id,
            session_id=session_id,
        )
        return chain

    # ------------------------------------------------------------------
    # environment
    # ------------------------------------------------------------------
    def _prepare_environment(self, git: GitRepository, chain: Chain) -> None:
        """Make sure the project is a git repository on the chain's work branch."""
        project_root = Path(chain.project_root)
        if not project_root.is_dir():
            raise CheckpointError(f"project directory does not exist: {project_root}")
        ensure_scaffold(project_root)
        from .codex_env import ensure_python_on_path

        ensure_python_on_path()
        git.ensure_identity()
        git.ensure_branch(chain.work_branch)
        if not git.snapshot().has_commits:
            git.commit_all("chore: initialise relay work chain")
        else:
            # Keep the repository clean without touching the user's own changes:
            # only the relay-generated .gitignore entry is committed.
            git.commit_paths("chore: ignore relay bookkeeping", [".gitignore"])
        self._log(
            "info",
            "environment ready",
            chain_id=chain.id,
            branch=git.snapshot().branch,
            project_root=str(project_root),
        )

    # ------------------------------------------------------------------
    # ledger handling
    # ------------------------------------------------------------------
    def _load_or_restore_state(self, chain: Chain) -> dict[str, Any]:
        """Read the ledger from disk, falling back to the newest valid checkpoint."""
        project_root = Path(chain.project_root)
        try:
            return read_package(project_root).state
        except CheckpointError as error:
            self._log("warning", "handoff package unreadable", error=str(error))
        checkpoint = self.db.latest_valid_checkpoint(chain.id)
        if checkpoint is None:
            raise CheckpointError(
                f"cannot resume chain {chain.id}: no readable handoff package and no "
                "valid checkpoint available"
            )
        package = HandoffPackage(
            handoff_md=checkpoint.handoff_md, state=json.loads(checkpoint.state_json)
        )
        write_package(project_root, package)
        self.db.append_event(
            event_key=f"{chain.id}:state-restored:{checkpoint.id}",
            kind="state_restored",
            source="controller",
            payload={"checkpoint_id": checkpoint.id, "version": checkpoint.version},
            chain_id=chain.id,
        )
        # The restored ledger is older than what the database last recorded, so
        # mirror it back; otherwise task_results would claim work this checkpoint
        # does not contain.
        self._sync_tasks(chain, package.state)
        return package.state

    def _reload_state(self, chain: Chain) -> dict[str, Any]:
        """Reload the ledger after a turn, restoring from checkpoint if needed."""
        project_root = Path(chain.project_root)
        try:
            state = read_package(project_root).state
        except CheckpointError as error:
            self._log("warning", "ledger unreadable after turn", error=str(error))
            checkpoint = self.db.latest_valid_checkpoint(chain.id)
            if checkpoint is None:
                raise
            state = json.loads(checkpoint.state_json)
            write_package(
                project_root,
                HandoffPackage(handoff_md=checkpoint.handoff_md, state=state),
            )
            self.db.append_event(
                event_key=f"{chain.id}:ledger-repaired:{checkpoint.id}:{int(time.time() * 1000)}",
                kind="ledger_repaired",
                source="controller",
                payload={"checkpoint_id": checkpoint.id, "error": str(error)},
                chain_id=chain.id,
            )
        self._sync_tasks(chain, state)
        return state

    def _take_newly_done_tasks(self, state: dict[str, Any]) -> list[str]:
        """Return task ids that became ``done`` since the previous turn."""
        done_now = {
            str(task.get("id"))
            for task in state.get("tasks") or []
            if task.get("status") == "done"
        }
        new_ids = sorted(done_now - self._done_task_ids)
        self._done_task_ids |= done_now
        return new_ids

    def _sync_tasks(self, chain: Chain, state: dict[str, Any]) -> None:
        """Mirror the ledger's task statuses into the database."""
        for task in state.get("tasks") or []:
            status = str(task.get("status", "pending"))
            self.db.upsert_task(
                chain_id=chain.id,
                task_id=str(task.get("id")),
                title=str(task.get("title", "")),
                status=status,
                evidence=evidence_to_text(task.get("evidence")),
                session_id=chain.active_session_id,
                verified=status == "done" and bool(task.get("evidence")),
            )

    def _build_prompt(self, chain: Chain, session_id: str) -> str:
        """Choose the prompt for the upcoming turn based on the session's history."""
        state = self._state or {}
        task = next_pending_task(state)
        if self._last_validation is not None and not self._last_validation.complete:
            return build_repair_prompt(
                project_root=Path(chain.project_root),
                chain_id=chain.id,
                failures=self._last_validation.failures,
                unfinished=self._last_validation.unfinished_tasks,
                acceptance_tails=[
                    result.output_tail for result in self._last_validation.acceptance
                ],
            )
        session = self.db.get_session(session_id)
        if session.request_count == 0:
            if (session.ordinal or 1) <= 1:
                return build_initial_prompt(
                    project_root=Path(chain.project_root),
                    chain_id=chain.id,
                    goal=chain.goal,
                    next_task=task,
                )
            return build_resume_prompt(
                project_root=Path(chain.project_root),
                chain_id=chain.id,
                handoff_sequence=len(self.db.list_handoffs(chain.id)),
                next_task=task,
            )
        return build_continue_prompt(
            project_root=Path(chain.project_root),
            chain_id=chain.id,
            next_task=task,
        )

    # ------------------------------------------------------------------
    # sessions and turns
    # ------------------------------------------------------------------
    def _session_label(self, chain: Chain, ordinal: int, state: dict[str, Any]) -> str:
        """Return the human label shown for this session in Codex's session list."""
        short = chain.id[3:11] if chain.id.startswith("ch_") else chain.id[:8]
        label = f"Relay {short} #{ordinal}"
        task = next_pending_task(state)
        if task:
            title = str(task.get("title", "")).strip()
            if len(title) > 48:
                title = title[:45] + "..."
            label = f"{label} \u00b7 {task['id']} {title}".strip()
        elif all_tasks_done(state):
            label = f"{label} \u00b7 done"
        return label[:120]

    def _apply_session_label(
        self, chain: Chain, session_id: str, state: dict[str, Any], *, git: GitRepository | None = None
    ) -> None:
        """Push a readable name (and git metadata) for a session to Codex.

        This is what makes relay sessions identifiable in the Codex desktop
        session list. It is purely cosmetic, so every failure is logged and
        ignored rather than aborting the chain.
        """
        session = self.db.find_session(session_id)
        if session is None:
            return
        label = self._session_label(chain, session.ordinal or 1, state)
        if self._session_labels.get(session_id) != label:
            setter = getattr(self.driver, "set_thread_name", None)
            if setter is not None:
                try:
                    if setter(session_id, label):
                        self._session_labels[session_id] = label
                except Exception as error:  # noqa: BLE001 - cosmetic only
                    self._log("warning", "session labelling failed", error=str(error))

        if git is not None and session_id not in self._metadata_sent:
            updater = getattr(self.driver, "update_thread_metadata", None)
            if updater is not None:
                try:
                    snapshot = git.snapshot()
                    if updater(
                        session_id,
                        branch=snapshot.branch,
                        sha=snapshot.head,
                        origin_url=git.remote_url(),
                    ):
                        self._metadata_sent.add(session_id)
                except Exception as error:  # noqa: BLE001 - cosmetic only
                    self._log("warning", "session metadata failed", error=str(error))

    def _start_initial_session(self, chain: Chain) -> str:
        """Create the chain's first session, or the next one if a session was lost."""
        existing = [
            session.ordinal
            for session in self.db.main_sessions(chain.id)
            if session.ordinal is not None
        ]
        ordinal = (max(existing) + 1) if existing else 1
        overrides = self._session_config_overrides()
        thread_id = self.driver.start_thread(
            model=self.config.relay.model,
            effort=self.config.relay.effort,
            sandbox=self.config.controller.sandbox,
            approval_policy=self.config.controller.approval_policy,
            approvals_reviewer=self.config.controller.approvals_reviewer,
            config_overrides=overrides or None,
            cwd=Path(chain.project_root),
        )
        self.db.create_session(
            session_id=thread_id,
            chain_id=chain.id,
            role="main",
            ordinal=ordinal,
            model=self.config.relay.model,
            state="active",
            metadata={"simulated_pressure": self.monitor.simulating},
        )
        self.db.update_chain(chain.id, active_session_id=thread_id)
        self.db.append_event(
            event_key=f"{chain.id}:session-started:{thread_id}",
            kind="session_started",
            source="controller",
            payload={"session_id": thread_id, "ordinal": ordinal},
            chain_id=chain.id,
            session_id=thread_id,
            simulated=self.monitor.simulating,
        )
        self._log(
            "info",
            "session started",
            chain_id=chain.id,
            session_id=thread_id,
            ordinal=ordinal,
        )
        self._apply_session_label(chain, thread_id, self._state or {}, git=None)
        return thread_id

    def _run_work_turn(self, chain: Chain, session_id: str, prompt: str) -> TurnOutcome:
        """Send one turn, enforce the request budget, and record the result."""
        self._check_budgets(chain)
        session = self.db.get_session(session_id)
        self.db.record_model_request(
            chain_id=chain.id, session_id=session_id, simulated=self._driver_is_mock
        )
        self.db.update_session(
            session_id, state="writing", request_count=session.request_count + 1
        )
        try:
            outcome = self.driver.start_turn(
                session_id,
                prompt,
                wait=True,
                timeout=self.config.relay.turn_timeout_seconds,
            )
        except SessionError:
            self.db.update_session(session_id, state="active")
            raise
        if outcome is None:
            raise SessionError("turn/start returned no outcome while waiting was requested")
        self._record_usage(session_id, outcome)
        return outcome

    def _record_usage(self, session_id: str, outcome: TurnOutcome) -> None:
        """Persist token usage reported for the turn."""
        fields: dict[str, Any] = {"state": "active"}
        usage = outcome.usage
        if usage is not None:
            fields.update(
                token_input=usage.input_tokens,
                token_cached=usage.cached_input_tokens,
                token_output=usage.output_tokens,
                token_total=usage.total_tokens,
                context_window=usage.context_window,
            )
        self.db.update_session(session_id, **fields)

    def _record_turn_event(
        self, chain: Chain, session_id: str, outcome: TurnOutcome, marker: str | None
    ) -> None:
        """Store one turn result as an immutable event."""
        self.db.append_event(
            event_key=f"{chain.id}:turn:{outcome.turn_id}",
            kind="turn_completed",
            source="controller",
            payload={
                "session_id": session_id,
                "turn_id": outcome.turn_id,
                "status": outcome.status,
                "marker": marker,
                "message_tail": outcome.final_text[-2000:],
            },
            chain_id=chain.id,
            session_id=session_id,
            simulated=self.monitor.simulating,
        )

    # ------------------------------------------------------------------
    # validation, pressure, checkpoints, handoff
    # ------------------------------------------------------------------
    def _validate_if_finished(
        self, chain: Chain, state: dict[str, Any], git: GitRepository
    ) -> ValidationReport | None:
        """Run the acceptance gate once the ledger claims every task is done."""
        if not all_tasks_done(state):
            self._last_validation = None
            return None
        validator = TaskValidator(project_root=Path(chain.project_root), logger=self._logger)
        report = validator.validate(state, git_snapshot=git.snapshot(), run_commands=True)
        self._last_validation = None if report.complete else report
        self.db.append_event(
            event_key=f"{chain.id}:validation:{int(report.checked_at * 1000)}",
            kind="validation",
            source="controller",
            payload={
                "complete": report.complete,
                "summary": report.summary(),
                "pending_human": report.pending_human,
                "failures": report.failures,
                "acceptance": [
                    {
                        "command": result.command,
                        "returncode": result.returncode,
                        "passed": result.passed,
                    }
                    for result in report.acceptance
                ],
            },
            chain_id=chain.id,
        )
        return report

    def _sample_pressure(self, chain: Chain, session_id: str) -> PressureDecision:
        """Sample context pressure for the active session and record it."""
        thread_state = self._thread_state(session_id)
        sample: PressureSample = self.monitor.sample(thread_state)
        decision = self.monitor.decide(
            sample, turns_since_checkpoint=self._turns_since_checkpoint
        )
        self.db.append_event(
            event_key=f"{chain.id}:pressure:{session_id}:{int(time.time() * 1000)}",
            kind="context_pressure",
            source="monitor",
            payload={
                "ratio": sample.ratio,
                "source": sample.source,
                "simulated": sample.simulated,
                "context_used_tokens": sample.context_used_tokens,
                "context_window": sample.context_window,
                "turn_count": sample.turn_count,
                "compaction_events": sample.compaction_events,
                "level": decision.level,
                "reason": decision.reason,
            },
            chain_id=chain.id,
            session_id=session_id,
            simulated=sample.simulated,
        )
        self._log(
            "info",
            "context pressure evaluated",
            chain_id=chain.id,
            session_id=session_id,
            level=decision.level,
            sample=sample.describe(),
        )
        return decision

    def _thread_state(self, session_id: str) -> Any:
        """Return the driver's observation for a thread, when it exposes one."""
        getter = getattr(self.driver, "thread_state", None)
        if getter is None:
            return None
        try:
            return getter(session_id)
        except Exception:  # noqa: BLE001 - monitoring must never break the loop
            return None

    def _write_checkpoint(self, chain: Chain, git: GitRepository, *, reason: str) -> Checkpoint:
        """Snapshot ledger + git state into a new checkpoint version."""
        self._state = self._state or self._load_or_restore_state(chain)
        checkpoint = capture_checkpoint(
            self.db,
            chain,
            reason=reason,
            logger=self._logger,
            git_executable=self.git_executable,
            context_notes=self._context_notes(self._state),
        )
        self._log(
            "info",
            "checkpoint written",
            chain_id=chain.id,
            checkpoint_id=checkpoint.id,
            version=checkpoint.version,
            reason=reason,
        )
        return checkpoint

    def _do_handoff(
        self, chain: Chain, git: GitRepository, decision: PressureDecision
    ) -> HandoffResult:
        """Run the handoff state machine for the next sequence number."""
        sequence = len(self.db.list_handoffs(chain.id)) + 1
        manager = HandoffManager(
            database=self.db,
            chain=chain,
            driver=self.driver,
            git=git,
            config=self.config,
            project_root=Path(chain.project_root),
            write_checkpoint=lambda reason: self._write_checkpoint(chain, git, reason=reason),
            logger=self._logger,
            simulated=decision.sample.simulated,
            label_session=lambda session_id: self._apply_session_label(
                chain, session_id, self._state or {}, git=git
            ),
        )
        self.db.append_event(
            event_key=f"{chain.id}:handoff-start:{sequence}",
            kind="handoff_started",
            source="controller",
            payload={
                "sequence": sequence,
                "trigger_level": decision.level,
                "trigger_reason": decision.reason,
                "simulated": decision.sample.simulated,
            },
            chain_id=chain.id,
            session_id=chain.active_session_id,
            simulated=decision.sample.simulated,
        )
        result = manager.run(sequence=sequence)
        self.db.append_event(
            event_key=f"{chain.id}:handoff-end:{sequence}",
            kind="handoff_finished",
            source="controller",
            payload={
                "sequence": sequence,
                "success": result.success,
                "new_session_id": result.new_session_id,
                "attempts": result.attempts,
                "error": result.error,
                "rolled_back": result.rolled_back,
            },
            chain_id=chain.id,
            session_id=result.new_session_id,
            simulated=decision.sample.simulated,
        )
        if result.success:
            self._turns_since_checkpoint = 0
            self._log(
                "info",
                "handoff finished",
                chain_id=chain.id,
                sequence=sequence,
                new_session_id=result.new_session_id,
                attempts=result.attempts,
            )
        return result

    # ------------------------------------------------------------------
    # budgets and reporting
    # ------------------------------------------------------------------
    def _check_budgets(self, chain: Chain) -> None:
        """Raise ``BudgetExceeded`` when any hard limit has been reached."""
        if self._turns_run >= self.budget.max_turns:
            raise BudgetExceeded(f"turn budget exhausted ({self.budget.max_turns})")
        if self.db.count_model_requests(chain.id) >= self.budget.max_model_requests:
            raise BudgetExceeded(
                f"model request budget exhausted ({self.budget.max_model_requests})"
            )
        if self.budget.max_token_total is not None:
            used = sum(session.token_total for session in self.db.list_sessions(chain.id))
            if used >= self.budget.max_token_total:
                raise BudgetExceeded(
                    f"token budget exhausted ({used} >= {self.budget.max_token_total})"
                )

    def _session_config_overrides(self) -> dict[str, Any]:
        """Codex config overrides applied to relay-managed sessions."""
        overrides: dict[str, Any] = {}
        testing = self.config.testing
        if testing.context_window_override:
            overrides["model_context_window"] = testing.context_window_override
        if testing.auto_compact_token_limit:
            overrides["model_auto_compact_token_limit"] = testing.auto_compact_token_limit
        return overrides

    def _context_notes(self, state: dict[str, Any]) -> str:
        """Compose the "important context" section of the handoff file."""
        notes: list[str] = []
        for decision in (state.get("decisions") or [])[-5:]:
            summary = decision.get("summary") if isinstance(decision, dict) else str(decision)
            if summary:
                notes.append(f"- Decision: {summary}")
        task = next_pending_task(state)
        if task:
            notes.append(f"- Next task: `{task['id']}` — {task.get('title', '')}")
        return "\n".join(notes)

    def _write_report(self, chain: Chain) -> Path | None:
        """Generate the development report for a finished run."""
        if self._report_writer is None:
            return None
        try:
            return self._report_writer(chain.id)
        except Exception as error:  # noqa: BLE001 - reporting must not fail a run
            self._log("error", "report generation failed", chain_id=chain.id, error=str(error))
            return None

    def _log(self, severity: str, message: str, **fields: Any) -> None:
        if self._logger is None:
            return
        getattr(self._logger, severity, self._logger.info)(message, extra={"relay_fields": fields})
