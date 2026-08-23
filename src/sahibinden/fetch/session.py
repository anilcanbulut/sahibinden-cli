"""Establishing the browser session the fetcher runs on.

sahibinden serves a sign-in wall to every cookieless session. That was verified
exhaustively: a plain incognito window on the same residential IP, a Google
referral, Turkish and English Accept-Language, deep /ilan/ links and the mobile
site were all walled. Nothing about the browser is being detected — a
fingerprint audit passed 56/56 — the site simply requires a session.

So the profile has to be signed in by a human, in a real window. That is what
this module drives; it never types for you, and it never reads stdin, because
the login is multi-step (password -> 2FA -> content) and any navigation of ours
mid-flow cancels it.

Sessions expire. When one does, every fetch comes back LOGIN_WALL and the run
ends itself — `sahibinden-cli login` is how you get back.
"""

from __future__ import annotations

import time
from urllib.parse import urlparse

from ..config import config
from .browser import DEFAULT_PROFILE_DIR, launch
from .fetcher import BREAKER_PATH, _store_raw
from .guard import Breaker, Verdict, assess

START_URL = "https://www.sahibinden.com/"

#: A cheap, stable search page. Deliberately not a listing we track, so the
#: verification request cannot be mistaken for data collection.
VERIFY_URL = "https://www.sahibinden.com/skoda-scala"

SIGN_IN_TIMEOUT = 15 * 60
POLL_SECONDS = 3

#: Content domains. secure.sahibinden.com is the auth flow — its pages are not
#: the wall, but they are emphatically not a finished session either, and
#: navigating away from one mid-flow cancels the login.
CONTENT_HOSTS = ("www.sahibinden.com", "m.sahibinden.com")

#: Real listing pages measured 370-411KB; the sign-in wall measured 78-101KB.
#: Requiring positive bulk avoids mistaking any lightweight interstitial for a
#: working session.
MIN_CONTENT_BYTES = 150_000

#: Consecutive passing polls required, so a page caught mid-render cannot
#: trigger a false positive.
CONFIRMATIONS = 2


def wait_for_session(page, timeout: int = SIGN_IN_TIMEOUT, *, log=print) -> bool:
    """Block until the human has finished signing in, without reading stdin.

    We only inspect pages the user already loaded — no navigation, so this costs
    nothing against the budget and cannot interrupt a multi-step login.

    Success requires positive evidence: a content-domain page, well past the
    size of any interstitial, not the wall, confirmed on consecutive polls.
    """
    deadline = time.time() + timeout
    last_note = 0.0
    streak = 0

    while time.time() < deadline:
        try:
            url, html = page.url, page.content()
        except Exception:
            time.sleep(POLL_SECONDS)  # mid-navigation
            continue

        host = (urlparse(url).hostname or "").lower()
        looks_done = (
            host in CONTENT_HOSTS
            and len(html) >= MIN_CONTENT_BYTES
            and assess(200, {}, html, expect="search").verdict is not Verdict.LOGIN_WALL
        )

        if looks_done:
            streak += 1
            if streak >= CONFIRMATIONS:
                log(f"\n  session confirmed on {url[:70]} ({len(html):,} bytes)")
                return True
        else:
            if streak:
                streak = 0
            if host and host not in CONTENT_HOSTS and time.time() - last_note > 20:
                log(f"    (on {host} — still in the login flow, not touching it)")
                last_note = time.time()

        if time.time() - last_note > 30:
            left = (deadline - time.time()) / 60
            log(f"    waiting for sign-in ... {left:.0f} min left")
            last_note = time.time()

        time.sleep(POLL_SECONDS)

    return False


def sign_in(*, timeout: int = SIGN_IN_TIMEOUT, log=print) -> dict:
    """Open a real Chrome window, wait for a human login, then verify it.

    Returns a result dict; ``ok`` is only true once a real search page has come
    back through the same code path the fetcher uses, so a session that looks
    established but still gets walled is reported as a failure, not a success.
    """
    breaker = Breaker(BREAKER_PATH)
    result = {"ok": False, "profile": str(DEFAULT_PROFILE_DIR), "stage": "start",
              "cookies": 0, "verdict": None, "reason": "", "listings": None,
              "breaker": breaker.status()}

    log(f"Profile : {DEFAULT_PROFILE_DIR}")
    log(f"Limits  : {config.describe()}")
    log(f"Breaker : {breaker.status()}\n")

    try:
        breaker.check()
    except RuntimeError as exc:
        result.update(stage="refused", reason=str(exc))
        log(f"Not now — {exc}")
        return result

    log("A Chrome window will open. In it:\n")
    log("  1. Sign in with your BURNER account (not your personal one)")
    log("  2. Tick 'Beni hatırla' / 'Keep me signed in' if offered")
    log("  3. Click into a car listing, scroll, go back — browse like a person")
    log("\nNo need to come back here — this detects the session on its own.\n")

    # The one place the window must be visible — you are typing into it.
    with launch(offscreen=False) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(START_URL, wait_until="domcontentloaded", timeout=60_000)

        if not wait_for_session(page, timeout, log=log):
            result.update(stage="timeout",
                          reason="timed out waiting for sign-in; nothing was fetched")
            log("\nTimed out waiting for sign-in. Nothing was fetched; rerun "
                "when you are ready.")
            return result

        cookies = ctx.cookies()
        result["cookies"] = len(cookies)
        log(f"\n  {len(cookies)} cookies stored for "
            f"{sorted({c['domain'] for c in cookies})}")

        log(f"\n  verifying against {VERIFY_URL} ...")
        response = page.goto(VERIFY_URL, wait_until="domcontentloaded", timeout=60_000)
        status = response.status if response else 0
        html = page.content()
        verdict = assess(status, dict(response.headers) if response else {},
                         html, expect="search")
        breaker.record(verdict)
        _store_raw(VERIFY_URL, html, failed=not verdict.is_usable)

    result.update(stage="verified", verdict=verdict.verdict.value,
                  reason=verdict.reason, status=status, bytes=len(html),
                  breaker=breaker.status())
    log(f"  {verdict.verdict.value.upper()}  HTTP {status}  {len(html):,} bytes")

    if not verdict.is_usable:
        log(f"  {verdict.reason}\n")
        log("Session did NOT clear the wall. If you signed in successfully and")
        log("still see this, the gate is not (only) the login — the page we got")
        log("is saved under data/raw/failed/, with a screenshot beside it.")
        return result

    from ..parse.search import parse_search

    parsed = parse_search(html)
    result.update(ok=True, listings=len(parsed["listings"]),
                  total_results=parsed["total_results"])
    log(f"\n  parsed {len(parsed['listings'])} listings, "
        f"total_results={parsed['total_results']}")
    log("\nSession established and verified. The profile persists, so the")
    log("fetcher can now run unattended.")
    log(f"\nBreaker: {breaker.status()}")
    return result
