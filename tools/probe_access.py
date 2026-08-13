"""Find out which sahibinden URLs a warm-ish profile can actually reach.

The sign-in wall appeared on the homepage and on a filtered search URL. That
does not prove every path is gated: sites commonly wall the entry page while
letting deep links through, because search-engine traffic lands on deep links.

Unlike the fetcher, this keeps going after a LOGIN_WALL — the point is to map
which paths work. It still stops dead on CHALLENGED or BLOCKED, since those are
the verdicts that mean "stop asking".
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sahibinden.fetch.browser import launch  # noqa: E402
from sahibinden.fetch.fetcher import BREAKER_PATH, _store_raw  # noqa: E402
from sahibinden.fetch.guard import Breaker, Verdict, assess  # noqa: E402

PROBES = [
    ("homepage", "https://www.sahibinden.com/", "search"),
    ("deep listing", "https://www.sahibinden.com/ilan/vasita-otomobil-skoda-"
                     "beyoglu-motorstan-skoda-scala-1.0-tsi-elite-hatasiz-"
                     "boyasiz-1331176770/detay", "detail"),
    ("plain search", "https://www.sahibinden.com/skoda-scala", "search"),
]


MOBILE_PROBES = [
    ("mobile home", "https://m.sahibinden.com/", "search"),
    ("mobile search", "https://m.sahibinden.com/skoda-scala", "search"),
]


def main() -> int:
    if "--mobile" in sys.argv:
        PROBES[:] = MOBILE_PROBES
    breaker = Breaker(BREAKER_PATH, daily_limit=200)
    print(f"Budget remaining today: {breaker.remaining}\n")

    findings = []
    with launch() as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        for index, (label, url, expect) in enumerate(PROBES):
            try:
                breaker.check()
            except RuntimeError as exc:
                print(f"STOPPED: {exc}")
                break

            if index:
                delay = random.uniform(12, 25)
                print(f"    waiting {delay:.1f}s ...", flush=True)
                time.sleep(delay)

            print(f"[{label}] {url[:78]}", flush=True)
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except Exception as exc:
                print(f"    navigation failed: {type(exc).__name__}\n")
                continue

            status = response.status if response else 0
            html = page.content()
            verdict = assess(status, dict(response.headers) if response else {},
                             html, expect=expect)
            breaker.record(verdict)
            findings.append((label, verdict))

            path = _store_raw(url, html, failed=not verdict.is_usable)
            print(f"    {verdict.verdict.value.upper():<11} HTTP {status}  "
                  f"{len(html):>8,} bytes  rows={verdict.listing_count}")
            print(f"    {verdict.reason}")
            print(f"    saved: {path.name}\n", flush=True)

            if verdict.should_halt:
                print("!! CHALLENGED/BLOCKED — stopping immediately, no retry.")
                break

    print("=" * 62)
    for label, verdict in findings:
        mark = "REACHABLE" if verdict.is_usable else verdict.verdict.value.upper()
        print(f"  {label:<16} {mark}")

    usable = [lbl for lbl, v in findings if v.is_usable]
    if usable:
        print(f"\nWorking paths: {', '.join(usable)}")
        print("The wall is not total — we can build around these.")
    else:
        print("\nEvery path walled. A real session is required; "
              "see the options in tools/seed_profile.py and tools/handshake.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
