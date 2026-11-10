"""Driving real Codex main sessions through the app server.

A :class:`CodexAgent` owns the app-server connection and tracks per-thread
state (turn count, token usage, compaction events, last agent message). A
:class:`CodexSession` is a thin handle on one thread, which keeps the handoff
code honest: a new session always means a new ``thread/start``, never
``thread/resume`` or ``thread/fork``.

Everything the Relay relies on is an official protocol message:
``thread/start``, ``turn/start``, ``thread/tokenUsage/updated``,
``turn/completed`` and ``thread/compacted``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .appserver import AppServer, AppServerError

#: Turn statuses that mean the turn has finished, in any outcome.
TERMINAL_TURN_STATUSES = frozenset({"completed", "failed", "interrupted", "cancelled"})


class SessionError(RuntimeError):
    """Raised when a Codex session cannot be created or driven."""


@dataclass
class TokenUsage:
    """Token accounting reported by ``thread/tokenUsage/updated``."""

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    context_window: int | None = None
    reported_at: float = 0.0

    @classmethod
    def from_notification(cls, usage: dict[str, Any]) -> "TokenUsage":
        """Build usage from the ``tokenUsage`` payload.

        ``last`` describes the most recent model response, which is the right
        basis for "how full is the context window now"; ``total`` accumulates
        the whole thread.
        """
        last = usage.get("last") or {}
        total = usage.get("total") or {}
        return cls(
            input_tokens=int(last.get("inputTokens") or 0),
            cached_input_tokens=int(last.get("cachedInputTokens") or 0),
            output_tokens=int(last.get("outputTokens") or 0),
            reasoning_output_tokens=int(last.get("reasoningOutputTokens") or 0),
            total_tokens=int(total.get("totalTokens") or 0),
            context_window=usage.get("modelContextWindow"),
            reported_at=time.time(),
        )

    def context_used_tokens(self) -> int:
        """Return the tokens currently occupying the context window."""
        return self.input_tokens + self.cached_input_tokens

    def context_ratio(self) -> float | None:
        """Return the context fill ratio, or None when it cannot be computed."""
        window = self.context_window
        if not window or window <= 0:
            return None
        return min(1.0, self.context_used_tokens() / float(window))


@dataclass
class ThreadState:
    """Live state of one Codex thread as observed through notifications."""

    thread_id: str
    created_at: float = field(default_factory=time.time)
    turn_count: int = 0
    completed_turn_ids: list[str] = field(default_factory=list)
    active_turn_id: str | None = None
    last_turn_status: str | None = None
    last_agent_text: str | None = None
    usage: TokenUsage | None = None
    compaction_events: int = 0
    error_messages: list[str] = field(default_factory=list)
    condition: threading.Condition = field(default_factory=threading.Condition)

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-friendly snapshot for persistence."""
        return {
            "thread_id": self.thread_id,
            "turn_count": self.turn_count,
            "completed_turns": len(self.completed_turn_ids),
            "last_turn_status": self.last_turn_status,
            "compaction_events": self.compaction_events,
            "context_window": self.usage.context_window if self.usage else None,
            "context_used_tokens": self.usage.context_used_tokens() if self.usage else None,
            "context_ratio": self.usage.context_ratio() if self.usage else None,
        }


@dataclass(frozen=True)
class TurnOutcome:
    """Result of one completed turn."""

    thread_id: str
    turn_id: str
    status: str
    final_text: str
    usage: TokenUsage | None


class CodexAgent:
    """Owns an :class:`AppServer` connection and per-thread observations."""

    def __init__(
        self,
        app_server: AppServer,
        *,
        project_root: Path,
        logger: Any = None,
    ) -> None:
        self._app = app_server
        self._project_root = project_root
        self._threads: dict[str, ThreadState] = {}
        self._lock = threading.Lock()
        self._logger = logger
        self._app.add_notification_listener(self._on_notification)

    # ------------------------------------------------------------------
    # thread lifecycle
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
        """Create a brand new main thread and return its id."""
        params: dict[str, Any] = {"cwd": str(cwd or self._project_root)}
        merged_config: dict[str, Any] = dict(config_overrides or {})
        if model:
            params["model"] = model
        if effort:
            # ``thread/start`` has no ``effort`` field; reasoning effort is a
            # Codex config value, so it is applied through the thread config.
            merged_config["model_reasoning_effort"] = effort
        if sandbox:
            params["sandbox"] = sandbox
        if approval_policy:
            params["approvalPolicy"] = approval_policy
        if approvals_reviewer:
            params["approvalsReviewer"] = approvals_reviewer
        if merged_config:
            params["config"] = merged_config
        response = self._app.request("thread/start", params)
        thread = response.get("thread") or {}
        thread_id = thread.get("id")
        if not thread_id:
            raise SessionError(f"thread/start returned no thread id: {response}")
        with self._lock:
            self._threads[thread_id] = ThreadState(thread_id=thread_id)
        self._log(
            "info",
            "started new main session",
            thread_id=thread_id,
            model=response.get("model"),
            cwd=response.get("cwd"),
        )
        return thread_id

    def read_thread(self, thread_id: str) -> dict[str, Any]:
        """Read a thread snapshot (turns, status, cwd) from the app server."""
        response = self._app.request("thread/read", {"threadId": thread_id})
        return response.get("thread") or {}

    def thread_state(self, thread_id: str) -> ThreadState:
        """Return the live state for ``thread_id``, creating a placeholder if needed."""
        with self._lock:
            state = self._threads.get(thread_id)
            if state is None:
                state = ThreadState(thread_id=thread_id)
                self._threads[thread_id] = state
            return state

    # ------------------------------------------------------------------
    # turns
    # ------------------------------------------------------------------
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
        """Send one user turn and optionally wait for it to finish."""
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
        }
        if effort:
            params["effort"] = effort
        if model:
            params["model"] = model
        response = self._app.request("turn/start", params)
        turn = response.get("turn") or {}
        turn_id = turn.get("id")
        if not turn_id:
            raise SessionError(f"turn/start returned no turn id: {response}")
        state = self.thread_state(thread_id)
        with state.condition:
            state.turn_count += 1
            state.active_turn_id = turn_id
        self._log("info", "turn started", thread_id=thread_id, turn_id=turn_id)
        if not wait:
            return None
        return self.wait_for_turn(thread_id, turn_id, timeout=timeout)

    def wait_for_turn(
        self, thread_id: str, turn_id: str, *, timeout: float | None = None
    ) -> TurnOutcome:
        """Block until ``turn_id`` on ``thread_id`` reaches a terminal status."""
        state = self.thread_state(thread_id)
        deadline = None if timeout is None else time.monotonic() + timeout
        with state.condition:
            while turn_id not in state.completed_turn_ids:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise SessionError(
                        f"timed out waiting for turn {turn_id} on thread {thread_id}"
                    )
                state.condition.wait(timeout=remaining)
            status = state.last_turn_status or "completed"
            text = state.last_agent_text or ""
        return TurnOutcome(
            thread_id=thread_id,
            turn_id=turn_id,
            status=status,
            final_text=text,
            usage=state.usage,
        )

    def is_turn_active(self, thread_id: str) -> bool:
        """Return True when a turn is currently running on the thread."""
        state = self.thread_state(thread_id)
        with state.condition:
            return state.active_turn_id is not None

    def interrupt_turn(self, thread_id: str, turn_id: str | None = None) -> None:
        """Interrupt the active turn (used only by explicit operator cancel)."""
        target = turn_id
        if target is None:
            target = self.thread_state(thread_id).active_turn_id
        if not target:
            return
        try:
            self._app.request("turn/interrupt", {"threadId": thread_id, "turnId": target})
        except AppServerError as error:
            self._log("warning", "turn interrupt failed", thread_id=thread_id, error=str(error))

    # ------------------------------------------------------------------
    # notification handling
    # ------------------------------------------------------------------
    def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId")
        if method == "thread/tokenUsage/updated":
            usage = TokenUsage.from_notification(params.get("tokenUsage") or {})
            if thread_id:
                state = self.thread_state(thread_id)
                with state.condition:
                    state.usage = usage
                    state.condition.notify_all()
            return

        if method == "turn/completed":
            turn = params.get("turn") or {}
            turn_id = turn.get("id") or params.get("turnId")
            status = str(turn.get("status") or "completed")
            final_text = _extract_agent_text(turn)
            if thread_id:
                state = self.thread_state(thread_id)
                with state.condition:
                    if turn_id:
                        state.completed_turn_ids.append(turn_id)
                    state.active_turn_id = None
                    state.last_turn_status = status
                    if final_text:
                        state.last_agent_text = final_text
                    state.condition.notify_all()
            self._log(
                "info",
                "turn completed",
                thread_id=thread_id,
                turn_id=turn_id,
                status=status,
            )
            return

        if method == "item/completed":
            item = params.get("item") or {}
            text = _extract_item_text(item)
            if text and thread_id and str(item.get("type", "")).lower() in (
                "agentmessage",
                "agent_message",
            ):
                state = self.thread_state(thread_id)
                with state.condition:
                    state.last_agent_text = text
            return

        if method in ("thread/compacted", "thread/compact/completed"):
            if thread_id:
                state = self.thread_state(thread_id)
                with state.condition:
                    state.compaction_events += 1
            self._log("info", "context compacted", thread_id=thread_id, method=method)
            return

        if method in ("error", "thread/error"):
            message = str(params.get("message") or params.get("error") or params)
            if thread_id:
                state = self.thread_state(thread_id)
                with state.condition:
                    state.error_messages.append(message)
            self._log("error", "app-server error notification", thread_id=thread_id, message=message)
            return

    def _log(self, severity: str, message: str, **fields: Any) -> None:
        if self._logger is None:
            return
        getattr(self._logger, severity, self._logger.info)(message, extra={"relay_fields": fields})


def _extract_agent_text(payload: Any) -> str:
    """Return the last agent message text found anywhere in ``payload``."""
    texts: list[str] = []
    _collect_text(payload, texts)
    return texts[-1] if texts else ""


def _collect_text(node: Any, texts: list[str]) -> None:
    if isinstance(node, dict):
        item_type = str(node.get("type", "")).lower()
        if item_type in ("agentmessage", "agent_message", "text"):
            candidate = node.get("text")
            if isinstance(candidate, str) and candidate.strip():
                texts.append(candidate)
        for value in node.values():
            _collect_text(value, texts)
    elif isinstance(node, list):
        for value in node:
            _collect_text(value, texts)


def _extract_item_text(item: dict[str, Any]) -> str:
    texts: list[str] = []
    _collect_text(item, texts)
    return texts[-1] if texts else ""
