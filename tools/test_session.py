"""Offline checks for session state and the login command. No network.

Runs against a throwaway breaker file, so it never touches the real one.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sahibinden import cli  # noqa: E402
from sahibinden.fetch.guard import (Assessment, Breaker, Verdict,  # noqa: E402
                                    assess)

WALL = ROOT / "data" / "raw" / "failed"


def main() -> int:
    failures = []

    def check(label, got, want):
        ok = got == want
        if not ok:
            failures.append(label)
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<52} {got!r}"
              + ("" if ok else f"  (want {want!r})"))

    print("\n1. the breaker remembers how the last response was judged")
    path = Path(tempfile.mkdtemp()) / "breaker.json"
    breaker = Breaker(path, daily_limit=10)
    check("nothing recorded yet", breaker.last_verdict, None)
    breaker.record(Assessment(Verdict.OK, None, 200, "ok", listing_count=20))
    check("an ok request", breaker.last_verdict["verdict"], "ok")
    breaker.record(Assessment(Verdict.LOGIN_WALL, "sahibinden", 200,
                              "sign-in required (G140P9PJ-0823)"))
    check("then a walled one", breaker.last_verdict["verdict"], "login_wall")
    # Written to disk, not just held in memory: the point of recording it is
    # that the *next* process can answer "is the session dead?" for free.
    check("survives a reload", Breaker(path).last_verdict["verdict"], "login_wall")

    print("\n2. status turns that into a sentence, and only blames the session "
          "when it is the session")
    text, fix = cli.session_note(None)
    check("no history", "unknown" in text, True)
    check("and nothing to do about it", fix, None)
    text, fix = cli.session_note({"verdict": "ok", "when": "2026-08-23 10:00"})
    check("signed in", text.startswith("signed in"), True)
    check("nothing to fix", fix, None)
    text, fix = cli.session_note({"verdict": "login_wall", "when": "2026-08-23 19:25"})
    check("expired is called expired", text.startswith("EXPIRED"), True)
    check("with the time it died", "19:25" in text, True)
    check("and the way out", "sahibinden-cli login" in (fix or ""), True)
    # A ban and a markup change are not login problems. Telling someone to sign
    # in again when the breaker tripped sends them to fix the wrong thing.
    text, fix = cli.session_note({"verdict": "challenged", "when": "x",
                                  "reason": "cloudflare marker in body"})
    check("a challenge is reported, not misdiagnosed", fix, None)
    check("with its reason", "cloudflare" in text, True)

    print("\n3. the real wall page is still recognised as one")
    walls = sorted(WALL.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
    sample = next((p for p in walls if p.stat().st_size < 150_000), None)
    if sample is None:
        print("  SKIP  no saved wall page under data/raw/failed/")
    else:
        html = sample.read_text(encoding="utf-8", errors="replace")
        check(f"{sample.name[:34]}...", assess(200, {}, html).verdict,
              Verdict.LOGIN_WALL)

    print("\n4. login reports what happened in its exit code")
    calls = {}

    def fake_sign_in(*, timeout, log):
        calls["timeout"] = timeout
        log("pretending")
        return dict(calls["result"])

    original = None
    import sahibinden.fetch.session as session_module
    original, session_module.sign_in = session_module.sign_in, fake_sign_in
    try:
        args = argparse.Namespace(json=True, timeout=15)
        calls["result"] = {"ok": True, "stage": "verified"}
        check("a good login", cli.cmd_login(args), cli.EXIT_OK)
        check("minutes reach the flow as seconds", calls["timeout"], 900)
        calls["result"] = {"ok": False, "stage": "verified"}
        check("signed in but still walled is an error", cli.cmd_login(args),
              cli.EXIT_ERROR)
        # Refused is its own code so a caller waits instead of retrying into a
        # cooldown it cannot clear by trying harder.
        calls["result"] = {"ok": False, "stage": "refused"}
        check("cooling down is a refusal", cli.cmd_login(args), cli.EXIT_REFUSED)
        calls["result"] = {"ok": False, "stage": "timeout"}
        check("giving up waiting is an error", cli.cmd_login(args), cli.EXIT_ERROR)
    finally:
        session_module.sign_in = original

    print("\n" + "=" * 62)
    print("FAILURES:" if failures else "all checks passed",
          *failures, sep="\n  " if failures else "")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
