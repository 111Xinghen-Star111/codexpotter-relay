"""Allow ``python -m relay`` to behave exactly like the ``relay`` script."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
