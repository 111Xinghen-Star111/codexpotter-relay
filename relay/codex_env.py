"""Locating the installed Codex CLI.

Codex ships inside the desktop application on Windows and is frequently absent
from ``PATH``, so the Relay searches a documented order of locations before
giving up:

1. an explicit ``--codex`` / configuration override;
2. the ``CODEXPOTTER_CODEX`` environment variable;
3. ``codex`` on ``PATH``;
4. the Codex desktop application install directory
   (``%LOCALAPPDATA%\\OpenAI\\Codex\\bin\\*\\codex.exe``), newest first.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ENV_CODEX_PATH = "CODEXPOTTER_CODEX"


class CodexNotFoundError(RuntimeError):
    """Raised when no usable Codex CLI executable can be located."""


def _version_key(path: Path) -> tuple:
    """Sort key preferring a fully built install over a partially written one."""
    try:
        return (path.stat().st_size, str(path))
    except OSError:
        return (0, str(path))


def find_codex_executable(override: str | None = None) -> Path:
    """Return the path to the Codex CLI executable."""
    candidates: list[Path] = []

    if override:
        candidates.append(Path(override).expanduser())

    env_path = os.environ.get(ENV_CODEX_PATH)
    if env_path:
        candidates.append(Path(env_path).expanduser())

    on_path = shutil.which("codex")
    if on_path:
        candidates.append(Path(on_path))

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
        if base.is_dir():
            candidates.extend(
                sorted(base.glob("*/codex.exe"), key=_version_key, reverse=True)
            )
        candidates.append(Path(local_app_data) / "Programs" / "codex" / "codex.exe")

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    raise CodexNotFoundError(
        "Codex CLI executable not found. Pass --codex <path>, set "
        f"{ENV_CODEX_PATH}, or install Codex so that `codex` is on PATH."
    )


def codex_version(executable: Path, *, timeout: float = 60.0) -> str:
    """Return the version string reported by ``codex --version``."""
    completed = subprocess.run(
        [str(executable), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    text = (completed.stdout or completed.stderr or "").strip()
    match = re.search(r"(\d+\.\d+\.\d+[^\s]*)", text)
    return match.group(1) if match else text.splitlines()[0] if text else "unknown"


def codex_login_status(executable: Path, *, timeout: float = 60.0) -> tuple[bool, str]:
    """Return ``(logged_in, description)`` for the Codex account."""
    completed = subprocess.run(
        [str(executable), "login", "status"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    output = ((completed.stdout or "") + (completed.stderr or "")).strip()
    lowered = output.lower()
    logged_in = completed.returncode == 0 and "not logged in" not in lowered
    return logged_in, output


def ensure_python_on_path() -> str | None:
    """Make the running interpreter's directory resolvable for child processes.

    Acceptance commands are written by users (``python -m pytest``, ``python
    script.py``). When the controller itself was started from a shell whose
    ``PATH`` predates the Python installation, those commands fail with exit
    code 9009 / "command not found" even though the project is fine. Prepending
    the interpreter's own directory keeps such commands working.
    """
    executable = sys.executable
    if not executable:
        return None
    directory = str(Path(executable).parent)
    current = os.environ.get("PATH", "")
    parts = [part for part in current.split(os.pathsep) if part]
    if not any(part.rstrip("\\/").lower() == directory.rstrip("\\/").lower() for part in parts):
        os.environ["PATH"] = os.pathsep.join([directory, *parts])
    return executable
