"""The single primitive every crawl goes through: fetch one page, safely.

Invariants this module enforces, so no caller can forget them:

1. The breaker is consulted *before* the request and updated *after*.
2. Every response is assessed before any parser touches it.
3. Nothing is ever retried. A challenge ends the run.
4. Raw bytes are written to disk before parsing, so a parser bug never costs
   a re-fetch.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from pathlib import Path

from contextlib import contextmanager

from .browser import connect, launch
from .guard import Assessment, Breaker, assess

from ..config import config

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
BREAKER_PATH = PROJECT_ROOT / "data" / "breaker.json"

#: Time spent on a page before moving on. Zero-dwell page views are not what
#: humans generate.
MIN_DWELL = 2.5
MAX_DWELL = 6.0


@dataclass
class FetchResult:
    url: str
    assessment: Assessment
    html: str | None
    raw_path: Path | None

    @property
    def ok(self) -> bool:
        return self.assessment.is_usable


def _slugify(url: str) -> str:
    keep = "".join(c if c.isalnum() else "-" for c in url.split("://", 1)[-1])
    return keep.strip("-")[:120]


def _store_raw(url: str, html: str, *, failed: bool = False) -> Path:
    """Always store, especially on failure — that is when we need the evidence.

    Storing only successes meant the one response worth investigating was the
    one thrown away, and diagnosing it cost another request.
    """
    target = RAW_DIR / "failed" if failed else RAW_DIR
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{time.strftime('%Y%m%dT%H%M%S')}_{_slugify(url)}.html"
    path.write_text(html, encoding="utf-8")
    return path


def _dwell(page) -> None:
    """Look at the page like a person would before navigating away."""
    page.wait_for_timeout(int(random.uniform(MIN_DWELL, MAX_DWELL) * 1000))
    try:
        page.mouse.wheel(0, random.randint(400, 1400))
        page.wait_for_timeout(int(random.uniform(0.8, 2.0) * 1000))
    except Exception:
        pass  # scrolling is cosmetic; never fail a fetch over it


@contextmanager
def _session(attach: bool):
    """A page to work in, plus the promise that we leave the browser as we found it.

    When attached to the user's own Chrome we open our own tab and close only
    that tab; their windows and their browser stay untouched.
    """
    opener = connect() if attach else launch()
    with opener as ctx:
        page = ctx.new_page() if attach else (ctx.pages[0] if ctx.pages else ctx.new_page())
        try:
            yield page
        finally:
            if attach:
                try:
                    page.close()
                except Exception:
                    pass


def fetch_pages(
    urls: list[str],
    *,
    expect: str = "search",
    daily_limit: int | None = None,
    breaker_path: Path | None = None,
    attach: bool = False,
) -> list[FetchResult]:
    """Fetch URLs in one browser session, stopping dead on the first problem.

    Defaults to launching our own profile, which ``tools/seed_profile.py`` has
    seeded with real sahibinden cookies — that is what gets past the sign-in
    wall. ``attach=True`` drives an already-running Chrome over CDP instead, but
    Chrome 136+ refuses the debugging port on the default profile directory, so
    it is only usable against a browser started on a non-default --user-data-dir.
    """
    breaker = Breaker(breaker_path or BREAKER_PATH,
                      daily_limit=daily_limit or config.daily_budget)
    results: list[FetchResult] = []

    with _session(attach) as page:
        for index, url in enumerate(urls):
            breaker.check()  # raises if tripped or out of budget

            # Pace against the last request on record, not against this batch's
            # index — otherwise the first request of every batch goes out with
            # no gap. Widened by the breaker's backoff, so past trips slow us
            # down permanently rather than being forgotten after the cooldown.
            target = (random.uniform(config.min_delay, config.max_delay)
                      * breaker.delay_multiplier)
            pause = breaker.sleep_needed(target)
            if pause > 0.5:
                print(f"    waiting {pause:.1f}s ...", flush=True)
                time.sleep(pause)

            print(f"  -> {url}", flush=True)
            response = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            status = response.status if response else 0
            headers = dict(response.headers) if response else {}
            html = page.content()

            verdict = assess(status, headers, html, expect=expect)
            breaker.record(verdict)

            raw_path = _store_raw(url, html, failed=not verdict.is_usable)
            if not verdict.is_usable:
                # A screenshot answers "what did the browser actually see?" far
                # faster than reading 80KB of markup.
                try:
                    page.screenshot(path=str(raw_path.with_suffix(".png")), full_page=False)
                except Exception:
                    pass
            results.append(FetchResult(url, verdict, html, raw_path))

            print(
                f"     {verdict.verdict.value.upper()} "
                f"(HTTP {status}, {len(html):,} bytes, {verdict.listing_count} rows) "
                f"{verdict.reason}",
                flush=True,
            )

            # Two different stops. A ban trips the breaker and needs a human to
            # clear it; a login wall or markup change is a config problem, so we
            # end the run without poisoning tomorrow's.
            if verdict.should_halt:
                print("     !! breaker TRIPPED — stopping, no retry", flush=True)
                break
            if not verdict.is_usable:
                print("     -- unusable response, ending run (breaker not tripped)", flush=True)
                break

            _dwell(page)

    return results
