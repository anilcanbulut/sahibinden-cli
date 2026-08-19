"""Offline checks for per-panel damage filtering. No network.

Runs against a throwaway database, so it never reads the working one.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sahibinden.panels import (PANELS, build_requirements, describe,  # noqa: E402
                               evaluate, resolve_panels, resolve_status,
                               summarise)
from sahibinden.store import Store  # noqa: E402

URL = "https://www.sahibinden.com/volkswagen-passat"


def diagram(**overrides) -> dict:
    """A clean 13-panel diagram, with the named panels overridden."""
    parts = {panel: "original" for panel in PANELS}
    parts.update(overrides)
    return parts


def listing(lid, price, **kw):
    base = {"listing_id": lid, "price_try": price, "url": f"/ilan/{lid}",
            "title": f"car {lid}", "trim": None, "city": "Ankara",
            "district": "Çankaya", "store_name": None, "store_url": None,
            "listed_date": "2026-08-01",
            "attributes": {"year": "2020", "km": "100.000"}}
    base.update(kw)
    return base


def sweep(listings):
    return {"base_url": URL, "layout": "vehicle", "total_results": 20,
            "pages_fetched": 1, "pages_available": 1, "listings": listings,
            "resolved": {"brand": "Volkswagen", "model": "passat"}}


def detail(lid, parts):
    counts = {s: sum(1 for v in parts.values() if v == s) for s in
              ("original", "local_painted", "painted", "changed")}
    return {"listing_id": lid, "heavy_damage_record": "hayir",
            "damage": {"parts": parts, "counts": counts},
            "description": None}


def main() -> int:
    failures = []

    def check(label, got, want):
        ok = got == want
        if not ok:
            failures.append(label)
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<54} {got!r}"
              + ("" if ok else f"  (want {want!r})"))

    print("\n1. naming a panel")
    check("english", resolve_panels("hood")[0], ["hood"])
    check("turkish", resolve_panels("kaput")[0], ["hood"])
    check("case and spacing", resolve_panels("Front Right Door")[0],
          ["front_right_door"])
    check("group", resolve_panels("doors")[0],
          ["front_right_door", "rear_right_door", "front_left_door", "rear_left_door"])
    check("turkish group", resolve_panels("kapilar")[0], resolve_panels("doors")[0])
    check("structural excludes the bumpers",
          [p for p in resolve_panels("structural")[0] if "bumper" in p], [])
    check("a typo is refused, with candidates",
          resolve_panels("hodd")[1]["options"], ["hood"])

    print("\n2. naming a status")
    check("english", resolve_status("painted")[0], "painted")
    check("turkish", resolve_status("boyalı")[0], "painted")
    check("değişen", resolve_status("değişen")[0], "changed")
    check("lokal", resolve_status("lokal")[0], "local_painted")
    check("nonsense is refused", resolve_status("shiny")[1]["options"],
          ["original", "local_painted", "painted", "changed", "unknown"])

    print("\n3. requirements combine, and conflicts are errors")
    req, error = build_requirements(original=["hood"], not_changed=["bumpers"])
    check("no error", error, None)
    check("hood must be untouched", req["hood"], {"original"})
    check("bumpers may be painted", req["front_bumper"],
          {"original", "local_painted", "painted"})
    # Two rules over the same panel keep the stricter one rather than letting
    # argument order decide what the query means.
    req, error = build_requirements(original=["structural"], not_changed=["hood"])
    check("overlap keeps the stricter rule", req["hood"], {"original"})
    _, error = build_requirements(panel=["hood=changed"], original=["hood"])
    check("an impossible pair is refused",
          "cannot both hold" in (error or {}).get("error", ""), True)
    _, error = build_requirements(panel=["hood"])
    check("PANEL=STATUS is required", "wants PANEL=STATUS" in error["error"], True)

    print("\n4. a car is judged only on what the diagram shows")
    req, _ = build_requirements(original=["hood"])
    check("clean hood passes", evaluate(diagram(), req), "pass")
    check("painted hood fails", evaluate(diagram(hood="painted"), req), "fail")
    check("changed hood fails", evaluate(diagram(hood="changed"), req), "fail")
    check("paint elsewhere is irrelevant",
          evaluate(diagram(rear_bumper="painted"), req), "pass")
    # The three ways of not knowing. None of them may pass, and none of them
    # may be called a failure either: these are the cars to go and look at.
    check("no diagram at all is unchecked", evaluate(None, req), "unchecked")
    check("diagram without that panel is unchecked",
          evaluate({"roof": "original"}, req), "unchecked")
    check("unparsed marking is unchecked",
          evaluate(diagram(hood="unknown"), req), "unchecked")
    check("unless unknown was asked for explicitly",
          evaluate(diagram(hood="unknown"),
                   build_requirements(panel=["hood=unknown"])[0]), "pass")

    print("\n5. whole-car caps")
    two_painted = diagram(hood="painted", trunk="painted")
    check("within the cap", evaluate(two_painted, {}, max_painted=2), "pass")
    check("over the cap", evaluate(two_painted, {}, max_painted=1), "fail")
    check("local paint counts as paint",
          evaluate(diagram(hood="local_painted"), {}, max_painted=0), "fail")
    check("changed cap", evaluate(diagram(roof="changed"), {}, max_changed=0), "fail")
    # A cap is a claim about the whole car, so a partial diagram cannot answer
    # it even when every panel it does show is clean.
    check("partial diagram cannot answer a cap",
          evaluate({"hood": "original"}, {}, max_painted=0), "unchecked")

    print("\n6. reading a query and a car back")
    req, _ = build_requirements(original=["structural"], not_changed=["bumpers"])
    check("described as two rules, not thirteen", len(describe(req)), 2)
    check("group named in the description",
          any("structural (11 panels)" in line for line in describe(req)), True)
    check("summary of a diagram", summarise(diagram(hood="painted", roof="changed")),
          "11orij 1boya 1değişen")
    check("no diagram", summarise(None), "?")

    print("\n7. the market query counts unchecked separately")
    db = Path(tempfile.mkdtemp()) / "t.db"
    with Store(db) as store:
        store.record_sweep(sweep([listing("CLEAN", 900_000),
                                  listing("HOOD_PAINTED", 700_000),
                                  listing("BUMPER_PAINTED", 800_000),
                                  listing("NO_DETAIL", 500_000)]))
        store.record_detail(detail("CLEAN", diagram()))
        store.record_detail(detail("HOOD_PAINTED", diagram(hood="painted")))
        store.record_detail(detail("BUMPER_PAINTED", diagram(rear_bumper="painted")))

        req, _ = build_requirements(original=["hood"])
        result = store.market(panels=req)
        check("only cars with a clean hood match", result["matched"], 2)
        check("the painted hood is a failure", result["panel_filter"]["failed"], 1)
        check("the unfetched car is unchecked",
              result["panel_filter"]["unchecked"], 1)
        check("and is named, so it can be fetched",
              result["panel_filter"]["unchecked_ids"], ["NO_DETAIL"])
        check("cheapest row carries its diagram",
              result["cheapest"][0]["panels"], "12orij 1boya")

        # Unfiltered, the same scope keeps everything: the panel filter is the
        # only thing that removed rows above.
        check("no panel filter, no exclusions", store.market()["matched"], 4)
        check("and no panel report", store.market()["panel_filter"], None)

        req, _ = build_requirements(original=["all"])
        check("all-original is stricter than hood-only",
              store.market(panels=req)["matched"], 1)

    print("\n" + "=" * 62)
    print("FAILURES:" if failures else "all checks passed",
          *failures, sep="\n  " if failures else "")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
