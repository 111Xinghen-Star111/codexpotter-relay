"""MCP server that exposes the Relay inside Codex as a plugin.

Codex plugins talk to a Model Context Protocol server over stdio (newline
delimited JSON-RPC 2.0). This module implements exactly the methods Codex calls
— ``initialize``, ``ping``, ``tools/list``, ``tools/call`` — and maps them onto
the existing Relay library, so the plugin is a thin surface over the same engine
the ``relay`` CLI uses.

The controller itself must not live inside the plugin process: a plugin server is
started and stopped with a Codex session, while a work chain has to survive
session ends and Codex restarts. ``relay_start`` therefore spawns the controller
detached and returns immediately, which is also what the requirements demand
("控制器不能依赖旧 Agent 存活").
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import RelayConfig, load_config
from .db import Database
from .paths import database_path

PROTOCOL_VERSIONS = ("2025-03-26", "2025-06-18", "2025-11-25")
DEFAULT_PROTOCOL = "2025-11-25"
SERVER_NAME = "codexpotter-relay"


@dataclass(frozen=True)
class ToolResult:
    """Result of one tool call."""

    text: str
    is_error: bool = False

    def as_mcp(self) -> dict[str, Any]:
        """Render the MCP ``tools/call`` result payload."""
        payload: dict[str, Any] = {"content": [{"type": "text", "text": self.text}]}
        if self.is_error:
            payload["isError"] = True
        return payload


class RelayToolbox:
    """Implements the relay tools against the production database."""

    def __init__(
        self,
        *,
        config: RelayConfig | None = None,
        database: Database | None = None,
        config_path: Path | None = None,
        db_path: Path | None = None,
    ) -> None:
        self._config = config
        self._config_path = config_path
        self._db_path = db_path
        self._database = database

    # ------------------------------------------------------------------
    # lazily created dependencies
    # ------------------------------------------------------------------
    @property
    def config(self) -> RelayConfig:
        if self._config is None:
            self._config = load_config(self._config_path)
        return self._config

    @property
    def db(self) -> Database:
        if self._database is None:
            path = Path(self._db_path) if self._db_path else database_path("production")
            database = Database(path)
            database.migrate()
            self._database = database
        return self._database

    # ------------------------------------------------------------------
    # tools
    # ------------------------------------------------------------------
    def tools(self) -> list[dict[str, Any]]:
        """Return the MCP tool descriptors."""
        return [
            _tool(
                "relay_start",
                "Submit one development objective and start an unattended work chain. "
                "The chain keeps working across context-window handoffs without further prompts.",
                {
                    "goal": {"type": "string", "description": "The development objective."},
                    "project": {
                        "type": "string",
                        "description": "Absolute path of the project to work in.",
                    },
                    "tasks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional task list, each formatted 'T1:title'.",
                    },
                    "accept": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Acceptance commands run before the chain may report completion.",
                    },
                    "max_handoffs": {"type": "integer", "minimum": 0},
                    "max_model_requests": {"type": "integer", "minimum": 1},
                },
                required=["goal", "project"],
            ),
            _tool(
                "relay_status",
                "Show one chain: state, sessions, handoffs, checkpoints, tasks and budgets.",
                {"chain": {"type": "string", "description": "Chain id; omit for the newest chain."}},
            ),
            _tool("relay_chains", "List recent work chains with their state.", {}),
            _tool(
                "relay_report",
                "Write and return the development report (markdown path plus a short digest).",
                {"chain": {"type": "string"}},
                required=["chain"],
            ),
            _tool(
                "relay_add_requirement",
                "Record a new requirement for an existing chain (optionally adding tasks). "
                "It takes effect from the next turn or the next session after a handoff.",
                {
                    "chain": {"type": "string"},
                    "text": {"type": "string", "description": "The new requirement."},
                    "tasks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional extra tasks formatted 'T4:title'.",
                    },
                },
                required=["chain", "text"],
            ),
            _tool(
                "relay_checkpoint",
                "Write a checkpoint of the chain's ledger and git state right now.",
                {"chain": {"type": "string"}, "reason": {"type": "string"}},
                required=["chain"],
            ),
            _tool(
                "relay_pause",
                "Pause a chain: it stops issuing model requests at the next safe point.",
                {"chain": {"type": "string"}},
                required=["chain"],
            ),
            _tool(
                "relay_resume",
                "Resume a paused chain.",
                {"chain": {"type": "string"}},
                required=["chain"],
            ),
            _tool(
                "relay_stop",
                "Stop a chain permanently.",
                {"chain": {"type": "string"}},
                required=["chain"],
            ),
            _tool(
                "relay_recover",
                "Take over chains left running by a stopped controller, then continue them.",
                {"chain": {"type": "string"}},
            ),
            _tool("relay_doctor", "Check Codex, git and python prerequisites.", {}),
        ]

    def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Dispatch one tool call."""
        handlers: dict[str, Callable[[dict[str, Any]], ToolResult]] = {
            "relay_start": self._start,
            "relay_status": self._status,
            "relay_chains": self._chains,
            "relay_report": self._report,
            "relay_add_requirement": self._add_requirement,
            "relay_checkpoint": self._checkpoint,
            "relay_pause": lambda args: self._lifecycle("pause", args),
            "relay_resume": lambda args: self._lifecycle("resume", args),
            "relay_stop": lambda args: self._lifecycle("stop", args),
            "relay_recover": self._recover,
            "relay_doctor": self._doctor,
        }
        handler = handlers.get(name)
        if handler is None:
            return ToolResult(f"unknown relay tool: {name}", is_error=True)
        try:
            return handler(arguments or {})
        except Exception as error:  # noqa: BLE001 - never crash the MCP server
            return ToolResult(f"{type(error).__name__}: {error}", is_error=True)

    # ------------------------------------------------------------------
    # individual tools
    # ------------------------------------------------------------------
    def _start(self, arguments: dict[str, Any]) -> ToolResult:
        from .controller import RelayController
        from .ops import spawn_detached_controller

        goal = str(arguments.get("goal") or "").strip()
        project = str(arguments.get("project") or "").strip()
        if not goal or not project:
            return ToolResult("relay_start needs both `goal` and `project`.", is_error=True)
        project_path = Path(project).expanduser()
        if not project_path.is_absolute():
            return ToolResult("`project` must be an absolute path.", is_error=True)
        project_path.mkdir(parents=True, exist_ok=True)

        tasks = [_parse_task(spec) for spec in arguments.get("tasks") or []]
        accept = [str(item) for item in arguments.get("accept") or []]
        chain = RelayController.create_chain(
            database=self.db,
            project_root=project_path,
            goal=goal,
            tasks=tasks,
            acceptance_commands=accept,
            config=self.config,
            mode="production",
        )
        pid, log_path = spawn_detached_controller(
            chain.id,
            config_path=self._config_path,
            db_path=self.db.path,
        )
        return ToolResult(
            f"Work chain {chain.id} started.\n"
            f"project: {chain.project_root}\nbranch: {chain.work_branch}\n"
            f"controller pid: {pid}\nlog: {log_path}\n"
            "It runs unattended: check progress with relay_status, reports with relay_report."
        )

    def _status(self, arguments: dict[str, Any]) -> ToolResult:
        chain_id = arguments.get("chain") or self._newest_chain_id()
        if not chain_id:
            return ToolResult("no work chain exists yet; call relay_start first.")
        chain = self.db.get_chain(str(chain_id))
        sessions = self.db.main_sessions(chain.id)
        handoffs = self.db.list_handoffs(chain.id)
        checkpoints = self.db.list_checkpoints(chain.id, limit=5)
        tasks = self.db.list_tasks(chain.id)
        if not tasks:
            # Right after relay_start the controller has not synced the ledger
            # into the database yet, so read the tasks from the project ledger.
            tasks = _ledger_tasks(Path(chain.project_root))
        lines = [
            f"chain {chain.id} [{chain.state}]",
            f"  goal: {chain.goal.strip().splitlines()[0][:120]}",
            f"  project: {chain.project_root} ({chain.work_branch})",
            f"  active session: {chain.active_session_id or '-'}",
            f"  model requests: {self.db.count_model_requests(chain.id)}",
            f"  stop reason: {chain.stop_reason or '-'}",
            "  sessions:",
        ]
        for session in sessions:
            lines.append(
                f"    #{session.ordinal or '-'} {session.id[:16]} {session.state} "
                f"turns={session.request_count} tokens={session.token_total} "
                f"ctx={session.context_window or '-'}"
            )
        lines.append("  handoffs:")
        for handoff in handoffs:
            lines.append(f"    #{handoff.sequence} {handoff.phase} {handoff.state}")
        lines.append("  checkpoints:")
        for checkpoint in checkpoints:
            lines.append(f"    v{checkpoint.version} {checkpoint.reason}")
        lines.append("  tasks:")
        for task in tasks:
            lines.append(f"    {task['task_id']}: {task['status']} — {task['title']}")
        return ToolResult("\n".join(lines))

    def _chains(self, arguments: dict[str, Any]) -> ToolResult:
        chains = self.db.list_chains(limit=50)
        if not chains:
            return ToolResult("no work chain exists yet.")
        lines = ["id | state | mode | project"]
        for chain in chains:
            lines.append(f"{chain.id} | {chain.state} | {chain.mode} | {chain.project_root}")
        return ToolResult("\n".join(lines))

    def _report(self, arguments: dict[str, Any]) -> ToolResult:
        from .report import write_chain_report

        chain_id = str(arguments.get("chain") or self._newest_chain_id() or "")
        if not chain_id:
            return ToolResult("no chain given and none exists.", is_error=True)
        chain = self.db.get_chain(chain_id)
        path = write_chain_report(self.db, chain.id)
        tasks = self.db.list_tasks(chain.id)
        done = sum(1 for task in tasks if task["status"] == "done")
        return ToolResult(
            f"report: {path}\nchain {chain.id} [{chain.state}] "
            f"tasks {done}/{len(tasks)} done, "
            f"handoffs {len(self.db.list_handoffs(chain.id))}, "
            f"model requests {self.db.count_model_requests(chain.id)}"
        )

    def _add_requirement(self, arguments: dict[str, Any]) -> ToolResult:
        from .ops import append_requirement

        chain = self.db.get_chain(str(arguments.get("chain")))
        text = str(arguments.get("text") or "").strip()
        if not text:
            return ToolResult("`text` must not be empty.", is_error=True)
        tasks = [_parse_task(spec) for spec in arguments.get("tasks") or []]
        checkpoint = append_requirement(self.db, chain, text=text, tasks=tasks)
        return ToolResult(
            f"requirement recorded on {chain.id} (checkpoint v{checkpoint.version}). "
            "It applies from the next turn of the current session or the next session "
            "after a handoff; a turn already running cannot be interrupted to inject it."
        )

    def _checkpoint(self, arguments: dict[str, Any]) -> ToolResult:
        from .ops import capture_checkpoint

        chain = self.db.get_chain(str(arguments.get("chain")))
        checkpoint = capture_checkpoint(
            self.db, chain, reason=str(arguments.get("reason") or "manual")
        )
        return ToolResult(
            f"checkpoint v{checkpoint.version} written "
            f"(valid={checkpoint.is_intact()}, head={(checkpoint.git_head or '-')[:10]})"
        )

    def _lifecycle(self, action: str, arguments: dict[str, Any]) -> ToolResult:
        from .ops import pause_chain, resume_chain, stop_chain

        chain = self.db.get_chain(str(arguments.get("chain")))
        if action == "pause":
            chain = pause_chain(self.db, chain)
        elif action == "resume":
            chain = resume_chain(self.db, chain)
        else:
            chain = stop_chain(self.db, chain)
        return ToolResult(f"{chain.id}: {chain.state}")

    def _recover(self, arguments: dict[str, Any]) -> ToolResult:
        from .ops import spawn_detached_controller

        chain_id = arguments.get("chain")
        if chain_id:
            candidates = [self.db.get_chain(str(chain_id))]
        else:
            candidates = self.db.chains_needing_recovery()
        if not candidates:
            return ToolResult("nothing to recover: no chain is left running.")
        lines = []
        for chain in candidates:
            pid, log_path = spawn_detached_controller(
                chain.id, config_path=self._config_path, db_path=self.db.path
            )
            lines.append(f"resuming {chain.id} (was {chain.state}) pid={pid} log={log_path}")
        return ToolResult("\n".join(lines))

    def _doctor(self, arguments: dict[str, Any]) -> ToolResult:
        from .ops import doctor

        report = doctor(self.config)
        lines = [f"{key}: {value}" for key, value in report.items()]
        return ToolResult("\n".join(lines))

    # ------------------------------------------------------------------
    def _newest_chain_id(self) -> str | None:
        chains = self.db.list_chains(limit=1)
        return chains[0].id if chains else None


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    """Build one MCP tool descriptor."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return {
        "name": name,
        "description": description,
        "inputSchema": schema,
        "annotations": {
            "readOnlyHint": name
            in ("relay_status", "relay_chains", "relay_report", "relay_doctor"),
            "destructiveHint": name in ("relay_stop",),
            "openWorldHint": False,
        },
    }


def _parse_task(spec: str) -> dict[str, Any]:
    """Parse ``T1:title`` (or a bare title) into a ledger task."""
    text = str(spec).strip()
    if ":" in text:
        task_id, title = text.split(":", 1)
        return {"id": task_id.strip(), "title": title.strip(), "status": "pending"}
    return {"id": text, "title": text, "status": "pending"}


def _ledger_tasks(project_root: Path) -> list[dict[str, Any]]:
    """Read the task list from ``.relay/state.json``, tolerating a missing file."""
    from .checkpoint import CheckpointError, read_package

    try:
        package = read_package(project_root)
    except (CheckpointError, OSError):
        return []
    return [
        {
            "task_id": str(task.get("id")),
            "title": str(task.get("title", "")),
            "status": str(task.get("status", "pending")),
        }
        for task in package.state.get("tasks") or []
    ]


class McpServer:
    """Newline-delimited JSON-RPC server for the MCP ``tools`` surface."""

    def __init__(self, toolbox: RelayToolbox | None = None, *, version: str = "0.1.0") -> None:
        self.toolbox = toolbox or RelayToolbox()
        self.version = version

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """Return the JSON-RPC ``result`` for ``method`` or None when unknown."""
        if method == "initialize":
            requested = (params or {}).get("protocolVersion")
            return {
                "protocolVersion": requested if requested in PROTOCOL_VERSIONS else DEFAULT_PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": self.version},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.toolbox.tools()}
        if method == "tools/call":
            name = str((params or {}).get("name") or "")
            arguments = (params or {}).get("arguments") or {}
            if not isinstance(arguments, dict):
                return {"content": [{"type": "text", "text": "arguments must be an object"}], "isError": True}
            return self.toolbox.call(name, arguments).as_mcp()
        if method in ("resources/list",):
            return {"resources": []}
        if method in ("resources/templates/list",):
            return {"resourceTemplates": []}
        if method in ("prompts/list",):
            return {"prompts": []}
        return None

    def serve(self, stdin=None, stdout=None) -> int:
        """Run the stdio loop until EOF."""
        input_stream = stdin or sys.stdin
        output_stream = stdout or sys.stdout
        for line in input_stream:
            line = line.strip()
            if not line:
                continue
            message = _decode(line)
            if message is None:
                _write(output_stream, {"id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(
                message.get("method"), str
            ):
                _write(
                    output_stream,
                    {"id": None, "error": {"code": -32600, "message": "Invalid request"}},
                )
                continue
            if "id" not in message:
                continue  # notification: nothing to answer
            request_id = message.get("id")
            try:
                result = self.dispatch(str(message["method"]), message.get("params") or {})
            except Exception:  # noqa: BLE001 - protocol requires a response
                _write(output_stream, {"id": request_id, "error": {"code": -32603, "message": "Internal error"}})
                continue
            if result is None:
                _write(
                    output_stream,
                    {"id": request_id, "error": {"code": -32601, "message": "Method not found"}},
                )
            else:
                _write(output_stream, {"id": request_id, "result": result})
        return 0


def _decode(line: str) -> Any:
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _write(stream, payload: dict[str, Any]) -> None:
    stream.write(json.dumps({"jsonrpc": "2.0", **payload}, ensure_ascii=False) + "\n")
    stream.flush()


def main() -> int:
    """Entry point used by ``python -m relay.mcp`` and the plugin manifest."""
    return McpServer().serve()


if __name__ == "__main__":
    raise SystemExit(main())
