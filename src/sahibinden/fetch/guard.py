"""Classify every response before it reaches a parser, and halt on trouble.

The failure mode this exists to prevent: a fetcher that receives a challenge
page, fails to recognise it, retries, and converts a soft rate-limit into a hard
IP ban. So nothing here retries. A single BLOCKED or CHALLENGED verdict trips the
breaker and the run ends until a human clears it.

Verdicts are deliberately coarse — we only need to answer "keep going or stop".
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..config import config


class Verdict(str, Enum):
    OK = "ok"                  # real page, parseable
    CHALLENGED = "challenged"  # anti-bot interstitial — stop immediately
    BLOCKED = "blocked"        # 403/429/503 — stop immediately
    LOGIN_WALL = "login_wall"  # sign-in demanded; session problem, not detection
    EMPTY = "empty"            # 200 and no challenge, but no listings either
    CHANGED = "changed"        # 200, listings present, but markup shifted
    NOT_FOUND = "not_found"    # the site's own 404 page — a wrong path, not a ban


#: Header names that only appear when an anti-bot vendor is in the path.
VENDOR_HEADERS = {
    "x-kpsdk-ct": "kasada",
    "x-kpsdk-cd": "kasada",
    "x-kpsdk-r": "kasada",
    "cf-mitigated": "cloudflare",
    "x-datadome": "datadome",
    "x-datadome-cid": "datadome",
}

#: Body markers. Matched case-insensitively against the first ~40KB only —
#: challenge pages are tiny, so a match deep in a 400KB page is a false positive.
BODY_MARKERS = [
    (re.compile(r"just a moment", re.I), "cloudflare"),
    (re.compile(r"/cdn-cgi/challenge-platform", re.I), "cloudflare"),
    (re.compile(r"cf-browser-verification|cf_chl_", re.I), "cloudflare"),
    (re.compile(r"kpsdk|kasada", re.I), "kasada"),
    (re.compile(r"captcha-delivery\.com|geo\.captcha", re.I), "datadome"),
    (re.compile(r"_px[A-Za-z0-9]{2,}|perimeterx", re.I), "perimeterx"),
    (re.compile(r"erişim engellendi|eri\w*im engellendi", re.I), "sahibinden"),
    (re.compile(r"robot olmad[ıi]ğ[ıi]n[ıi]z[ıi]", re.I), "sahibinden"),
    (re.compile(r"unusual traffic|olağan dışı trafik", re.I), "generic"),
    # sahibinden's own block page, observed with HTTP 429: "Olağan dışı erişim
    # tespit ettik... sitemize olağan dışı (otomatik) erişim". It shares its
    # <title> ("Hata Sayfası") with the plain 404 page, so it has to be caught
    # here, by its wording, before any status-based reading of that title.
    (re.compile(r"ola[ğg]an ?d[ıi][şs][ıi] (\(otomatik\) )?eri[şs]im", re.I), "sahibinden"),
]

#: sahibinden serves a sign-in page to cookieless sessions. Verified to affect a
#: plain incognito window on a residential IP too, so it is a session-state
#: condition, not bot detection — worth its own verdict so the two never get
#: confused again. The G-code is sahibinden's own trigger reference.
LOGIN_WALL_MARKERS = [
    re.compile(r"sign in to sahibinden\.com", re.I),
    re.compile(r"sahibinden\.com'a giri[şs] yapman[ıi]z gerekmektedir", re.I),
    # e.g. (G1PLF817-0813). Kept loose on the middle segment since the format
    # is sahibinden's and undocumented; the parens, leading G and -NNNN tail
    # make it specific enough not to fire on listing text.
    re.compile(r"\(G[A-Z0-9]{6,10}-\d{4}\)"),
]

#: sahibinden's own 404 page: "Aradığınız sayfaya ulaşılamadı". It is small,
#: so without this the size test below files it as a challenge: one misspelt
#: path then trips a 90-minute cooldown and slows every request for a week.
#: That happened. Matched on this sentence only — the page's title, "Hata
#: Sayfası", is shared with the block page.
NOT_FOUND_MARKERS = [
    # The page wraps the second half in <strong>.
    re.compile(r"arad[ıi]\w*n[ıi]z sayfaya\s*(?:<[^>]+>\s*)*ula[şs][ıi]lamad[ıi]", re.I),
]

BLOCKING_STATUS = {401, 403, 405, 429, 503}
CHALLENGE_SCAN_BYTES = 40_000


@dataclass
class Assessment:
    verdict: Verdict
    vendor: str | None = None
    status: int | None = None
    reason: str = ""
    listing_count: int = 0

    @property
    def should_halt(self) -> bool:
        return self.verdict in (Verdict.CHALLENGED, Verdict.BLOCKED)

    @property
    def is_usable(self) -> bool:
        return self.verdict is Verdict.OK


def assess(
    status: int,
    headers: dict[str, str] | None,
    body: str,
    *,
    expect: str = "search",
) -> Assessment:
    """Judge one response. ``expect`` is 'search' or 'detail'."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}

    for name, vendor in VENDOR_HEADERS.items():
        if name in headers:
            return Assessment(Verdict.CHALLENGED, vendor, status,
                              f"anti-bot header {name!r} present")

    if status in BLOCKING_STATUS:
        return Assessment(Verdict.BLOCKED, None, status, f"HTTP {status}")

    head = body[:CHALLENGE_SCAN_BYTES]
    for pattern, vendor in BODY_MARKERS:
        if pattern.search(head):
            return Assessment(Verdict.CHALLENGED, vendor, status,
                              f"{vendor} marker in body")

    # Only with a 404 status as well as the site's own wording: a challenge
    # page never carries both, so this cannot excuse a real block.
    if status == 404 and any(p.search(head) for p in NOT_FOUND_MARKERS):
        return Assessment(Verdict.NOT_FOUND, "sahibinden", status,
                          "sahibinden has no page at this address")

    # Checked before the size test: the sign-in page is a legitimately-sized
    # page, so a size heuristic would misfile it as a challenge.
    for pattern in LOGIN_WALL_MARKERS:
        m = pattern.search(body)
        if m:
            return Assessment(Verdict.LOGIN_WALL, "sahibinden", status,
                              f"sign-in required ({m.group(0)[:24]})")

    # A challenge page is small. A real listing page is hundreds of KB.
    if len(body) < 20_000:
        return Assessment(Verdict.CHALLENGED, None, status,
                          f"body only {len(body)} bytes — too small to be a real page")

    if expect == "search":
        if "searchResultsTable" not in body:
            # A search that matches nothing is drawn with no results table at
            # all, but it is still a search page: the form is there. Reading it
            # as a markup change sent every too-narrow search and misspelt
            # district to "markup changed", exit 2.
            if 'id="searchResultsSearchForm"' in body:
                return Assessment(Verdict.EMPTY, None, status,
                                  "no listings match this search")
            return Assessment(Verdict.CHANGED, None, status,
                              "no #searchResultsTable — markup changed or not a results page")
        count = body.count("searchResultsItem")
        if count == 0:
            return Assessment(Verdict.EMPTY, None, status, "table present but zero rows")
        return Assessment(Verdict.OK, None, status, "ok", listing_count=count)

    if "classifiedInfoList" not in body:
        return Assessment(Verdict.CHANGED, None, status,
                          "no .classifiedInfoList — markup changed or not a detail page")
    return Assessment(Verdict.OK, None, status, "ok", listing_count=1)


#: Observed sahibinden bans lift on their own, on the order of an hour, and are
#: triggered by request rate rather than by browser fingerprint. So a trip is a
#: cooldown, not a shutdown — halting until a human intervenes would turn a
#: one-hour pause into an indefinite one for a tool meant to run unattended.
#: Cooldowns escalate: tripping twice means the rate is wrong, not unlucky.
COOLDOWN_LADDER = (90 * 60, 4 * 60 * 60, 12 * 60 * 60)

#: After a trip we do not return to the previous pace. Each trip in the trailing
#: window widens the inter-request delay, so the crawler converges downward onto
#: a rate the site tolerates instead of rediscovering the ceiling every day.
BACKOFF_PER_TRIP = 1.75
TRIP_MEMORY_DAYS = 7


@dataclass
class Breaker:
    """Rate governor and cooldown, persisted across processes.

    State lives on disk so a crash-and-restart cannot silently resume hammering
    a site that already pushed back.
    """

    state_path: Path
    # Read at construction, not import, so CLI overrides take effect.
    daily_limit: int = field(default_factory=lambda: config.daily_budget)
    _state: dict = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if self.state_path.exists():
            self._state = json.loads(self.state_path.read_text(encoding="utf-8"))
        else:
            self._state = {}
        self._state.setdefault("tripped_until", 0)
        self._state.setdefault("tripped_reason", None)
        self._state.setdefault("trips", [])
        self._state.setdefault("day", None)
        self._state.setdefault("count", 0)
        self._state.setdefault("last_request_at", 0.0)
        self._state.setdefault("last_verdict", None)

    def _save(self) -> None:
        self.state_path.write_text(json.dumps(self._state, indent=2), encoding="utf-8")

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    def _recent_trips(self) -> list[dict]:
        cutoff = time.time() - TRIP_MEMORY_DAYS * 86_400
        return [t for t in self._state["trips"]
                if t.get("at", 0) >= cutoff and not t.get("forgiven")]

    def check(self) -> None:
        """Raise if we must not send another request right now."""
        remaining_cooldown = self._state["tripped_until"] - time.time()
        if remaining_cooldown > 0:
            raise RuntimeError(
                f"Cooling down for {remaining_cooldown / 60:.0f} more minutes.\n"
                f"  reason: {self._state['tripped_reason']}\n"
                "This clears itself — no manual action needed. To end it now, "
                "run 'sahibinden-cli reset-cooldown'."
            )
        if self._state["tripped_until"]:
            # Cooldown elapsed: clear it, but the trip stays in history so the
            # backoff multiplier keeps the lesson.
            print(f"  cooldown expired ({self._state['tripped_reason']}) — resuming")
            self._state["tripped_until"] = 0
            self._state["tripped_reason"] = None
            self._save()

        if self._state["day"] == self._today() and self._state["count"] >= self.daily_limit:
            raise RuntimeError(
                f"Daily budget spent ({self._state['count']}/{self.daily_limit}). "
                "Resumes tomorrow."
            )

    def record(self, assessment: Assessment) -> None:
        if self._state["day"] != self._today():
            self._state["day"] = self._today()
            self._state["count"] = 0
        self._state["count"] += 1
        self._state["last_request_at"] = time.time()
        # Kept so "is the session still good?" is answerable offline. Learning
        # that the hard way costs a request, and a walled session walls every
        # request, so the answer has to be on disk.
        self._state["last_verdict"] = {
            "verdict": assessment.verdict.value,
            "reason": assessment.reason,
            "at": time.time(),
            "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        if assessment.should_halt:
            step = min(len(self._recent_trips()), len(COOLDOWN_LADDER) - 1)
            cooldown = COOLDOWN_LADDER[step]
            self._state["trips"].append({
                "at": time.time(),
                "when": time.strftime("%Y-%m-%d %H:%M:%S"),
                "verdict": assessment.verdict.value,
                "vendor": assessment.vendor,
                "reason": assessment.reason,
                "requests_before_trip": self._state["count"],
                "cooldown_seconds": cooldown,
            })
            self._state["tripped_until"] = time.time() + cooldown
            self._state["tripped_reason"] = (
                f"{assessment.verdict.value} after {self._state['count']} requests today "
                f"({assessment.reason})"
            )
        self._save()

    def clear_cooldown(self, *, forget_trips: bool = False) -> dict:
        """End a cooldown now, on the operator's say-so.

        By default the trip stays counted, so delays stay widened and the next
        trip climbs the ladder: ending the wait early is not evidence the rate
        was fine. ``forget_trips`` drops the past week's trips from that count
        too. They are marked rather than deleted, so the history of what
        triggered each block — the evidence the defaults are tuned from — stays
        on disk.
        """
        recent = self._recent_trips()
        cleared = {
            "was_cooling_down": self.cooling_down,
            "minutes_left": round(max(0.0, self._state["tripped_until"] - time.time()) / 60),
            "reason": self._state["tripped_reason"],
            "trips_forgiven": len(recent) if forget_trips else 0,
        }
        self._state["tripped_until"] = 0
        self._state["tripped_reason"] = None
        if forget_trips:
            for trip in recent:
                trip["forgiven"] = True
        self._save()
        return cleared

    def sleep_needed(self, target_delay: float) -> float:
        """Seconds still to wait before the next request may go out.

        Measured from the last request recorded on disk rather than from a
        position in the current batch, so pacing survives across separate
        fetch_pages() calls and across separate processes. Batch-relative
        pacing let the first request of each batch go out immediately, which
        produced exactly the back-to-back pattern that gets rate-banned.
        """
        elapsed = time.time() - self._state["last_request_at"]
        return max(0.0, target_delay - elapsed)

    @property
    def delay_multiplier(self) -> float:
        """Widen delays permanently after each trip, so we settle below the ceiling."""
        return BACKOFF_PER_TRIP ** len(self._recent_trips())

    @property
    def remaining(self) -> int:
        if self._state["day"] != self._today():
            return self.daily_limit
        return max(0, self.daily_limit - self._state["count"])

    @property
    def last_verdict(self) -> dict | None:
        """How the most recent response was judged, or None before any."""
        return self._state.get("last_verdict")

    @property
    def cooling_down(self) -> bool:
        return self._state["tripped_until"] > time.time()

    def status(self) -> str:
        trips = self._recent_trips()
        parts = [f"{self.remaining}/{self.daily_limit} requests left today"]
        if trips:
            parts.append(f"{len(trips)} trip(s) in {TRIP_MEMORY_DAYS}d")
            parts.append(f"delay x{self.delay_multiplier:.2f}")
        if self.cooling_down:
            mins = (self._state["tripped_until"] - time.time()) / 60
            parts.append(f"COOLING DOWN {mins:.0f}m")
        return " | ".join(parts)
