"""Seed the automation profile with sahibinden cookies from your real Chrome.

Why this exists: sahibinden serves a sign-in wall to any cookieless session —
confirmed against a plain incognito window on the same residential IP, with and
without a Google referrer. Only an established session gets through.

Attaching to the running browser over CDP would have been simpler, but Chrome
136+ refuses --remote-debugging-port on the default profile directory (an
anti-infostealer measure). So we copy the cookie store instead and let
Playwright launch against it as a normal persistent profile.

Privacy: the cookie database is copied wholesale, then every row that is not a
sahibinden cookie is deleted before anything launches. Chrome encrypts cookie
*values*, but ``host_key`` is plaintext, so filtering needs no decryption and
nothing else from your browsing ever reaches this project.

Chrome must be fully closed — the files are locked while it runs, and a copy
taken mid-write is a corrupt database.

    python tools/seed_profile.py
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sahibinden.fetch.browser import DEFAULT_PROFILE_DIR  # noqa: E402

SOURCE_ROOT = Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data"
KEEP_HOSTS = ("sahibinden.com", ".sahibinden.com")


def chrome_is_running() -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        return "chrome.exe" in out
    except Exception:
        return False


def seed() -> int:
    if not SOURCE_ROOT.exists():
        print(f"Chrome profile not found at {SOURCE_ROOT}")
        return 1

    if chrome_is_running():
        print("Chrome is still running. Close it completely first:\n")
        print("  Get-Process chrome -ErrorAction SilentlyContinue | Stop-Process -Force\n")
        print("The cookie database is locked while Chrome runs, and a copy taken")
        print("mid-write is corrupt.")
        return 1

    src_cookies = SOURCE_ROOT / "Default" / "Network" / "Cookies"
    src_state = SOURCE_ROOT / "Local State"
    if not src_cookies.exists():
        print(f"No cookie store at {src_cookies}")
        return 1

    dst_root = Path(DEFAULT_PROFILE_DIR)
    dst_cookies = dst_root / "Default" / "Network" / "Cookies"
    dst_cookies.parent.mkdir(parents=True, exist_ok=True)

    # Local State carries the wrapped encryption key. Without it the copied
    # cookie values cannot be decrypted and Chrome silently discards them.
    if src_state.exists():
        shutil.copy2(src_state, dst_root / "Local State")
        print(f"  copied Local State ({src_state.stat().st_size:,} bytes)")

    shutil.copy2(src_cookies, dst_cookies)
    print(f"  copied cookie store ({src_cookies.stat().st_size:,} bytes)")

    conn = sqlite3.connect(dst_cookies)
    try:
        total = conn.execute("SELECT COUNT(*) FROM cookies").fetchone()[0]
        placeholders = ",".join("?" for _ in KEEP_HOSTS)
        conn.execute(
            f"DELETE FROM cookies WHERE host_key NOT IN ({placeholders})", KEEP_HOSTS
        )
        conn.commit()
        kept = conn.execute("SELECT COUNT(*) FROM cookies").fetchone()[0]
        names = [r[0] for r in conn.execute("SELECT name FROM cookies").fetchall()]
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()

    print(f"\n  {total:,} cookies in source -> kept {kept} sahibinden cookies, "
          f"deleted {total - kept:,} others")
    if kept:
        print(f"  kept: {', '.join(sorted(names)[:12])}"
              f"{' ...' if len(names) > 12 else ''}")
    print(f"\nProfile seeded: {dst_root}")

    if kept == 0:
        print("\nNo sahibinden cookies found. Visit sahibinden.com in normal Chrome,")
        print("browse a listing, close Chrome, and run this again.")
        return 2

    print("\nNext: python tools/stage1_fetch.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(seed())
