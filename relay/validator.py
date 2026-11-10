"""Deciding whether the development objective is actually finished.

The validator never trusts the agent's own claim of completion. It combines
three sources of evidence:

1. the machine ledger (``.relay/state.json``): every task must be ``done``;
2. the configured acceptance commands (build / tests) executed in the project;
3. the git state, so "done without any change" is detectable.

Anything that cannot be verified automatically is reported as pending human
acceptance instead of being counted as complete.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .checkpoint import GitSnapshot


@dataclass(frozen=True)
class AcceptanceResult:
    """Outcome of one acceptance command."""

    command: str
    returncode: int
    passed: bool
    duration_seconds: float
    output_tail: str


@dataclass
class ValidationReport:
    """Aggregated verdict for one validation attempt."""

    complete: bool
    pending_human: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    acceptance: list[AcceptanceResult] = field(default_factory=list)
    unfinished_tasks: list[str] = field(default_factory=list)
    blocked_tasks: list[str] = field(default_factory=list)
    checked_at: float = field(default_factory=time.time)

    def summary(self) -> str:
        """Return a one-line human summary."""
        if self.complete and self.pending_human:
            return "tasks complete, waiting for human acceptance"
        if self.complete:
            return "complete"
        if self.blocked_tasks:
            return f"blocked on {len(self.blocked_tasks)} task(s)"
        if self.unfinished_tasks:
            return f"{len(self.unfinished_tasks)} task(s) still open"
        return "incomplete"


class TaskValidator:
    """Runs acceptance commands and reconciles them with the task ledger."""

    def __init__(
        self,
        *,
        project_root: Path,
        logger: Any = None,
        command_timeout_seconds: float = 900.0,
    ) -> None:
        self.project_root = project_root
        self._logger = logger
        self.command_timeout = command_timeout_seconds

    def validate(
        self,
        state: dict[str, Any],
        *,
        git_snapshot: GitSnapshot | None = None,
        run_commands: bool = True,
    ) -> ValidationReport:
        """Validate the ledger plus acceptance commands."""
        report = ValidationReport(complete=False)
        tasks = state.get("tasks") or []
        for task in tasks:
            status = task.get("status")
            if status in ("pending", "in_progress"):
                report.unfinished_tasks.append(task["id"])
            elif status == "blocked":
                report.blocked_tasks.append(task["id"])
            elif status == "unverifiable":
                report.pending_human.append(
                    f"task {task['id']} ({task.get('title', '')}) marked unverifiable by the agent"
                )

        acceptance_commands: Sequence[str] = (
            (state.get("acceptance") or {}).get("commands") or []
        )
        if run_commands:
            for command in acceptance_commands:
                result = self._run_command(command)
                report.acceptance.append(result)
                if not result.passed:
                    report.failures.append(
                        f"acceptance command failed with exit code {result.returncode}: {command}"
                    )

        if report.unfinished_tasks:
            report.complete = False
            return report
        if report.blocked_tasks:
            report.complete = False
            return report
        if report.failures:
            report.complete = False
            return report

        if not acceptance_commands:
            report.pending_human.append(
                "no automated acceptance commands were configured for this chain"
            )
        if git_snapshot is not None and not git_snapshot.has_commits:
            report.pending_human.append(
                "the work branch has no commits, so code changes cannot be verified from git"
            )
        report.complete = True
        return report

    def _run_command(self, command: str) -> AcceptanceResult:
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=str(self.project_root),
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.command_timeout,
                check=False,
            )
            duration = time.monotonic() - started
            output = ((completed.stdout or "") + (completed.stderr or "")).strip()
            result = AcceptanceResult(
                command=command,
                returncode=completed.returncode,
                passed=completed.returncode == 0,
                duration_seconds=duration,
                output_tail=output[-4000:],
            )
        except subprocess.TimeoutExpired:
            duration = time.monotonic() - started
            result = AcceptanceResult(
                command=command,
                returncode=-1,
                passed=False,
                duration_seconds=duration,
                output_tail=f"timed out after {self.command_timeout:.0f}s",
            )
        if self._logger is not None:
            self._logger.info(
                "acceptance command finished",
                extra={
                    "relay_fields": {
                        "command": command,
                        "returncode": result.returncode,
                        "duration": round(result.duration_seconds, 3),
                    }
                },
            )
        return result
