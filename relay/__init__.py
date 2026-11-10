"""CodexPotter Relay — automatic cross-session Codex development handoff.

Public entry points:

* :func:`relay.cli.main` — the ``relay`` command line interface.
* :class:`relay.controller.RelayController` — the per-chain control loop.
* :class:`relay.handoff.HandoffManager` — the PREPARE/START/VERIFY/COMMIT/CONTINUE
  state machine that moves write access between main sessions.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
