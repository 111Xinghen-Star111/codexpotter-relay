"""Package the Relay as a Codex plugin.

A Codex plugin is a directory with a ``plugin.json`` manifest, an ``mcp.json``
server declaration and optional ``skills/``. Plugins come from a *marketplace*,
which is a directory containing ``.agents/plugins/marketplace.json``.

This module renders that tree into the relay home directory, registers it with
``codex plugin marketplace add`` plus ``codex plugin add``, and removes it again.
Rendering (rather than shipping static files) lets the manifest embed the real
interpreter path and package root, so the plugin works without the user editing
anything.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codex_env import find_codex_executable
from .paths import codex_home, relay_home

MARKETPLACE_NAME = "codexpotter-relay-local"
PLUGIN_NAME = "codexpotter-relay"
PLUGIN_VERSION = "0.1.0"

PLUGIN_DESCRIPTION = (
    "Run a Codex work chain unattended: when the context window fills, the relay "
    "opens a brand new Codex session, hands the work over and keeps going."
)


@dataclass(frozen=True)
class PluginPaths:
    """Filesystem locations of the rendered marketplace."""

    marketplace_root: Path
    plugin_root: Path

    @property
    def manifest(self) -> Path:
        """Path of the marketplace manifest."""
        return self.marketplace_root / ".agents" / "plugins" / "marketplace.json"


def plugin_paths(root: Path | None = None) -> PluginPaths:
    """Return the rendered marketplace/plugin locations."""
    base = (root or (relay_home() / "plugin-marketplace")).resolve()
    return PluginPaths(marketplace_root=base, plugin_root=base / "plugins" / PLUGIN_NAME)


def package_root() -> Path:
    """Return the directory that must be on ``PYTHONPATH`` to import ``relay``."""
    return Path(__file__).resolve().parents[1]


def render_plugin(root: Path | None = None) -> PluginPaths:
    """Write the marketplace and plugin tree; returns the rendered paths."""
    paths = plugin_paths(root)
    plugin = paths.plugin_root
    (paths.marketplace_root / ".agents" / "plugins").mkdir(parents=True, exist_ok=True)
    (plugin / "skills" / "relay").mkdir(parents=True, exist_ok=True)
    (plugin / ".codex-plugin").mkdir(parents=True, exist_ok=True)

    manifest = {
        "name": MARKETPLACE_NAME,
        "interface": {"displayName": "CodexPotter Relay (local)"},
        "plugins": [
            {
                "name": PLUGIN_NAME,
                "source": {"source": "local", "path": f"./plugins/{PLUGIN_NAME}"},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Productivity",
            }
        ],
    }
    _write_json(paths.manifest, manifest)

    plugin_manifest = {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        "name": PLUGIN_NAME,
        "version": PLUGIN_VERSION,
        "description": PLUGIN_DESCRIPTION,
        "author": {"name": "CodexPotter Relay"},
        "extensions": {
            "com.openai": {
                "requires_local_executor": True,
                "interface": {
                    "displayName": "CodexPotter Relay",
                    "shortDescription": "自动跨会话接力开发",
                    "longDescription": (
                        "提交一次开发目标，Relay 在后台持续执行；上下文接近上限时自动创建"
                        "全新 Codex 会话并交接未完成工作，直到任务完成并生成报告。"
                    ),
                    "developerName": "CodexPotter Relay",
                    "category": "Productivity",
                    "capabilities": ["Local", "Automation"],
                    "defaultPrompt": "用 relay 接手这个仓库：先 relay_doctor，再 relay_start 一个目标。",
                },
            }
        },
    }
    _write_json(plugin / "plugin.json", plugin_manifest)
    _write_json(
        plugin / ".codex-plugin" / "plugin.json",
        {
            "name": PLUGIN_NAME,
            "version": PLUGIN_VERSION,
            "description": PLUGIN_DESCRIPTION,
            "interface": {
                "displayName": "CodexPotter Relay",
                "shortDescription": "自动跨会话接力开发",
                "category": "Productivity",
            },
        },
    )

    interpreter = sys.executable or "python"
    mcp_manifest = {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
        "mcpServers": {
            PLUGIN_NAME: {
                "type": "stdio",
                "command": interpreter,
                "args": ["-m", "relay.mcp"],
                "cwd": "${PLUGIN_ROOT}",
                "env": {
                    "PYTHONPATH": str(package_root()),
                    "PYTHONIOENCODING": "utf-8",
                },
            }
        },
    }
    _write_json(plugin / "mcp.json", mcp_manifest)
    (plugin / "skills" / "relay" / "SKILL.md").write_text(_skill_text(), encoding="utf-8")
    return paths


def _skill_text() -> str:
    """Return the ``$relay`` skill body shipped inside the plugin."""
    return """---
name: relay
description: Use only when the user explicitly asks for $relay, "接力开发", or wants one
  objective carried out unattended across context-window handoffs.
---

# CodexPotter Relay

The relay runs a development objective in the background as a *work chain*. When
one Codex session approaches its context limit, the relay writes a checkpoint and
starts a brand new session that continues from the checkpoint — no user prompts
in between.

Use the tools from the `codexpotter-relay` plugin. Never re-implement them.

## Start a chain

```text
relay_doctor                                              # prerequisites first
relay_start goal="..." project="C:\\absolute\\path"
            tasks=["T1:first step", "T2:second step"]
            accept=["npm test"]                            # completion proof
```

Always pass `accept` when the project has a test or build command: the chain will
not report completion until it passes.

## Watch it

- `relay_status` — state, sessions, handoffs, checkpoints, tasks
- `relay_report` — write and locate the markdown report
- `relay_chains` — all chains

## Control it

- `relay_pause` / `relay_resume` / `relay_stop`
- `relay_checkpoint` — snapshot now
- `relay_recover` — take over chains a crashed controller left running

## What to tell the user

- The chain runs unattended; they do not need to send "continue".
- New sessions are real new main sessions (new context), named
  `Relay <chain> #n · <task>` in the Codex session list.
- Work happens on branch `relay/<chain-id>`, never on their current branch.
- Tell them where the report is and whether acceptance passed.
"""


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _run_codex(arguments: list[str], *, timeout: float = 300.0) -> subprocess.CompletedProcess[str]:
    """Run the Codex CLI and return the completed process."""
    executable = find_codex_executable()
    return subprocess.run(
        [str(executable), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def install_plugin(root: Path | None = None) -> dict[str, Any]:
    """Render the marketplace and register the plugin with Codex."""
    paths = render_plugin(root)
    steps: list[dict[str, Any]] = []

    marketplaces = _run_codex(["plugin", "marketplace", "list"])
    already_registered = MARKETPLACE_NAME in (marketplaces.stdout or "")
    if not already_registered:
        added = _run_codex(["plugin", "marketplace", "add", str(paths.marketplace_root)])
        steps.append(
            {
                "step": "marketplace add",
                "returncode": added.returncode,
                "output": ((added.stdout or "") + (added.stderr or "")).strip()[:800],
            }
        )

    installed = _run_codex(["plugin", "add", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"])
    steps.append(
        {
            "step": "plugin add",
            "returncode": installed.returncode,
            "output": ((installed.stdout or "") + (installed.stderr or "")).strip()[:800],
        }
    )
    status = plugin_status(root)
    return {
        "marketplace_root": str(paths.marketplace_root),
        "plugin_root": str(paths.plugin_root),
        "steps": steps,
        "registered": status["registered"],
        "cache_present": status["cache_present"],
        "ok": bool(status["registered"]),
    }


def uninstall_plugin(root: Path | None = None, *, remove_marketplace: bool = True) -> dict[str, Any]:
    """Uninstall the plugin and (optionally) drop its marketplace."""
    steps: list[dict[str, Any]] = []
    removed = _run_codex(["plugin", "remove", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"])
    steps.append(
        {
            "step": "plugin remove",
            "returncode": removed.returncode,
            "output": ((removed.stdout or "") + (removed.stderr or "")).strip()[:500],
        }
    )
    if remove_marketplace:
        dropped = _run_codex(["plugin", "marketplace", "remove", MARKETPLACE_NAME])
        steps.append(
            {
                "step": "marketplace remove",
                "returncode": dropped.returncode,
                "output": ((dropped.stdout or "") + (dropped.stderr or "")).strip()[:500],
            }
        )
    paths = plugin_paths(root)
    if paths.marketplace_root.is_dir():
        shutil.rmtree(paths.marketplace_root, ignore_errors=True)
        steps.append({"step": "delete rendered tree", "returncode": 0, "output": str(paths.marketplace_root)})
    return {"steps": steps, "registered": plugin_status(root)["registered"]}


def plugin_status(root: Path | None = None) -> dict[str, Any]:
    """Report whether the plugin is registered in Codex and present in its cache."""
    config_file = codex_home() / "config.toml"
    registered = False
    if config_file.is_file():
        text = config_file.read_text(encoding="utf-8", errors="replace")
        registered = f'[plugins."{PLUGIN_NAME}@{MARKETPLACE_NAME}"]' in text
    cache_dir = codex_home() / "plugins" / "cache" / MARKETPLACE_NAME
    paths = plugin_paths(root)
    return {
        "plugin_id": f"{PLUGIN_NAME}@{MARKETPLACE_NAME}",
        "marketplace_root": str(paths.marketplace_root),
        "plugin_root": str(paths.plugin_root),
        "rendered": paths.manifest.is_file(),
        "registered": registered,
        "cache_present": cache_dir.is_dir(),
        "cache_dir": str(cache_dir),
        "codex_config": str(config_file),
    }
