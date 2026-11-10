"""Hook-based state collection for CodexPotter Relay.

Two channels exist, and the Relay uses both:

1. **Hook collector** — ``relay hook --event <name>`` is a small, fast command
   that reads a JSON payload from stdin and stores it in the ``events`` table.
   It always exits 0 so a hook can never break a Codex turn.
2. **App-server notifications** — ``hook/started``, ``hook/completed``,
   ``turn/completed``, ``item/completed``, ``thread/tokenUsage/updated`` and
   ``thread/compacted`` are subscribed to directly. This channel is the primary
   source of truth because it is part of the documented protocol.

Whether a registered hook actually fires is verified rather than assumed:
:meth:`HookInstaller.installed_events` reports what was written, and
:func:`hooks_observed` reports what Codex actually executed. The test report
states which channel produced the evidence.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .db import Database
from .paths import codex_home

#: Hook events requested by the project requirements, mapped to the Codex event
#: names that the app-server protocol defines.
REQUIRED_EVENTS = (
    "sessionStart",
    "userPromptSubmit",
    "postToolUse",
    "postModelRequest",
    "preCompact",
    "subagentStop",
    "stop",
    "sessionEnd",
)

#: Additional events the Relay subscribes to when available.
OPTIONAL_EVENTS = (
    "preToolUse",
    "preModelRequest",
    "permissionRequest",
    "postCompact",
    "subagentStart",
    "interrupt",
)

ALL_EVENTS = REQUIRED_EVENTS + OPTIONAL_EVENTS

MANAGED_BLOCK_START = "# >>> CodexPotter Relay hooks (managed block) >>>"
MANAGED_BLOCK_END = "# <<< CodexPotter Relay hooks (managed block) <<<"


@dataclass(frozen=True)
class HookRecord:
    """One hook execution recorded for a chain."""

    event: str
    chain_id: str | None
    session_id: str | None
    payload: dict[str, Any]


def collect_hook_event(
    database: Database,
    *,
    event: str,
    payload: dict[str, Any],
    chain_id: str | None = None,
    session_id: str | None = None,
) -> bool:
    """Store one hook payload; returns True when it was a new event."""
    chain_id = chain_id or payload.get("chainId") or payload.get("chain_id")
    session_id = session_id or payload.get("threadId") or payload.get("sessionId")
    event_key = payload.get("eventKey") or payload.get("event_key")
    if not event_key:
        event_key = (
            f"hook:{event}:{chain_id or '-'}:{session_id or '-'}:"
            f"{payload.get('turnId') or payload.get('turn_id') or ''}:"
            f"{payload.get('timestamp') or payload.get('occurredAt') or ''}"
        )
    return database.append_event(
        event_key=str(event_key),
        kind=f"hook:{event}",
        source="hook",
        payload=payload,
        chain_id=chain_id,
        session_id=session_id,
    )


def run_hook_command(
    *,
    event: str,
    database: Database,
    stdin_text: str = "",
) -> int:
    """Entry point used by the generated hook command; always returns 0."""
    try:
        payload: dict[str, Any] = {}
        if stdin_text.strip():
            parsed = json.loads(stdin_text)
            if isinstance(parsed, dict):
                payload = parsed
            else:
                payload = {"value": parsed}
        payload.setdefault("event", event)
        payload.setdefault("cwd", os.getcwd())
        payload.setdefault("timestamp", payload.get("timestamp") or "")
        collect_hook_event(database, event=event, payload=payload)
    except Exception:  # noqa: BLE001 - hooks must never break a turn
        return 0
    return 0


def hooks_observed(database: Database, *, chain_id: str | None = None) -> list[str]:
    """Return hook events Codex actually executed for a chain."""
    events = database.list_events(chain_id=chain_id, limit=500)
    observed = {
        event["kind"].split(":", 1)[1]
        for event in events
        if event["kind"].startswith("hook:") and event["source"] == "hook"
    }
    return sorted(observed)


def app_server_hook_events(database: Database, *, chain_id: str | None = None) -> list[str]:
    """Return hook events reported through app-server notifications."""
    events = database.list_events(chain_id=chain_id, limit=500)
    observed: set[str] = set()
    for event in events:
        if event["kind"] not in ("hook_started", "hook_completed"):
            continue
        name = (event["payload"] or {}).get("eventName")
        if name:
            observed.add(str(name))
    return sorted(observed)


class HookInstaller:
    """Writes a removable hooks block into the Codex configuration."""

    def __init__(
        self,
        *,
        python_executable: str | None = None,
        database_path: Path | None = None,
        config_file: Path | None = None,
        validate: bool = True,
    ) -> None:
        self.python_executable = python_executable or sys.executable
        self.database_path = database_path
        self.config_file = config_file or (codex_home() / "config.toml")
        self.backup_file = self.config_file.with_suffix(".toml.relay-backup")
        self.validate = validate

    def render_block(self) -> str:
        """Render the managed TOML block for every requested event."""
        lines = [MANAGED_BLOCK_START]
        lines.append(
            "# Written by CodexPotter Relay. Remove with `relay hooks uninstall`\n"
            "# (or scripts\\uninstall.ps1). Codex 0.167 does not execute [hooks]\n"
            "# entries from config.toml, so state capture also runs through\n"
            "# app-server notifications."
        )
        for event in ALL_EVENTS:
            command = self._command_for(event)
            lines.append(f"[hooks.{event}]")
            lines.append(f'command = {json.dumps(command)}')
            lines.append("async = true")
            lines.append("")
        lines.append(MANAGED_BLOCK_END)
        return "\n".join(lines) + "\n"

    def _command_for(self, event: str) -> str:
        """Build the hook command line, pointing at the installed relay module."""
        parts = [
            _quote(self.python_executable),
            "-m",
            "relay",
            "hook",
            "--event",
            event,
        ]
        if self.database_path is not None:
            parts.extend(["--db", _quote(str(self.database_path))])
        return " ".join(parts)

    def install(self) -> dict[str, Any]:
        """Write the managed block, backing up the previous config once.

        After writing, the Codex configuration is re-parsed. If Codex rejects
        the file, the original content is restored so a failed install can never
        leave a broken configuration behind.
        """
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        existing = self.config_file.read_text(encoding="utf-8") if self.config_file.exists() else ""
        if MANAGED_BLOCK_START in existing:
            previous = existing
            updated = self._replace_block(existing, self.render_block())
        else:
            previous = existing
            if self.config_file.exists() and not self.backup_file.exists():
                shutil.copy2(self.config_file, self.backup_file)
            prefix = existing if existing.endswith("\n") or not existing else existing + "\n"
            separator = "\n" if prefix.strip() else ""
            updated = prefix + separator + self.render_block()
        self.config_file.write_text(updated, encoding="utf-8")

        if self.validate:
            valid, message = self._config_parses()
            if not valid:
                self.config_file.write_text(previous, encoding="utf-8")
                return {
                    "installed": False,
                    "config_file": str(self.config_file),
                    "error": message,
                    "restored": True,
                }
        return {
            "installed": True,
            "config_file": str(self.config_file),
            "backup_file": str(self.backup_file) if self.backup_file.exists() else None,
            "events": list(ALL_EVENTS),
        }

    def _config_parses(self) -> tuple[bool, str]:
        """Return whether Codex can still parse the configuration file."""
        from .codex_env import find_codex_executable

        try:
            executable = find_codex_executable()
        except Exception as error:  # noqa: BLE001 - reported, not raised
            return False, f"codex executable not found: {error}"
        completed = subprocess.run(
            [str(executable), "app-server", "--print-startup-config"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
        if completed.returncode == 0:
            return True, "ok"
        return False, ((completed.stdout or "") + (completed.stderr or "")).strip()[:1500]

    def uninstall(self) -> dict[str, Any]:
        """Remove the managed block, restoring the backup when nothing else remains."""
        if not self.config_file.exists():
            return {"removed": False, "reason": "config file does not exist"}
        existing = self.config_file.read_text(encoding="utf-8")
        if MANAGED_BLOCK_START not in existing:
            return {"removed": False, "reason": "no managed block present"}
        cleaned = self._strip_block(existing)
        if not cleaned.strip() and self.backup_file.exists():
            shutil.copy2(self.backup_file, self.config_file)
        else:
            self.config_file.write_text(cleaned, encoding="utf-8")
        return {
            "removed": True,
            "config_file": str(self.config_file),
            "restored_backup": not cleaned.strip() and self.backup_file.exists(),
        }

    def verify_registration(self) -> dict[str, Any]:
        """Ask the app server which hooks it actually discovered.

        Returns ``{"registered": [...], "raw": ...}``. An empty list means Codex
        did not pick the block up, which the reports must state plainly.
        """
        from .appserver import AppServer
        from .codex_env import find_codex_executable

        server = AppServer(
            find_codex_executable(),
            request_timeout=60,
            startup_timeout=120,
            cwd=self.config_file.parent,
        )
        try:
            server.start()
            response = server.request("hooks/list", {"cwds": [str(self.config_file.parent)]})
            data = response.get("data") or []
            registered: list[str] = []
            errors: list[str] = []
            for entry in data:
                for hook in entry.get("hooks") or []:
                    name = hook.get("eventName") or hook.get("event")
                    if name:
                        registered.append(str(name))
                errors.extend(entry.get("errors") or [])
            return {"registered": sorted(set(registered)), "errors": errors, "raw": response}
        finally:
            server.stop()

    @staticmethod
    def _replace_block(existing: str, block: str) -> str:
        start = existing.index(MANAGED_BLOCK_START)
        end = existing.index(MANAGED_BLOCK_END) + len(MANAGED_BLOCK_END)
        return existing[:start] + block.rstrip("\n") + existing[end:]

    @staticmethod
    def _strip_block(existing: str) -> str:
        start = existing.index(MANAGED_BLOCK_START)
        end = existing.index(MANAGED_BLOCK_END) + len(MANAGED_BLOCK_END)
        remainder = (existing[:start].rstrip("\n") + "\n" + existing[end:].lstrip("\n")).strip()
        return remainder + "\n" if remainder else ""


def _quote(value: str) -> str:
    """Quote a path for a Windows command line."""
    if " " in value or "\t" in value:
        return f'"{value}"'
    return value


def summarize_event_sources(events: Iterable[str]) -> str:
    """Return a short label for a set of observed hook event names."""
    names = sorted(set(events))
    if not names:
        return "none"
    return f"{len(names)} event(s): {', '.join(names)}"
