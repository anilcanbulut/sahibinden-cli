#!/usr/bin/env python
"""Entry point so the tool runs as `python sahibinden.py ...` from the repo root
without needing PYTHONPATH set or the package installed."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from sahibinden.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
