"""Run a paginated sweep and summarise what came back.

    python tools/sweep.py <search-url> [--pages N]
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sahibinden.crawl import sweep  # noqa: E402
from sahibinden.store import Store  # noqa: E402

DEFAULT_URL = ("https://www.sahibinden.com/skoda-scala"
               "?unpaintedParts=true&a116445=1263354&sorting=price_asc"
               "&unchangingTracks=true")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", default=DEFAULT_URL)
    ap.add_argument("--pages", type=int, default=None,
                    help="cap pages fetched (default: budget-limited)")
    ap.add_argument("--no-store", action="store_true",
                    help="print only, do not persist to the database")
    args = ap.parse_args()

    result = sweep(args.url, max_pages=args.pages)

    if result.get("error"):
        print(f"\nSWEEP FAILED: {result['error']}  {result.get('reason','')}")
        return 2

    listings = result["listings"]

    recorded = None
    if not args.no_store:
        with Store() as store:
            recorded = store.record_sweep(result)
            totals = store.stats()
    print(f"\n{'='*72}")
    print(f"  total on site   : {result['total_results']}")
    print(f"  pages fetched   : {result['pages_fetched']} of {result['pages_available']}")
    print(f"  unique listings : {len(listings)}  (deduped {result['duplicates_dropped']})")
    print(f"  breaker         : {result['breaker']}")

    prices = [x["price_try"] for x in listings if x["price_try"]]
    if prices:
        print(f"\n  price range     : {min(prices):,} - {max(prices):,} TL")
        print(f"  median          : {int(statistics.median(prices)):,} TL")

    complete = sum(1 for x in listings if x["price_try"] and x["url"] and x["listed_date"])
    print(f"  fully parsed    : {complete}/{len(listings)}")

    if recorded:
        print(f"\n  --- stored ---")
        print(f"  new listings    : {recorded['new']}")
        print(f"  price changes   : {len(recorded['price_changes'])}")
        for lid, old, now in recorded["price_changes"][:8]:
            arrow = "down" if now < old else "up"
            print(f"      {lid}  {old:,} -> {now:,} TL  ({arrow} "
                  f"{abs(now-old)/old*100:.1f}%)")
        print(f"  delisted        : {len(recorded['delisted'])}"
              + ("" if recorded["complete"] else "  (partial sweep — not checked)"))
        print(f"  db totals       : {totals['listings']} listings, "
              f"{totals['active']} active, {totals['price_points']} price points, "
              f"{totals['sweeps']} sweeps")

    print(f"\n  {'id':<12}{'year':<6}{'km':<10}{'price':>12}  {'date':<12}location")
    print("  " + "-" * 70)
    for x in listings[:12]:
        a = x["attributes"]
        loc = f"{x['city'] or ''} {x['district'] or ''}".strip()
        print(f"  {x['listing_id']:<12}{a.get('year',''):<6}{a.get('km',''):<10}"
              f"{x['price_try'] or 0:>12,}  {x['listed_date'] or '':<12}{loc}")
    if len(listings) > 12:
        print(f"  ... and {len(listings)-12} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
