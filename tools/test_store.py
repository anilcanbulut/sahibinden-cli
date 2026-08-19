"""Offline checks for the store's time-series logic. No network."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sahibinden.store import Store  # noqa: E402

URL = "https://www.sahibinden.com/skoda-scala"


def listing(lid, price, **kw):
    base = {"listing_id": lid, "price_try": price, "url": f"/ilan/{lid}",
            "title": f"car {lid}", "trim": "1.6 TDI", "city": "İstanbul",
            "district": "Kadıköy", "store_name": None, "store_url": None,
            "listed_date": "2026-08-01", "attributes": {"year": "2020", "km": "100.000"}}
    base.update(kw)
    return base


def sweep(listings, *, fetched, available, base_url=URL):
    return {"base_url": base_url, "layout": "vehicle", "total_results": available * 20,
            "pages_fetched": fetched, "pages_available": available, "listings": listings}


def main() -> int:
    db = Path(tempfile.mkdtemp()) / "t.db"
    failures = []

    def check(label, got, want):
        ok = got == want
        if not ok:
            failures.append(label)
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<52} {got!r}"
              + ("" if ok else f"  (want {want!r})"))

    with Store(db) as s:
        print("\n1. first complete sweep")
        r = s.record_sweep(sweep([listing("A", 900_000), listing("B", 800_000)],
                                 fetched=2, available=2))
        check("all listings are new", r["new"], 2)
        check("marked complete", r["complete"], True)
        check("nothing delisted on first sweep", r["delisted"], [])

        print("\n2. re-sweep, no changes")
        r = s.record_sweep(sweep([listing("A", 900_000), listing("B", 800_000)],
                                 fetched=2, available=2))
        check("no new listings", r["new"], 0)
        check("no price rows added for unchanged prices",
              s.stats()["price_points"], 2)

        print("\n3. price drop on A")
        r = s.record_sweep(sweep([listing("A", 850_000), listing("B", 800_000)],
                                 fetched=2, available=2))
        check("price change detected", r["price_changes"], [("A", 900_000, 850_000)])
        check("history now has 3 points", s.stats()["price_points"], 3)
        check("A history", [row["price_try"] for row in s.price_history("A")],
              [900_000, 850_000])

        print("\n4. PARTIAL sweep missing B — must NOT mark it delisted")
        r = s.record_sweep(sweep([listing("A", 850_000)], fetched=1, available=2))
        check("partial sweep not marked complete", r["complete"], False)
        check("nothing delisted from a partial sweep", r["delisted"], [])
        check("B still active", s.stats()["active"], 2)

        print("\n5. COMPLETE sweep missing B — now it is delisted")
        r = s.record_sweep(sweep([listing("A", 850_000)], fetched=2, available=2))
        check("B delisted", r["delisted"], ["B"])
        check("active count drops", s.stats()["active"], 1)
        check("delisted count", s.stats()["delisted"], 1)

        print("\n6. B relisted")
        r = s.record_sweep(sweep([listing("A", 850_000), listing("B", 780_000)],
                                 fetched=2, available=2))
        check("relist clears delisted flag", s.stats()["delisted"], 0)
        check("relist recorded as price change", r["price_changes"],
              [("B", 800_000, 780_000)])

        print("\n7. detail queue")
        queue = s.needs_detail(limit=10)
        check("both active listings need details", len(queue), 2)
        check("cheapest first", queue[0]["listing_id"], "B")

        s.record_detail({"listing_id": "B", "heavy_damage_record": "evet",
                         "damage": {"parts": {"hood": "painted"},
                                    "counts": {"original": 12, "painted": 1}},
                         "description": "test"})
        check("detail stored", s.stats()["with_details"], 1)
        check("queue shrinks", len(s.needs_detail(limit=10)), 1)

        # A car the seller priced in euros has no lira price, and SQLite sorts
        # NULL first — so without a guard it would head a queue that is meant
        # to be cheapest-first and spend the budget on an unknown.
        # Partial on purpose: a complete sweep listing only C would delist the
        # others and take the later checks with it.
        s.record_sweep(sweep([listing("C", None, price_currency="EUR",
                                      price_amount=25_000)],
                             fetched=1, available=2))
        queue = s.needs_detail(limit=10)
        check("euro-priced listing is queued", {r["listing_id"] for r in queue},
              {"A", "C"})
        check("but not ahead of a priced one", queue[0]["listing_id"], "A")

        print("\n8. different search must not cross-delist")
        s.record_sweep(sweep([listing("Z", 500_000)], fetched=1, available=1,
                             base_url="https://www.sahibinden.com/vw-golf"))
        check("A and B untouched by other search", s.stats()["delisted"], 0)

    print("\n" + "=" * 62)
    print("FAILURES:" if failures else "all checks passed", *failures, sep="\n  " if failures else "")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
