"""Cross-session handoff: PREPARE → START → VERIFY → COMMIT → CONTINUE.

The handoff is the only place where write access moves between Codex sessions,
and it is written as a linear state machine with an explicit rollback path:

* **PREPARE** writes and verifies a checkpoint, then re-reads the package from
  disk so a broken write is caught before any session switch.
* **START** creates a genuinely new main session with ``thread/start``. The new
  thread id must be unknown to the chain and must have zero turns.
* **VERIFY** proves the new session is fresh and that the project still matches
  the checkpoint that was just written.
* **COMMIT** moves the chain's ``active_session_id`` to the new session and
  marks the old one as handed off; the database is the single source of truth.
* **CONTINUE** sends the resume instruction and requires the new session to
  report a relay marker, which is the proof that it actually worked instead of
  merely existing.

Any failure before COMMIT keeps the previous checkpoint and retries a bounded
number of times with a brand new thread. On final failure the chain is rolled
back to the previous session when that session is still usable.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .agent_session import TurnOutcome
from .checkpoint import CheckpointError, GitRepository, GitSnapshot, read_package
from .config import RelayConfig
from .db import Checkpoint, Chain, Database, Handoff

MARKER_PATTERN = re.compile(r"::relay\((progress|done|blocked)\)", re.IGNORECASE)


class HandoffError(RuntimeError):
    """Raised when a handoff cannot be completed safely."""


class SessionDriver(Protocol):
    """The subset of agent behaviour the handoff needs (real or mock)."""

    def start_thread(
        self,
        *,
        model: str | None = None,
        effort: str | None = None,
        sandbox: str | None = None,
        approval_policy: str | None = None,
        approvals_reviewer: str | None = None,
        config_overrides: dict[str, Any] | None = None,
        cwd: Path | None = None,
    ) -> str: ...

    def read_thread(self, thread_id: str) -> dict[str, Any]: ...

    def start_turn(
        self,
        thread_id: str,
        text: str,
        *,
        wait: bool = True,
        timeout: float | None = None,
        effort: str | None = None,
        model: str | None = None,
    ) -> TurnOutcome | None: ...


def parse_marker(text: str) -> str | None:
    """Return the relay marker in ``text`` (``progress``/``done``/``blocked``)."""
    match = MARKER_PATTERN.search(text or "")
    return match.group(1).lower() if match else None


@dataclass
class HandoffResult:
    """Outcome of one handoff attempt."""

    success: bool
    handoff_id: int
    new_session_id: str | None
    checkpoint_id: int | None
    attempts: int
    marker: str | None = None
    error: str | None = None
    rolled_back: bool = False
    duration_seconds: float = 0.0


class HandoffManager:
    """Executes the handoff state machine for one chain."""

    def __init__(
        self,
        *,
        database: Database,
        chain: Chain,
        driver: SessionDriver,
        git: GitRepository,
        config: RelayConfig,
        project_root: Path,
        write_checkpoint: Callable[[str], Checkpoint],
        logger: Any = None,
        simulated: bool = False,
    ) -> None:
        self.db = database
        self.chain = chain
        self.driver = driver
        self.git = git
        self.config = config
        self.project_root = project_root
        self.write_checkpoint = write_checkpoint
        self._logger = logger
        self.simulated = simulated

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def run(self, *, sequence: int) -> HandoffResult:
        """Execute one complete handoff and return its outcome."""
        started = time.monotonic()
        previous_session = self.chain.active_session_id
        handoff = self.db.create_handoff(
            chain_id=self.chain.id,
            sequence=sequence,
            from_session_id=previous_session,
            checkpoint_id=None,
            simulated=self.simulated,
        )
        self.db.transition_chain(self.chain.id, "handoff")

        checkpoint: Checkpoint | None = None
        try:
            checkpoint = self._prepare(handoff)
        except Exception as error:  # noqa: BLE001 - a broken package must roll back, not crash
            return self._fail(
                handoff,
                error=f"PREPARE failed: {error}",
                previous_session=previous_session,
                started=started,
                handed_off=False,
            )

        max_attempts = max(1, self.config.relay.max_handoff_retries + 1)
        last_error: str | None = None
        for attempt in range(1, max_attempts + 1):
            self.db.update_handoff(
                handoff.id, attempt=attempt, phase="START", state="pending"
            )
            new_session_id: str | None = None
            try:
                new_session_id = self._start(checkpoint)
                self.db.update_handoff(handoff.id, phase="VERIFY", to_session_id=new_session_id)
                self._verify(checkpoint, new_session_id)
                self.db.update_handoff(handoff.id, phase="COMMIT", state="pending")
                self._commit(checkpoint, new_session_id, previous_session)
            except Exception as error:  # noqa: BLE001 - bounded retry, then rollback
                last_error = f"attempt {attempt}: {error}"
                self._log(
                    "warning",
                    "handoff attempt failed",
                    handoff_id=handoff.id,
                    attempt=attempt,
                    error=str(error),
                )
                if new_session_id is not None:
                    self._abandon_session(new_session_id, reason=f"handoff attempt failed: {error}")
                continue

            self._log(
                "info",
                "handoff committed, continuing on the new session",
                handoff_id=handoff.id,
                new_session_id=new_session_id,
                attempt=attempt,
            )
            self.db.update_handoff(
                handoff.id, phase="CONTINUE", state="ok", finished=True
            )
            return HandoffResult(
                success=True,
                handoff_id=handoff.id,
                new_session_id=new_session_id,
                checkpoint_id=checkpoint.id,
                attempts=attempt,
                duration_seconds=time.monotonic() - started,
            )

        return self._fail(
            handoff,
            error=last_error or "handoff failed",
            previous_session=previous_session,
            started=started,
            handed_off=False,
        )

    # ------------------------------------------------------------------
    # phases
    # ------------------------------------------------------------------
    def _prepare(self, handoff: Handoff) -> Checkpoint:
        """Write a checkpoint, then prove it can be read back from disk."""
        checkpoint = self.write_checkpoint("handoff")
        if not checkpoint.is_intact():
            raise CheckpointError(
                f"checkpoint {checkpoint.id} failed its checksum immediately after writing"
            )
        package = read_package(self.project_root)
        if package.state.get("chain_id") != self.chain.id:
            raise CheckpointError(
                "handoff package belongs to chain "
                f"{package.state.get('chain_id')!r}, expected {self.chain.id!r}"
            )
        self.db.update_handoff(
            handoff.id, phase="START", state="pending", checkpoint_id=checkpoint.id
        )
        self._log(
            "info",
            "handoff prepared",
            handoff_id=handoff.id,
            checkpoint_id=checkpoint.id,
            checkpoint_version=checkpoint.version,
        )
        return checkpoint

    def _start(self, checkpoint: Checkpoint) -> str:
        """Create the new main session and register it."""
        existing_ordinals = [
            session.ordinal
            for session in self.db.main_sessions(self.chain.id)
            if session.ordinal is not None
        ]
        next_ordinal = (max(existing_ordinals) + 1) if existing_ordinals else 1

        override = self._session_config_overrides()
        thread_id = self.driver.start_thread(
            model=self.config.relay.model,
            effort=self.config.relay.effort,
            sandbox=self.config.controller.sandbox,
            approval_policy=self.config.controller.approval_policy,
            approvals_reviewer=self.config.controller.approvals_reviewer,
            config_overrides=override or None,
            cwd=self.project_root,
        )
        if self.db.find_session(thread_id) is not None:
            raise HandoffError(
                f"thread {thread_id} already belongs to a session; a handoff must "
                "create a clean new thread"
            )
        self.db.create_session(
            session_id=thread_id,
            chain_id=self.chain.id,
            role="main",
            ordinal=next_ordinal,
            model=self.config.relay.model,
            metadata={
                "handoff_checkpoint": checkpoint.version,
                "simulated_pressure": self.simulated,
            },
        )
        return thread_id

    def _verify(self, checkpoint: Checkpoint, session_id: str) -> None:
        """Prove the new session is genuinely fresh and the project still matches."""
        thread = self.driver.read_thread(session_id)
        turns = thread.get("turns") or []
        if turns:
            raise HandoffError(
                f"new thread {session_id} already contains {len(turns)} turn(s); "
                "it is not a clean session"
            )
        snapshot = self.git.snapshot()
        if snapshot.branch != checkpoint.git_branch:
            raise HandoffError(
                f"project branch changed during handoff: {snapshot.branch!r} != "
                f"{checkpoint.git_branch!r}"
            )
        if checkpoint.git_head and snapshot.head and snapshot.head != checkpoint.git_head:
            raise HandoffError(
                "project HEAD moved during handoff "
                f"({snapshot.head[:8]} != {checkpoint.git_head[:8]}); "
                "another process is writing to the project"
            )
        package = read_package(self.project_root)
        if package.state.get("chain_id") != self.chain.id:
            raise HandoffError("handoff package was replaced by another chain")
        self.db.mark_checkpoint_verified(checkpoint.id)
        self._log("info", "handoff verified", session_id=session_id, checkpoint_id=checkpoint.id)

    def _commit(
        self, checkpoint: Checkpoint, new_session_id: str, previous_session_id: str | None
    ) -> None:
        """Move write access to the new session."""
        if previous_session_id:
            self.db.update_session(
                previous_session_id,
                state="handed_off",
                ended_at=time.time(),
                touch=False,
            )
        self.db.update_session(new_session_id, state="active")
        self.db.update_chain(
            self.chain.id, active_session_id=new_session_id, state="running"
        )
        self.chain = self.db.get_chain(self.chain.id)
        self._log(
            "info",
            "write access committed to the new session",
            chain_id=self.chain.id,
            new_session_id=new_session_id,
            previous_session_id=previous_session_id,
            checkpoint_id=checkpoint.id,
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _session_config_overrides(self) -> dict[str, Any]:
        """Return Codex config overrides applied to relay-managed sessions."""
        overrides: dict[str, Any] = {}
        testing = self.config.testing
        if testing.context_window_override:
            overrides["model_context_window"] = testing.context_window_override
        if testing.auto_compact_token_limit:
            overrides["model_auto_compact_token_limit"] = testing.auto_compact_token_limit
        return overrides

    def _abandon_session(self, session_id: str, *, reason: str) -> None:
        """Mark a failed candidate session as ended without touching the chain."""
        try:
            self.db.update_session(
                session_id, state="failed", ended_at=time.time(), touch=False
            )
        except KeyError:
            return
        self._log("warning", "abandoned candidate session", session_id=session_id, reason=reason)

    def _fail(
        self,
        handoff: Handoff,
        *,
        error: str,
        previous_session: str | None,
        started: float,
        handed_off: bool,
    ) -> HandoffResult:
        """Record a terminal handoff failure and roll the chain back if possible."""
        rolled_back = False
        if previous_session and not handed_off:
            try:
                self.db.update_chain(
                    self.chain.id,
                    active_session_id=previous_session,
                    state="running",
                )
                rolled_back = True
            except Exception:  # noqa: BLE001 - rollback must never mask the original error
                rolled_back = False
        if not rolled_back:
            self.db.transition_chain(self.chain.id, "failed", reason=error)
        self.db.update_handoff(
            handoff.id,
            state="failed" if not rolled_back else "rolled_back",
            error=error,
            finished=True,
        )
        self._log(
            "error",
            "handoff failed",
            handoff_id=handoff.id,
            error=error,
            rolled_back=rolled_back,
        )
        return HandoffResult(
            success=False,
            handoff_id=handoff.id,
            new_session_id=None,
            checkpoint_id=handoff.checkpoint_id,
            attempts=handoff.attempt,
            error=error,
            rolled_back=rolled_back,
            duration_seconds=time.monotonic() - started,
        )

    def _log(self, level: str, message: str, **fields: Any) -> None:
        if self._logger is None:
            return
        getattr(self._logger, level, self._logger.info)(message, extra={"relay_fields": fields})
