"""Allow ``python -m openzap``."""

from __future__ import annotations

from openzap.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
