"""One-time session setup for the automation profile.

Kept as a thin wrapper: the flow itself now lives in
``sahibinden.fetch.session`` so the CLI can offer it as a first-class command,
and so it is covered by the import check in ``tools/test_tools.py`` rather than
only failing the moment someone actually needs to sign in.

    python tools/handshake.py        # equivalent to: sahibinden-cli login
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sahibinden.fetch.session import sign_in  # noqa: E402


def main() -> int:
    result = sign_in()
    if result["ok"]:
        return 0
    return 2 if result["stage"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
