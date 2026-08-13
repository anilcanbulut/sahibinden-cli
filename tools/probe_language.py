"""Is the sign-in wall about a cold session, or about looking foreign?

The wall page served to us links its homepage as /en and is titled in English.
We had set Accept-Language to en-US,en,tr to mirror the user's real Chrome — but
that browser reads sahibinden in Turkish, which it gets from a site cookie, not
from the header. So every walled attempt so far was *both* cookieless *and*
announcing English to a Turkey-only marketplace.

This isolates the second variable: a brand-new profile, equally cookieless, but
advertising tr-TR. If it gets through, language was the discriminator and no
cookie transplant is needed.

Each candidate uses a throwaway profile so no state leaks between them.
"""

from __future__ import annotations

import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sahibinden.fetch.browser import launch  # noqa: E402
from sahibinden.fetch.fetcher import BREAKER_PATH, _store_raw  # noqa: E402
from sahibinden.fetch.guard import Breaker, assess  # noqa: E402

URL = "https://www.sahibinden.com/skoda-scala"

CANDIDATES = [
    ("tr-TR only", "tr-TR"),
    ("tr-TR,tr,en", "tr-TR,tr,en-US,en"),
]


def main() -> int:
    breaker = Breaker(BREAKER_PATH, daily_limit=200)
    print(f"Budget remaining today: {breaker.remaining}\n")

    results = []
    for index, (label, langs) in enumerate(CANDIDATES):
        try:
            breaker.check()
        except RuntimeError as exc:
            print(f"STOPPED: {exc}")
            break

        if index:
            delay = random.uniform(15, 28)
            print(f"    waiting {delay:.1f}s ...\n", flush=True)
            time.sleep(delay)

        profile = Path(tempfile.mkdtemp(prefix="sahi-lang-"))
        print(f"[{label}]  Accept-Language: {langs}", flush=True)
        try:
            with launch(profile, accept_languages=langs) as ctx:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                response = page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
                status = response.status if response else 0
                html = page.content()
                verdict = assess(status, dict(response.headers) if response else {},
                                 html, expect="search")
                breaker.record(verdict)
                path = _store_raw(f"lang-{langs}", html, failed=not verdict.is_usable)

            results.append((label, verdict))
            print(f"    {verdict.verdict.value.upper():<11} HTTP {status}  "
                  f"{len(html):>8,} bytes  rows={verdict.listing_count}")
            print(f"    {verdict.reason}")
            print(f"    saved: {path.name}\n", flush=True)

            if verdict.should_halt:
                print("!! CHALLENGED/BLOCKED — stopping immediately.")
                break
        finally:
            shutil.rmtree(profile, ignore_errors=True)

    print("=" * 62)
    for label, verdict in results:
        print(f"  {label:<14} {'REACHABLE' if verdict.is_usable else verdict.verdict.value.upper()}")

    if any(v.is_usable for _, v in results):
        print("\nLanguage was the discriminator. No cookie transplant needed —")
        print("set ACCEPT_LANGUAGES to the winning value in browser.py.")
    else:
        print("\nStill walled with Turkish. The gate is session state, not language.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
