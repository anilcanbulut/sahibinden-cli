"""Import every module in the project. No network.

This exists because of a real failure: `tools/handshake.py` imported a
`describe` function that had long since become a method on Config. Nothing
imported that script, so nothing noticed — until a session expired and the one
command that fixes it died on an ImportError instead of opening a browser.

The scripts under tools/ are all guarded by `if __name__ == "__main__"`, so
importing them runs their imports and constants and nothing else. That is the
whole point: a stale import is a bug you want to find on a quiet afternoon, not
at the moment you are locked out.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

#: Nothing here talks to the network at import time; anything that ever does
#: belongs on this list rather than silently making the suite fetch things.
SKIP = {"__init__", "__main__"}


def package_modules() -> list[str]:
    names = []
    for path in sorted((ROOT / "src" / "sahibinden").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ROOT / "src").with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts.pop()
        # __main__ is the console entry point — importing it runs the CLI.
        if parts[-1] in SKIP:
            continue
        names.append(".".join(parts))
    return names


def script_paths() -> list[Path]:
    return [p for p in sorted((ROOT / "tools").glob("*.py"))
            if p.stem not in SKIP and p.name != Path(__file__).name]


def main() -> int:
    failures = []

    def check(label, fn):
        try:
            fn()
        except Exception:
            failures.append(label)
            print(f"  FAIL  {label}")
            print("        " + traceback.format_exc().strip().replace("\n", "\n        "))
        else:
            print(f"  PASS  {label}")

    print("\n1. package modules")
    for name in package_modules():
        check(name, lambda n=name: importlib.import_module(n))

    print("\n2. tools scripts")
    for path in script_paths():
        def load(p=path):
            spec = importlib.util.spec_from_file_location(f"_tool_{p.stem}", p)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        check(f"tools/{path.name}", load)

    print("\n" + "=" * 62)
    print("FAILURES:" if failures else "all modules import",
          *failures, sep="\n  " if failures else "")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
