"""One-time session setup for the automation profile.

sahibinden serves a sign-in wall to every cookieless session. That was verified
exhaustively: a plain incognito window on the same residential IP, a Google
referral, Turkish and English Accept-Language, deep /ilan/ links and the mobile
site were all walled. Nothing about the browser is being detected — a
fingerprint audit passed 56/56 — the site simply requires a session.

So the profile needs to be signed in once. Use a burner account, not your own:
bans here are IP-wide and self-releasing, so the account is not what absorbs the
damage, but there is no reason to attach your identity to this either.

This script hands you the keyboard. You are signing in yourself, in a real
browser window. Afterwards it spends one request to confirm the session
actually clears the wall, so you are not left guessing.

    python tools/handshake.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sahibinden.config import describe  # noqa: E402
from sahibinden.fetch.browser import DEFAULT_PROFILE_DIR, launch  # noqa: E402
from sahibinden.fetch.fetcher import BREAKER_PATH, _store_raw  # noqa: E402
from sahibinden.fetch.guard import Breaker, Verdict, assess  # noqa: E402
from sahibinden.parse.search import parse_search  # noqa: E402

START_URL = "https://www.sahibinden.com/"
VERIFY_URL = "https://www.sahibinden.com/skoda-scala"

SIGN_IN_TIMEOUT = 15 * 60
POLL_SECONDS = 3


#: Content domains. secure.sahibinden.com is the auth flow — its pages are not
#: the wall, but they are emphatically not a finished session either, and
#: navigating away from one mid-flow cancels the login.
CONTENT_HOSTS = ("www.sahibinden.com", "m.sahibinden.com")

#: Real listing pages measured 370-411KB; the sign-in wall measured 78-82KB.
#: Requiring positive bulk avoids mistaking any lightweight interstitial for a
#: working session.
MIN_CONTENT_BYTES = 150_000

#: Number of consecutive passing polls required, so a page caught mid-render
#: cannot trigger a false positive.
CONFIRMATIONS = 2


def wait_for_session(page, timeout: int = SIGN_IN_TIMEOUT) -> bool:
    """Block until the human has finished signing in, without reading stdin.

    We only inspect pages the user already loaded — no navigation, so this costs
    nothing against the budget and, critically, cannot interrupt a multi-step
    login (password -> 2FA -> content).

    Success requires positive evidence: a content-domain page, well past the
    size of any interstitial, not the wall, confirmed on consecutive polls.
    """
    import time
    from urllib.parse import urlparse

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
                print(f"\n  session confirmed on {url[:70]} ({len(html):,} bytes)")
                return True
        else:
            if streak:
                streak = 0
            if host and host not in CONTENT_HOSTS and time.time() - last_note > 20:
                print(f"    (on {host} — still in the login flow, not touching it)",
                      flush=True)
                last_note = time.time()

        if time.time() - last_note > 30:
            left = (deadline - time.time()) / 60
            print(f"    waiting for sign-in ... {left:.0f} min left", flush=True)
            last_note = time.time()

        time.sleep(POLL_SECONDS)

    return False


def main() -> int:
    breaker = Breaker(BREAKER_PATH)
    print(f"Profile : {DEFAULT_PROFILE_DIR}")
    print(f"Limits  : {describe()}")
    print(f"Breaker : {breaker.status()}\n")

    try:
        breaker.check()
    except RuntimeError as exc:
        print(f"Not now — {exc}")
        return 1

    print("A Chrome window will open. In it:\n")
    print("  1. Sign in with your BURNER account (not your personal one)")
    print("  2. Tick 'Beni hatırla' / 'Keep me signed in' if offered")
    print("  3. Click into a car listing, scroll, go back — browse like a person")
    print("\nNo need to come back here — this detects the session on its own.\n")

    # The one place the window must be visible — you are typing into it.
    with launch(offscreen=False) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(START_URL, wait_until="domcontentloaded", timeout=60_000)

        if not wait_for_session(page):
            print("\nTimed out waiting for sign-in. Nothing was fetched; rerun "
                  "when you are ready.")
            return 1

        cookies = ctx.cookies()
        print(f"\n  {len(cookies)} cookies stored for "
              f"{sorted({c['domain'] for c in cookies})}")

        print(f"\n  verifying against {VERIFY_URL} ...", flush=True)
        response = page.goto(VERIFY_URL, wait_until="domcontentloaded", timeout=60_000)
        status = response.status if response else 0
        html = page.content()
        verdict = assess(status, dict(response.headers) if response else {},
                         html, expect="search")
        breaker.record(verdict)
        _store_raw(VERIFY_URL, html, failed=not verdict.is_usable)

    print(f"  {verdict.verdict.value.upper()}  HTTP {status}  {len(html):,} bytes")

    if not verdict.is_usable:
        print(f"  {verdict.reason}\n")
        print("Session did NOT clear the wall. If you signed in successfully and")
        print("still see this, the gate is not (only) the login — tell me and we")
        print("will look at the saved page in data/raw/failed/.")
        return 2

    parsed = parse_search(html)
    print(f"\n  parsed {len(parsed['listings'])} listings, "
          f"total_results={parsed['total_results']}")
    print("\nSession established and verified. The profile persists, so the")
    print("fetcher can now run unattended.")
    print(f"\nBreaker: {breaker.status()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
