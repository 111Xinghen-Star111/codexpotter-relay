"""Filesystem locations for CodexPotter Relay.

Every path the Relay uses is resolved here so that production data, test data,
logs and per-project handoff packages each have exactly one documented home.
Set ``CODEXPOTTER_RELAY_HOME`` to relocate the whole state directory (used by
the test-suite so it never touches a real installation).
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "CODEXPOTTER_RELAY_HOME"
ENV_CODEX_HOME = "CODEX_HOME"

#: Project-side directory that holds handoff packages. It is always git-ignored
#: inside managed projects so relay bookkeeping never pollutes user commits.
PROJECT_STATE_DIR = ".relay"


def relay_home() -> Path:
    """Return the directory that holds database, logs and configuration."""
    override = os.environ.get(ENV_HOME)
    if override:
        return Path(override).expanduser().resolve()

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "CodexPotterRelay"

    # Non-Windows fallback so the controller stays usable for development.
    return Path.home() / ".codexpotter-relay"


def database_path(kind: str = "production") -> Path:
    """Return the SQLite file for ``production`` or ``test`` data.

    Production and test runs use physically separate databases so a test can
    never append rows to a real work chain.
    """
    if kind == "production":
        return relay_home() / "relay.db"
    if kind == "test":
        return relay_home() / "relay-test.db"
    raise ValueError(f"unknown database kind: {kind!r}")


def config_path() -> Path:
    """Return the configuration file path (created on first ``relay init``)."""
    return relay_home() / "relay.toml"


def log_dir() -> Path:
    """Return the directory that holds controller and test logs."""
    return relay_home() / "logs"


def chains_dir() -> Path:
    """Return the directory holding per-chain scratch data (not the project)."""
    return relay_home() / "chains"


def test_workspace_root() -> Path:
    """Return the isolated root for temporary projects used by Level 2/3 tests."""
    return relay_home() / "test-workspaces"


def codex_home() -> Path:
    """Return the Codex home directory (subject to ``CODEX_HOME``)."""
    override = os.environ.get(ENV_CODEX_HOME)
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".codex"


def project_state_dir(project_root: Path) -> Path:
    """Return the relay state directory inside a managed project."""
    return project_root / PROJECT_STATE_DIR


def ensure_directory(path: Path) -> Path:
    """Create ``path`` (and parents) if missing and return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path
