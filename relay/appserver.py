"""JSON-RPC client for the Codex ``app-server``.

The Relay drives Codex through the officially documented app server rather than
by scripting the interactive TUI. One :class:`AppServer` instance owns one
``codex app-server`` child process speaking newline-delimited JSON-RPC 2.0 over
stdio.

The client is deliberately small: it does the ``initialize`` handshake, exposes
``request``/``notify``, and dispatches three kinds of incoming traffic:

* responses to our requests (resolved by id);
* notifications such as ``thread/tokenUsage/updated`` (forwarded to listeners);
* server-initiated requests such as approval prompts and ``request_user_input``.

Server-initiated requests are answered by :class:`UnattendedPolicy`, which keeps
an unattended run from hanging while still honouring the sandbox: approvals are
declined by default, and interactive questions are answered with an empty set
so the agent continues on its own judgement.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

#: Notification handler: receives the raw ``params`` object of a notification.
NotificationHandler = Callable[[str, dict[str, Any]], None]


class AppServerError(RuntimeError):
    """Raised when the app server cannot be started, dies, or returns an error."""


class AppServerUnavailable(AppServerError):
    """Raised when the app server process exits before completing a request."""


@dataclass
class _PendingRequest:
    """A request waiting for its JSON-RPC response."""

    event: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


@dataclass(frozen=True)
class ServerRequestAnswer:
    """Decision the Relay returns to a server-initiated request."""

    result: dict[str, Any]
    reason: str


class UnattendedPolicy:
    """Answer server-initiated requests without a human at the keyboard.

    The Relay must never silently widen permissions: command and file-change
    approvals are declined, because the configured sandbox (``workspace-write``
    by default) is what allows ordinary work to proceed. Questions that an
    interactive client would show to the user are answered with no choices so
    the agent falls back to its own judgement, which is what the CodexPotter
    handoff prompt already instructs it to do.
    """

    def answer(self, method: str, params: dict[str, Any]) -> ServerRequestAnswer:
        """Return the response payload for one server-initiated request."""
        if method.endswith("requestApproval") or "Approval" in method:
            return ServerRequestAnswer({"decision": "decline"}, "approval declined (unattended)")
        if method.endswith("request_user_input") or method.endswith("requestUserInput"):
            return ServerRequestAnswer({"answers": {}}, "interactive question skipped")
        if method.endswith("elicitation/create") or "elicit" in method.lower():
            return ServerRequestAnswer({"action": "decline"}, "elicitation declined (unattended)")
        return ServerRequestAnswer({}, "unsupported server request ignored")


class AppServer:
    """Owns one ``codex app-server`` child process."""

    def __init__(
        self,
        executable: Path,
        *,
        config_overrides: dict[str, Any] | None = None,
        cwd: Path | None = None,
        client_name: str = "codexpotter-relay",
        client_version: str = "0.1.0",
        startup_timeout: float = 120.0,
        request_timeout: float = 300.0,
        policy: UnattendedPolicy | None = None,
        stderr_path: Path | None = None,
        logger: Any = None,
    ) -> None:
        self.executable = executable
        self.config_overrides = dict(config_overrides or {})
        self.cwd = cwd
        self.client_name = client_name
        self.client_version = client_version
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self.policy = policy or UnattendedPolicy()
        self.stderr_path = stderr_path
        self._logger = logger

        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._pending: dict[int, _PendingRequest] = {}
        self._pending_lock = threading.Lock()
        self._next_id = 1
        self._listeners: list[NotificationHandler] = []
        self._server_requests: list[dict[str, Any]] = []
        self._write_lock = threading.Lock()
        self._closed = threading.Event()
        self._stderr_tail: queue.Queue[str] = queue.Queue(maxsize=200)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Spawn the app server and complete the initialize handshake."""
        # Codex runs shell commands in child processes that inherit this
        # environment, so git must be resolvable before the server starts.
        try:
            from .checkpoint import ensure_git_on_path

            ensure_git_on_path()
        except Exception:  # noqa: BLE001 - a missing git is reported later, not here
            pass

        arguments = [str(self.executable), "app-server", "--listen", "stdio://"]
        for key, value in self.config_overrides.items():
            arguments.extend(["-c", f"{key}={_toml_literal(value)}"])

        stderr_target = subprocess.PIPE
        stderr_file_handle = None
        if self.stderr_path is not None:
            self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
            stderr_file_handle = open(self.stderr_path, "a", encoding="utf-8")
            stderr_target = stderr_file_handle

        self._process = subprocess.Popen(
            arguments,
            cwd=str(self.cwd) if self.cwd else None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_target,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._stderr_handle = stderr_file_handle

        self._reader = threading.Thread(
            target=self._read_stdout, name="relay-appserver-reader", daemon=True
        )
        self._reader.start()

        if stderr_target == subprocess.PIPE:
            threading.Thread(
                target=self._drain_stderr, name="relay-appserver-stderr", daemon=True
            ).start()

        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": self.client_name,
                    "title": "CodexPotter Relay",
                    "version": self.client_version,
                },
                "capabilities": {"experimentalApi": False},
            },
            timeout=self.startup_timeout,
        )
        self.notify("initialized", {})

    def stop(self, *, timeout: float = 10.0) -> None:
        """Terminate the child process and its reader threads."""
        if self._closed.is_set():
            return
        self._closed.set()
        process = self._process
        if process is None:
            return
        try:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.terminate()
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pass
        except OSError:
            pass
        if self._reader is not None:
            self._reader.join(timeout=timeout)
        handle = getattr(self, "_stderr_handle", None)
        if handle is not None:
            handle.close()
            self._stderr_handle = None
        self._fail_pending(AppServerUnavailable("app server stopped"))

    @property
    def alive(self) -> bool:
        """Return True while the child process is running."""
        return self._process is not None and self._process.poll() is None

    # ------------------------------------------------------------------
    # protocol
    # ------------------------------------------------------------------
    def add_notification_listener(self, handler: NotificationHandler) -> None:
        """Register a callback for every incoming notification."""
        self._listeners.append(handler)

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send a request and block until its response arrives."""
        if not self.alive:
            raise AppServerUnavailable(
                "app server is not running"
                + (f": {self.last_stderr()}" if self.last_stderr() else "")
            )
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            pending = _PendingRequest()
            self._pending[request_id] = pending

        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        if not pending.event.wait(timeout if timeout is not None else self.request_timeout):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise AppServerError(f"timeout waiting for {method} response")
        if pending.error is not None:
            raise AppServerError(f"{method} failed: {pending.error}")
        return pending.result or {}

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a notification (no response expected)."""
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def last_stderr(self) -> str:
        """Return buffered stderr lines for diagnostics."""
        lines = list(self._stderr_tail.queue)
        return "\n".join(lines[-20:])

    def server_requests(self) -> list[dict[str, Any]]:
        """Return every server-initiated request received so far."""
        return list(self._server_requests)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _write(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise AppServerUnavailable("app server stdin is unavailable")
        data = json.dumps(payload, ensure_ascii=False)
        with self._write_lock:
            try:
                process.stdin.write(data + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                raise AppServerUnavailable(f"app server stdin closed: {error}") from error

    def _read_stdout(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._log("warning", "app-server emitted non-JSON output", raw=line[:400])
                    continue
                self._dispatch(message)
        finally:
            self._fail_pending(
                AppServerUnavailable(
                    "app server exited" + (f": {self.last_stderr()}" if self.last_stderr() else "")
                )
            )

    def _dispatch(self, message: dict[str, Any]) -> None:
        if "method" in message and "id" in message:
            self._handle_server_request(message)
            return
        if "method" in message:
            self._handle_notification(message)
            return
        if "id" in message:
            self._handle_response(message)
            return
        self._log("warning", "unrecognised app-server message", message=message)

    def _handle_response(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        with self._pending_lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            return
        pending.result = message.get("result") or {}
        pending.error = message.get("error")
        pending.event.set()

    def _handle_notification(self, message: dict[str, Any]) -> None:
        method = str(message.get("method", ""))
        params = message.get("params") or {}
        for listener in list(self._listeners):
            try:
                listener(method, params)
            except Exception:  # noqa: BLE001 - a bad listener must not kill the reader
                self._log("error", "notification listener failed", method=method)

    def _handle_server_request(self, message: dict[str, Any]) -> None:
        method = str(message.get("method", ""))
        params = message.get("params") or {}
        record = {"method": method, "params": params, "received_at": time.time()}
        self._server_requests.append(record)
        answer = self.policy.answer(method, params)
        record["answer"] = answer.result
        record["reason"] = answer.reason
        self._log("info", "answered server request", method=method, reason=answer.reason)
        try:
            self._write(
                {"jsonrpc": "2.0", "id": message.get("id"), "result": answer.result}
            )
        except AppServerUnavailable:
            pass

    def _fail_pending(self, error: AppServerError) -> None:
        with self._pending_lock:
            pending_items = list(self._pending.values())
            self._pending.clear()
        for pending in pending_items:
            pending.error = {"message": str(error)}
            pending.event.set()

    def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            line = line.rstrip()
            if not line:
                continue
            try:
                self._stderr_tail.put_nowait(line)
            except queue.Full:
                try:
                    self._stderr_tail.get_nowait()
                    self._stderr_tail.put_nowait(line)
                except queue.Empty:
                    pass

    def _log(self, severity: str, message: str, **fields: Any) -> None:
        if self._logger is None:
            return
        getattr(self._logger, severity, self._logger.info)(message, extra={"relay_fields": fields})


def _toml_literal(value: Any) -> str:
    """Render a Python value as a TOML literal for a ``-c key=value`` override."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_literal(item) for item in value) + "]"
    if isinstance(value, dict):
        pairs = ", ".join(
            f"{_toml_key(key)} = {_toml_literal(item)}" for key, item in value.items()
        )
        return "{" + pairs + "}"
    raise TypeError(f"cannot express {type(value).__name__} as a config override")


def _toml_key(key: str) -> str:
    """Render a TOML key, quoting it only when necessary."""
    if key and all(character.isalnum() or character in "-_" for character in key):
        return key
    return _toml_literal(key)
