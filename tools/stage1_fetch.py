"""Stage 1 of the escalation ladder: exactly ONE request to sahibinden.

One page view from a browser already proven indistinguishable from the user's
own. If this is not safe, nothing about the project is, and we find out for the
price of a single request.

Success criterion is not "it didn't error" — it is that the live page parses to
the same shape as the manually-saved ground truth in samples/.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sahibinden.fetch.fetcher import fetch_pages  # noqa: E402
from sahibinden.parse.search import parse_search  # noqa: E402

# Same filters as the manually saved sample, so live output is comparable.
URL = (
    "https://www.sahibinden.com/skoda-scala"
    "?unpaintedParts=true&a116445=1263354&sorting=price_asc&unchangingTracks=true"
)
GROUND_TRUTH = Path(__file__).resolve().parents[1] / "samples" / "search_results_page.html"


def main() -> int:
    print("STAGE 1 — one request to sahibinden.com")
    print(f"  {URL}\n")

    results = fetch_pages([URL], expect="search", daily_limit=200)
    if not results:
        print("No result — breaker refused the request.")
        return 1

    result = results[0]
    if not result.ok:
        print(f"\nVERDICT: {result.assessment.verdict.value.upper()}")
        print(f"  vendor : {result.assessment.vendor or 'n/a'}")
        print(f"  reason : {result.assessment.reason}")
        print("\nStopping. Do not retry — investigate before the next attempt.")
        return 2

    live = parse_search(result.html)
    truth = parse_search(GROUND_TRUTH.read_text(encoding="utf-8", errors="replace"))

    print("\n--- live vs manually-saved ground truth ---")
    rows = [
        ("listings parsed", len(live["listings"]), len(truth["listings"])),
        ("total_results", live["total_results"], truth["total_results"]),
        ("has_next", live["has_next"], truth["has_next"]),
        ("permalinks ok", sum(1 for x in live["listings"] if (x["url"] or "").startswith("http")),
         sum(1 for x in truth["listings"] if (x["url"] or "").startswith("http"))),
        ("prices parsed", sum(1 for x in live["listings"] if x["price_try"]),
         sum(1 for x in truth["listings"] if x["price_try"])),
        ("dates parsed", sum(1 for x in live["listings"] if x["listed_date"]),
         sum(1 for x in truth["listings"] if x["listed_date"])),
    ]
    print(f"  {'field':<18}{'live':>10}{'saved':>10}")
    for label, a, b in rows:
        print(f"  {label:<18}{str(a):>10}{str(b):>10}")

    healthy = (
        len(live["listings"]) >= 15
        and live["total_results"]
        and all(x["url"] and x["price_try"] for x in live["listings"])
    )

    print(f"\n  raw saved: {result.raw_path}")
    print("\n--- first 5 live listings ---")
    for x in live["listings"][:5]:
        a = x["attributes"]
        print(f"  {x['listing_id']}  {a.get('year','?'):>4}  {a.get('km','?'):>9}  "
              f"{(x['price_try'] or 0):>10,} TL  {x['city'] or ''} {x['district'] or ''}")

    print("\n" + ("STAGE 1 PASSED — proceed to stage 2." if healthy
                  else "STAGE 1 INCONCLUSIVE — page loaded but parsed thin. Inspect the raw file."))
    return 0 if healthy else 3


if __name__ == "__main__":
    raise SystemExit(main())
