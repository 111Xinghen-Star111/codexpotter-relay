"""A fake Codex driver for Level 1 tests.

The mock keeps the same surface as :class:`relay.agent_session.CodexAgent` but
never contacts the model. It still does real, observable work — it advances the
ledger, writes a file and commits — so the database, checkpoint, git and
validation code paths are exercised for real while token usage is synthetic.

Injection points exist for the failure modes the requirements call out:
repeated handoff failures, corrupted ledgers and turn errors.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent_session import ThreadState, TokenUsage, TurnOutcome
from .appserver import AppServerError
from .checkpoint import (
    CheckpointError,
    GitRepository,
    next_pending_task,
    read_package,
    write_package,
)


@dataclass
class MockBehaviour:
    """Knobs that drive the fake agent's behaviour."""

    context_window: int = 200_000
    tokens_per_turn: int = 40_000
    #: Fail this many ``start_thread`` calls before succeeding (tests retry logic).
    fail_thread_starts: int = 0
    #: When True the first ``start_thread`` always succeeds, so a later handoff
    #: can be the thing that fails instead of the initial session.
    always_allow_first_thread: bool = False
    #: Raise a turn error on these zero-based turn indexes.
    fail_turns: tuple[int, ...] = ()
    #: Corrupt ``state.json`` after this zero-based turn index.
    corrupt_ledger_after_turn: int | None = None
    #: Force a marker on the given turn indexes (else derived from the ledger).
    forced_markers: dict[int, str] = field(default_factory=dict)
    #: Simulated wall-clock delay per turn, in seconds.
    turn_delay_seconds: float = 0.0
    #: When True the fake agent never advances the ledger (tests stuck loops).
    stall: bool = False


class MockAgent:
    """Deterministic stand-in for :class:`CodexAgent`."""

    #: The mock never calls the model, so its requests do not consume quota.
    is_mock = True

    def __init__(
        self,
        *,
        project_root: Path,
        behaviour: MockBehaviour | None = None,
        logger: Any = None,
        git_executable: str | None = None,
    ) -> None:
        self.project_root = project_root
        self.behaviour = behaviour or MockBehaviour()
        self._logger = logger
        self.git = GitRepository(project_root, git_executable=git_executable)
        self._threads: dict[str, ThreadState] = {}
        self._thread_turns: dict[str, list[str]] = {}
        self._lock = threading.Lock()
        self._run_token = uuid.uuid4().hex[:6]
        self._thread_counter = 0
        self._turn_counter = 0
        self._turn_index = 0
        self._remaining_start_failures = self.behaviour.fail_thread_starts
        #: Session ids in creation order; used by tests to assert A/B/C/D.
        self.created_threads: list[str] = []

    # ------------------------------------------------------------------
    # SessionDriver protocol
    # ------------------------------------------------------------------
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
    ) -> str:
        """Return a fresh mock thread id, honouring the injected failure count."""
        with self._lock:
            attempt_index = self._thread_counter
        is_first_attempt = attempt_index == 0 and not self.created_threads
        if self._remaining_start_failures > 0 and not (
            self.behaviour.always_allow_first_thread and is_first_attempt
        ):
            self._remaining_start_failures -= 1
            raise AppServerError("injected thread/start failure")
        with self._lock:
            self._thread_counter += 1
            thread_id = f"mock-thread-{self._run_token}-{self._thread_counter:03d}"
            self._threads[thread_id] = ThreadState(thread_id=thread_id)
            self._thread_turns[thread_id] = []
            self.created_threads.append(thread_id)
        self._log("info", "mock thread created", thread_id=thread_id)
        return thread_id

    def read_thread(self, thread_id: str) -> dict[str, Any]:
        """Return a thread snapshot in the shape ``thread/read`` uses."""
        with self._lock:
            turns = list(self._thread_turns.get(thread_id, []))
        return {
            "id": thread_id,
            "cwd": str(self.project_root),
            "turns": [{"id": turn_id, "status": "completed"} for turn_id in turns],
        }

    def attach_thread(self, thread_id: str, **_: Any) -> dict[str, Any]:
        """Reattach a mock thread; raises when the thread is unknown to the run."""
        if thread_id not in self._threads:
            raise RuntimeError(f"mock thread not found: {thread_id}")
        return self.read_thread(thread_id)

    def thread_state(self, thread_id: str) -> ThreadState:
        """Return the mock thread's live state."""
        with self._lock:
            state = self._threads.get(thread_id)
            if state is None:
                state = ThreadState(thread_id=thread_id)
                self._threads[thread_id] = state
                self._thread_turns[thread_id] = []
            return state

    def start_turn(
        self,
        thread_id: str,
        text: str,
        *,
        wait: bool = True,
        timeout: float | None = None,
        effort: str | None = None,
        model: str | None = None,
    ) -> TurnOutcome | None:
        """Advance the ledger and return a synthetic turn outcome."""
        with self._lock:
            turn_index = self._turn_index
            self._turn_index += 1
            self._turn_counter += 1
            turn_id = f"mock-turn-{self._run_token}-{self._turn_counter:03d}"

        if turn_index in self.behaviour.fail_turns:
            raise AppServerError(f"injected turn failure on turn {turn_index}")

        if self.behaviour.turn_delay_seconds:
            time.sleep(self.behaviour.turn_delay_seconds)

        marker = self.behaviour.forced_markers.get(turn_index)
        if marker is None and not self.behaviour.stall:
            marker = self._complete_one_task()

        state = self.thread_state(thread_id)
        usage = TokenUsage(
            input_tokens=min(
                self.behaviour.context_window,
                (turn_index + 1) * self.behaviour.tokens_per_turn,
            ),
            cached_input_tokens=0,
            output_tokens=512,
            total_tokens=(turn_index + 1) * (self.behaviour.tokens_per_turn + 512),
            context_window=self.behaviour.context_window,
            reported_at=time.time(),
        )
        with state.condition:
            state.turn_count += 1
            state.completed_turn_ids.append(turn_id)
            state.active_turn_id = None
            state.last_turn_status = "completed"
            state.last_agent_text = f"mock work finished.\n::{marker}" if marker else "mock work finished."
            state.usage = usage
            state.condition.notify_all()
        with self._lock:
            self._thread_turns.setdefault(thread_id, []).append(turn_id)

        if (
            self.behaviour.corrupt_ledger_after_turn is not None
            and turn_index == self.behaviour.corrupt_ledger_after_turn
        ):
            self._corrupt_ledger()

        return TurnOutcome(
            thread_id=thread_id,
            turn_id=turn_id,
            status="completed",
            final_text=state.last_agent_text or "",
            usage=usage,
        )

    def is_turn_active(self, thread_id: str) -> bool:
        """The mock is synchronous, so no turn is ever active between calls."""
        return False

    def interrupt_turn(self, thread_id: str, turn_id: str | None = None) -> None:
        """No-op for the mock driver."""

    # ------------------------------------------------------------------
    # fake work
    # ------------------------------------------------------------------
    def _complete_one_task(self) -> str:
        """Mark the next pending task done, write a file and commit."""
        try:
            package = read_package(self.project_root)
        except CheckpointError:
            return "blocked"
        state = package.state
        task = next_pending_task(state)
        if task is None:
            return "done"
        task["status"] = "done"
        task["evidence"] = f"mock evidence for {task['id']} at {int(time.time())}"
        remaining = next_pending_task(state)
        state["next_task_id"] = remaining["id"] if remaining else None
        state["handoff_count"] = state.get("handoff_count", 0)
        write_package(
            self.project_root,
            type(package)(handoff_md=package.handoff_md, state=state),
        )
        work_file = self.project_root / "work" / f"{task['id']}.txt"
        work_file.parent.mkdir(parents=True, exist_ok=True)
        work_file.write_text(f"{task['id']} {task['title']}\n", encoding="utf-8")
        self.git.commit_all(f"feat: complete {task['id']}")
        return "done" if remaining is None else "progress"

    def _corrupt_ledger(self) -> None:
        state_path = self.project_root / ".relay" / "state.json"
        state_path.write_text(json.dumps({"schema": 1, "broken": True}), encoding="utf-8")

    def _log(self, severity: str, message: str, **fields: Any) -> None:
        if self._logger is None:
            return
        getattr(self._logger, severity, self._logger.info)(message, extra={"relay_fields": fields})


def clone_behaviour(source: MockBehaviour, **changes: Any) -> MockBehaviour:
    """Return a copy of ``source`` with ``changes`` applied."""
    return dataclasses.replace(source, **changes)
