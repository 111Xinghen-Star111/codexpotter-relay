"""Checkpoints: git snapshots plus the resumable handoff package.

A checkpoint is not just a database row. It is the pair of files a fresh Codex
session must read to continue working, together with the git state they belong
to:

* ``<project>/.relay/HANDOFF.md`` — narrative handoff, reusing the section
  structure of the upstream CodexPotter handoff file so ``$loop`` keeps working
  inside a session;
* ``<project>/.relay/state.json`` — machine-readable ledger: tasks, decisions,
  acceptance commands and the next task id.

Both files are git-ignored inside managed projects, so the agent's commits stay
about the user's code.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .paths import PROJECT_STATE_DIR, project_state_dir

STATE_SCHEMA = 1

TASK_STATUSES = ("pending", "in_progress", "done", "blocked", "unverifiable")

HANDOFF_FILE_NAME = "HANDOFF.md"
STATE_FILE_NAME = "state.json"

GITIGNORE_ENTRIES = (
    f"{PROJECT_STATE_DIR}/",
)

HANDOFF_RULES = """\
## Relay Rules For The Next Session

You are continuing an existing work chain. Nothing from previous conversations
is in your context, so treat the files below as the source of truth.

1. Read `.relay/state.json` first. It lists every task with its status.
2. Read `.relay/HANDOFF.md` completely before touching code.
3. Verify the working tree with `git status` and `git log --oneline -10` before
   trusting any "done" record.
4. Continue with the single task marked `in_progress`, otherwise with the first
   `pending` task. Do not redo tasks marked `done` unless the current code
   contradicts them.
5. After finishing a task, update `.relay/state.json`: set its `status`, write
   concrete `evidence`, and move `next_task_id` forward.
6. Commit your work on the current branch. Never commit `.relay/**`.
7. Finish your final message with exactly one marker on its own line:
   `::relay(progress)` when work remains, `::relay(done)` when every task is
   done and verified, or `::relay(blocked)` when you cannot proceed.
8. Do not ask the operator questions. Make the best decision, record it under
   `decisions` in `.relay/state.json`, and continue.
"""


class CheckpointError(RuntimeError):
    """Raised when a handoff package is missing, corrupt or inconsistent."""


class GitError(RuntimeError):
    """Raised when a git command fails."""


def find_git_executable(override: str | None = None) -> str:
    """Return a usable git executable path."""
    if override:
        return override
    candidate = shutil.which("git")
    if candidate:
        return candidate
    for path in (
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files (x86)\Git\cmd\git.exe",
    ):
        if Path(path).is_file():
            return path
    raise GitError(
        "git executable not found; install Git for Windows or set controller.git_executable"
    )


def ensure_git_on_path(override: str | None = None) -> str | None:
    """Make ``git`` resolvable for child processes (and therefore for the agent).

    Codex sessions run shell commands in a child process that inherits this
    process's environment. A freshly installed Git for Windows is often absent
    from the current ``PATH``, so the git directory is prepended once here.
    """
    executable = find_git_executable(override)
    directory = str(Path(executable).parent)
    current = os.environ.get("PATH", "")
    parts = [part for part in current.split(os.pathsep) if part]
    if directory not in parts:
        os.environ["PATH"] = os.pathsep.join([directory, *parts])
    return executable


@dataclass(frozen=True)
class GitSnapshot:
    """Git state of a managed project at one moment."""

    branch: str
    head: str | None
    dirty: bool
    status_text: str
    has_commits: bool

    def as_json(self) -> dict[str, Any]:
        """Return a JSON-friendly view for the database and reports."""
        return {
            "branch": self.branch,
            "head": self.head,
            "dirty": self.dirty,
            "has_commits": self.has_commits,
        }


class GitRepository:
    """Minimal, explicit git operations used by the Relay."""

    def __init__(self, path: Path, *, git_executable: str | None = None) -> None:
        self.path = path
        self._git = find_git_executable(git_executable)

    def _command(self, arguments: Sequence[str]) -> list[str]:
        """Return the git command line for ``arguments``.

        ``safe.directory`` is passed explicitly for the managed project: on
        Windows a project can be owned by another account (for example one
        created by a sandboxed tool), and git then refuses every command with
        "dubious ownership". Scoping the exception to this one path keeps the
        protection for every other repository.
        """
        return [self._git, "-c", f"safe.directory={self.path}", *arguments]

    def _run(self, *arguments: str, timeout: float = 120.0) -> str:
        completed = subprocess.run(
            self._command(arguments),
            cwd=str(self.path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            raise GitError(
                f"git {' '.join(arguments)} failed in {self.path}: "
                f"{(completed.stderr or completed.stdout).strip()}"
            )
        return completed.stdout

    def is_repository(self) -> bool:
        """Return True when ``path`` is inside a git working tree."""
        if not self.path.is_dir():
            return False
        completed = subprocess.run(
            self._command(["rev-parse", "--is-inside-work-tree"]),
            cwd=str(self.path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return completed.returncode == 0 and completed.stdout.strip() == "true"

    def init(self) -> None:
        """Initialise a repository with an identity that works unattended."""
        self.path.mkdir(parents=True, exist_ok=True)
        self._run("init", "--initial-branch=main")
        self.configure_identity()

    def configure_identity(self, name: str = "CodexPotter Relay", email: str = "relay@localhost") -> None:
        """Set a local commit identity if the repository has none."""
        self._run("config", "user.name", name)
        self._run("config", "user.email", email)

    def ensure_identity(self) -> bool:
        """Guarantee a commit identity exists, using a repository-local default.

        A machine without ``user.name``/``user.email`` makes every commit fail
        with "Author identity unknown". Only the local config is touched, so the
        user's global git settings are never modified.
        """
        name = self._config_value("user.name")
        email = self._config_value("user.email")
        if name and email:
            return False
        self.configure_identity(name or "CodexPotter Relay", email or "relay@localhost")
        return True

    def _config_value(self, key: str) -> str:
        """Return a git config value, or an empty string when unset."""
        completed = subprocess.run(
            self._command(["config", "--get", key]),
            cwd=str(self.path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return completed.stdout.strip() if completed.returncode == 0 else ""

    def snapshot(self) -> GitSnapshot:
        """Return the current branch, head commit and dirty flag.

        Works on an unborn branch (a repository with no commits yet), which is
        what ``relay start`` sees in a brand new project directory.
        """
        branch = self._current_branch()
        status_text = self._run("status", "--porcelain").strip()
        head = self._rev_parse("HEAD")
        return GitSnapshot(
            branch=branch,
            head=head,
            dirty=bool(status_text),
            status_text=status_text,
            has_commits=head is not None,
        )

    def _current_branch(self) -> str:
        """Return the checked-out branch name, even before the first commit."""
        for arguments in (("symbolic-ref", "--short", "HEAD"), ("rev-parse", "--abbrev-ref", "HEAD")):
            completed = subprocess.run(
                self._command(list(arguments)),
                cwd=str(self.path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if completed.returncode == 0 and completed.stdout.strip():
                return completed.stdout.strip()
        return "HEAD"

    def _rev_parse(self, revision: str) -> str | None:
        """Return the commit hash for ``revision``, or None when it is unborn."""
        completed = subprocess.run(
            self._command(["rev-parse", "--verify", "--quiet", revision]),
            cwd=str(self.path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            return None
        value = completed.stdout.strip()
        return value or None

    def ensure_branch(self, branch: str) -> None:
        """Create ``branch`` if needed and check it out."""
        if not self.is_repository():
            self.init()
        existing = subprocess.run(
            self._command(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"]),
            cwd=str(self.path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if existing.returncode == 0:
            self._run("checkout", branch)
            return
        current = self.snapshot()
        if current.has_commits:
            self._run("checkout", "-b", branch)
        else:
            self._run("checkout", "-B", branch)

    def commit_all(self, message: str) -> str | None:
        """Stage everything and commit; return the new head or None if clean."""
        self._run("add", "-A")
        status = self._run("status", "--porcelain").strip()
        if not status:
            return None
        self._run("commit", "-m", message)
        return self.snapshot().head

    def commit_paths(self, message: str, paths: list[str]) -> str | None:
        """Commit only ``paths`` so unrelated user changes stay uncommitted."""
        existing = [path for path in paths if (self.path / path).exists()]
        if not existing:
            return None
        self._run("add", "--", *existing)
        staged = self._run("diff", "--cached", "--name-only").strip()
        if not staged:
            return None
        self._run("commit", "-m", message, "--", *existing)
        return self.snapshot().head

    def head(self) -> str | None:
        """Return the current commit hash, or None for an unborn branch."""
        return self.snapshot().head


@dataclass(frozen=True)
class HandoffPackage:
    """The two files a continuing session must read."""

    handoff_md: str
    state: dict[str, Any]

    def state_json(self) -> str:
        """Return the ledger serialised for storage."""
        return json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=True)


def handoff_paths(project_root: Path) -> tuple[Path, Path]:
    """Return ``(handoff_md_path, state_json_path)`` for a project."""
    directory = project_state_dir(project_root)
    return directory / HANDOFF_FILE_NAME, directory / STATE_FILE_NAME


def ensure_scaffold(project_root: Path) -> None:
    """Create the ``.relay`` directory and keep it out of git."""
    directory = project_state_dir(project_root)
    directory.mkdir(parents=True, exist_ok=True)
    ignore_file = project_root / ".gitignore"
    existing = ignore_file.read_text(encoding="utf-8") if ignore_file.exists() else ""
    lines = {line.strip() for line in existing.splitlines()}
    missing = [entry for entry in GITIGNORE_ENTRIES if entry not in lines]
    if missing:
        prefix = "" if not existing else ("" if existing.endswith("\n") else "\n")
        ignore_file.write_text(
            existing + prefix + "\n".join(missing) + "\n", encoding="utf-8"
        )


def write_package(project_root: Path, package: HandoffPackage) -> tuple[Path, Path]:
    """Write the handoff package atomically and return the two paths."""
    ensure_scaffold(project_root)
    handoff_path, state_path = handoff_paths(project_root)
    _atomic_write(handoff_path, package.handoff_md)
    _atomic_write(state_path, package.state_json() + "\n")
    return handoff_path, state_path


def read_package(project_root: Path) -> HandoffPackage:
    """Read and validate the handoff package, raising ``CheckpointError``."""
    handoff_path, state_path = handoff_paths(project_root)
    if not handoff_path.is_file():
        raise CheckpointError(f"missing handoff file: {handoff_path}")
    if not state_path.is_file():
        raise CheckpointError(f"missing state file: {state_path}")
    try:
        handoff_md = handoff_path.read_text(encoding="utf-8")
    except OSError as error:
        raise CheckpointError(f"cannot read {handoff_path}: {error}") from error
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CheckpointError(f"cannot parse {state_path}: {error}") from error
    validate_state(state, source=str(state_path))
    return HandoffPackage(handoff_md=handoff_md, state=state)


def validate_state(state: Any, *, source: str = "state") -> None:
    """Validate the machine ledger structure."""
    if not isinstance(state, dict):
        raise CheckpointError(f"{source}: state must be a JSON object")
    for key in ("schema", "chain_id", "goal", "tasks"):
        if key not in state:
            raise CheckpointError(f"{source}: missing key {key!r}")
    if state["schema"] != STATE_SCHEMA:
        raise CheckpointError(
            f"{source}: unsupported schema {state['schema']!r}, expected {STATE_SCHEMA}"
        )
    if not isinstance(state["goal"], str) or not state["goal"].strip():
        raise CheckpointError(f"{source}: goal must be a non-empty string")
    tasks = state["tasks"]
    if not isinstance(tasks, list) or not tasks:
        raise CheckpointError(f"{source}: tasks must be a non-empty list")
    seen: set[str] = set()
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise CheckpointError(f"{source}: task #{index} must be an object")
        for key in ("id", "title", "status"):
            if key not in task:
                raise CheckpointError(f"{source}: task #{index} is missing {key!r}")
        if task["id"] in seen:
            raise CheckpointError(f"{source}: duplicate task id {task['id']!r}")
        seen.add(task["id"])
        if task["status"] not in TASK_STATUSES:
            raise CheckpointError(
                f"{source}: task {task['id']!r} has invalid status {task['status']!r}"
            )
    next_task = state.get("next_task_id")
    if next_task is not None and next_task not in seen:
        raise CheckpointError(
            f"{source}: next_task_id {next_task!r} is not one of the declared tasks"
        )
    decisions = state.get("decisions", [])
    if not isinstance(decisions, list):
        raise CheckpointError(f"{source}: decisions must be a list")
    for index, decision in enumerate(decisions):
        if not isinstance(decision, (str, dict)):
            raise CheckpointError(
                f"{source}: decision #{index} must be a string or an object"
            )


def default_state(
    *,
    chain_id: str,
    goal: str,
    tasks: Iterable[dict[str, Any]],
    acceptance_commands: Iterable[str] = (),
) -> dict[str, Any]:
    """Build an initial ledger for a new chain."""
    task_list = [dict(task) for task in tasks]
    for task in task_list:
        task.setdefault("status", "pending")
        task.setdefault("evidence", None)
    first_pending = next(
        (task["id"] for task in task_list if task["status"] == "pending"), None
    )
    return {
        "schema": STATE_SCHEMA,
        "chain_id": chain_id,
        "goal": goal,
        "tasks": task_list,
        "decisions": [],
        "acceptance": {"commands": list(acceptance_commands), "notes": ""},
        "next_task_id": first_pending,
        "handoff_count": 0,
        "last_session": None,
    }


def evidence_to_text(value: Any) -> str | None:
    """Normalise a ledger ``evidence`` value into text for storage and display.

    Agents sometimes record structured evidence (objects or lists). The database
    column is text, so structured values are serialised instead of being
    silently dropped or crashing the sync.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def next_pending_task(state: dict[str, Any]) -> dict[str, Any] | None:
    """Return the task a continuing session should work on next."""
    tasks = state.get("tasks") or []
    for task in tasks:
        if task.get("status") == "in_progress":
            return task
    for task in tasks:
        if task.get("status") == "pending":
            return task
    return None


def all_tasks_done(state: dict[str, Any]) -> bool:
    """Return True when no task is pending, in progress or blocked."""
    tasks = state.get("tasks") or []
    return bool(tasks) and all(task.get("status") in ("done", "unverifiable") for task in tasks)


def build_handoff_markdown(
    *,
    chain_id: str,
    goal: str,
    original_request: str,
    context_notes: str,
    state: dict[str, Any],
    git_snapshot: GitSnapshot,
    handoff_sequence: int,
) -> str:
    """Render the narrative handoff file for the next session."""
    relative = f"{PROJECT_STATE_DIR}/"
    lines: list[str] = [
        "# Objective",
        "",
        "## Original User Request",
        "",
        original_request.strip(),
        "",
        "## Important Context, Constraints, and User Preferences",
        "",
        context_notes.strip() or "(none recorded)",
        "",
        "## Critical Data, Examples, and References",
        "",
        f"- Relay chain id: `{chain_id}`",
        f"- Handoff sequence: {handoff_sequence} (this file starts session "
        f"#{handoff_sequence + 1} of the chain)",
        f"- Goal: {goal.strip()}",
        f"- Work branch: `{git_snapshot.branch}` at `{git_snapshot.head or '<no commit yet>'}`",
        "- Task ledger: `.relay/state.json` (authoritative for task status)",
        f"- Files starting with `{relative}` are relay bookkeeping and must never be "
        "committed.",
        "",
        "## Task Ledger Snapshot",
        "",
        "| Task | Status | Evidence |",
        "| --- | --- | --- |",
    ]
    for task in state.get("tasks") or []:
        evidence = (evidence_to_text(task.get("evidence")) or "").replace("|", "\\|")
        if len(evidence) > 160:
            evidence = evidence[:157] + "..."
        lines.append(f"| {task['id']}: {task['title']} | {task['status']} | {evidence} |")
    lines.extend(
        [
            "",
            f"Next task: `{state.get('next_task_id') or 'none'}`",
            "",
            "## Decisions Recorded By Previous Sessions",
            "",
        ]
    )
    decisions = state.get("decisions") or []
    if decisions:
        for decision in decisions[-10:]:
            summary = decision.get("summary") if isinstance(decision, dict) else str(decision)
            if summary:
                lines.append(f"- {summary}")
    else:
        lines.append("- (none recorded)")
    lines.extend(["", "## Acceptance", ""])
    acceptance = (state.get("acceptance") or {}).get("commands") or []
    if acceptance:
        lines.append("Run these commands before claiming the objective is complete:")
        lines.append("")
        for command in acceptance:
            lines.append(f"```text\n{command}\n```")
    else:
        lines.append("No automated acceptance commands were configured.")
    lines.extend(["", HANDOFF_RULES, "", "# Done", ""])
    return "\n".join(lines)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
