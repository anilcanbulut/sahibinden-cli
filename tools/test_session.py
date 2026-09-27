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
    saved = sorted(WALL.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
    # data/raw/failed/ holds every unusable response, not only walls; the
    # site's own 404 page is small too and has its own check below.
    walls = [p for p in saved if p.stat().st_size < 150_000
             and "Hata Sayfas" not in p.read_text(encoding="utf-8", errors="replace")]
    sample = walls[0] if walls else None
    if sample is None:
        print("  SKIP  no saved wall page under data/raw/failed/")
    else:
        html = sample.read_text(encoding="utf-8", errors="replace")
        check(f"{sample.name[:34]}...", assess(200, {}, html).verdict,
              Verdict.LOGIN_WALL)

    print("\n3b. a wrong address is a 404, not a ban")
    # Before this verdict existed the error page failed the size test and
    # tripped a 90-minute cooldown over a misspelt district.
    missing = ("<html><head><title>sahibinden.com Hata Sayfası</title></head>"
               "<body><h1>Aradığınız sayfaya <strong>ulaşılamadı.</strong></h1>"
               "</body></html>")
    check("the site's 404 page", assess(404, {}, missing).verdict, Verdict.NOT_FOUND)
    check("does not halt the breaker", assess(404, {}, missing).should_halt, False)
    check("the same words with HTTP 200 are not trusted",
          assess(200, {}, missing).verdict, Verdict.CHALLENGED)
    check("a challenge served as a 404 is still a challenge",
          assess(404, {}, missing + "Just a moment...").verdict, Verdict.CHALLENGED)
    # The block page shares the 404 page's title. Whatever status it comes
    # with, its wording must win, or a ban could be read as "not found" and
    # the run would carry on into it.
    blocked = ("<html><head><title>sahibinden.com Hata Sayfası</title></head><body>"
               "Olağan dışı erişim tespit ettik... Cihazınızdan ya da bağlı "
               "olduğunuz ağdan sitemize olağan dışı (otomatik) erişim yapılmaya "
               "çalışıldığını görüyoruz. Destek kodu GTDYXB07-0927</body></html>")
    for code in (429, 404, 200):
        check(f"the block page with HTTP {code} halts",
              assess(code, {}, blocked).should_halt, True)
    # A search that matches nothing has the form but no results table; it is
    # an answer (zero listings), not a redesign of the site.
    nothing = ('<form id="searchResultsSearchForm"></form>' + " " * 30_000)
    check("zero results is empty, not changed markup",
          assess(200, {}, nothing).verdict, Verdict.EMPTY)
    check("a page with neither is still changed markup",
          assess(200, {}, " " * 30_000).verdict, Verdict.CHANGED)
    breaker = Breaker(Path(tempfile.mkdtemp()) / "breaker.json")
    breaker.record(assess(404, {}, missing))
    check("and it never starts a cooldown", breaker.cooling_down, False)
    # Chosen by the 404 sentence, not the title: the block page shares it.
    for page in [p for p in saved if "Aradığınız sayfaya" in
                 p.read_text(encoding="utf-8", errors="replace")][:1]:
        check(f"saved {page.name[:34]}...",
              assess(404, {}, page.read_text(encoding="utf-8", errors="replace")).verdict,
              Verdict.NOT_FOUND)

    print("\n4. login reports what happened in its exit code")
    calls = {}

    def fake_sign_in(*, timeout, log, attach=False):
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

    print("\n5. a cooldown can be ended early, and the block is still remembered")
    block = Assessment(Verdict.CHALLENGED, "cloudflare", 403, "test block")
    path = Path(tempfile.mkdtemp()) / "breaker.json"
    breaker = Breaker(path, daily_limit=10)
    breaker.record(block)
    check("a block starts a cooldown", breaker.cooling_down, True)
    cleared = breaker.clear_cooldown()
    check("which reports what was left", cleared["minutes_left"], 90)
    check("and why it started", "test block" in (cleared["reason"] or ""), True)
    check("then ends", Breaker(path).cooling_down, False)
    try:
        Breaker(path).check()
        allowed = True
    except RuntimeError:
        allowed = False
    check("so requests are allowed again", allowed, True)
    # Ending the wait is not a verdict on the rate: the block still counts.
    check("waits stay widened", Breaker(path).delay_multiplier, 1.75)
    breaker = Breaker(path, daily_limit=10)
    breaker.record(block)
    check("a second block climbs to 4 hours",
          breaker._state["trips"][-1]["cooldown_seconds"], 4 * 60 * 60)
    cleared = breaker.clear_cooldown(forget_trips=True)
    check("forgetting counts both blocks", cleared["trips_forgiven"], 2)
    check("waits back to normal", Breaker(path).delay_multiplier, 1.0)
    check("the blocks stay on disk", len(Breaker(path)._state["trips"]), 2)
    breaker = Breaker(path, daily_limit=10)
    breaker.record(block)
    check("the next block starts at 90 minutes again",
          breaker._state["trips"][-1]["cooldown_seconds"], 90 * 60)
    Breaker(path).clear_cooldown()
    check("nothing to end is not an error",
          Breaker(path).clear_cooldown()["was_cooling_down"], False)

    real_path, cli.BREAKER_PATH = cli.BREAKER_PATH, path
    try:
        Breaker(path).record(block)
        args = argparse.Namespace(json=True, forget_blocks=False)
        check("the command succeeds", cli.cmd_reset_cooldown(args), cli.EXIT_OK)
        check("and ends the cooldown", Breaker(path).cooling_down, False)
    finally:
        cli.BREAKER_PATH = real_path

    print("\n" + "=" * 62)
    print("FAILURES:" if failures else "all checks passed",
          *failures, sep="\n  " if failures else "")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
