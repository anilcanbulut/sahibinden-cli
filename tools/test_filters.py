"""Offline checks for the filter surface. No network.

Everything here runs against the committed pages in ``samples/`` and a
throwaway taxonomy file, so it never reads or writes the working
``data/taxonomy.json`` — and so a fresh clone can prove the filters work
before it has ever fetched anything.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sahibinden.parse.search import parse_search  # noqa: E402
from sahibinden.parse.util import parse_price  # noqa: E402
from sahibinden.taxonomy import Taxonomy, apply_post_filters  # noqa: E402

SAMPLES = ROOT / "samples"


def build_taxonomy() -> Taxonomy:
    """Harvest a fresh vocabulary out of the sample pages."""
    tax = Taxonomy(Path(tempfile.mkdtemp()) / "taxonomy.json")
    for path in sorted(SAMPLES.glob("*.html")):
        tax.merge(Taxonomy.extract(path.read_text(encoding="utf-8", errors="replace")))
    return tax


def query(url: str) -> list[tuple[str, str]]:
    """Query pairs as a list, because multi-select repeats its parameter."""
    return parse_qsl(urlparse(url).query)


def main() -> int:
    failures = []

    def check(label, got, want):
        ok = got == want
        if not ok:
            failures.append(label)
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<54} {got!r}"
              + ("" if ok else f"  (want {want!r})"))

    tax = build_taxonomy()

    print("\n1. every sidebar section is harvested")
    for group in ("Yakıt / Motor Tipi", "Vites", "Araç Durumu", "Kasa Tipi",
                  "Motor Gücü", "Motor Hacmi", "Çekiş", "Renk", "Garanti",
                  "Ağır Hasar Kayıtlı", "Plaka / Uyruk", "Kimden", "Takaslı",
                  "İlan Tarihi"):
        check(f"{group} harvested", group in tax.enums, True)
    for label in ("Fiyat", "Yıl", "KM"):
        check(f"{label} range harvested", label in tax.filters, True)
    check("Boya, Değişen Parça switches",
          sorted((tax.flags.get("Boya, Değişen Parça") or {}).values()),
          ["unchangingTracks", "unpaintedParts"])
    check("Fotoğraf, Video switches",
          sorted((tax.flags.get("Fotoğraf, Video") or {}).values()),
          ["hasClip", "hasVideo"])
    check("currency units", sorted(tax.units.get("price_currency") or {}),
          ["EUR", "GBP", "TL", "USD"])

    print("\n2. checkbox groups repeat, radio groups do not")
    # The site draws multi-select as checkboxes and single-select as radios;
    # taking its word for it is what keeps --seller from sending two values.
    check("fuel is multi-select", tax.enums["Yakıt / Motor Tipi"]["multi"], True)
    check("colour is multi-select", tax.enums["Renk"]["multi"], True)
    check("seller is single choice", tax.enums["Kimden"]["multi"], False)
    check("listing age is single choice", tax.enums["İlan Tarihi"]["multi"], False)

    print("\n3. one search using every filter in the sidebar")
    built = tax.build_url(
        "vw", "Passat", city="Ankara", district="Çankaya",
        min_year=2019, max_year=2024, min_km=10_000, max_km=150_000,
        min_price=500_000, max_price=900_000,
        enums={"fuel": ["diesel", "hybrid"], "transmission": ["automatic"],
               "condition": ["used"], "body": ["Sedan"], "power": ["101 - 125 HP"],
               "engine": ["1601 - 1800 cm3"], "drive": ["fwd"],
               "color": ["white", "black"], "warranty": ["no"],
               "heavy_damage": ["no"], "plate": ["Türkiye (TR) Plakalı"],
               "seller": ["dealer"], "tradeable": ["no"],
               "listed_within": ["Son 7 gün içinde"]},
        flags={"has_video": True, "has_clip": True},
        undamaged_only=True, keyword="hatasız", keyword_in_description=True,
    )
    check("no error", built.get("error"), None)
    check("brand and model in the path",
          urlparse(built["url"]).path, "/volkswagen-passat/ankara-cankaya")
    check("district is flagged as a guess", built["district_is_guess"], True)
    check("nothing fell through to post-filters", built["post_filters"], {})

    pairs = query(built["url"])
    got = dict(pairs)
    check("min year", got.get("a5_min"), "2019")
    check("max year", got.get("a5_max"), "2024")
    check("min km", got.get("a4_min"), "10000")
    check("max km", got.get("a4_max"), "150000")
    check("min price", got.get("price_min"), "500000")
    check("max price", got.get("price_max"), "900000")
    check("two fuels, one parameter repeated",
          [v for k, v in pairs if k == "a17"], ["22", "1312756"])
    check("two colours", len([v for k, v in pairs if k == "a3"]), 2)
    # Every remaining group is checked against the id the page itself
    # advertised for the label that was asked for. Repeating the ids here
    # would only assert that the harvest copied them twice; what matters is
    # that the value the user named is the value the URL carries.
    for group, label in [("Vites", "Otomatik"), ("Araç Durumu", "İkinci El"),
                         ("Kasa Tipi", "Sedan"), ("Motor Gücü", "101 - 125 HP"),
                         ("Motor Hacmi", "1601 - 1800 cm3"),
                         ("Çekiş", "Önden Çekiş"), ("Renk", "Beyaz"),
                         ("Garanti", "Hayır"), ("Ağır Hasar Kayıtlı", "Hayır"),
                         ("Plaka / Uyruk", "Türkiye (TR) Plakalı"),
                         ("Kimden", "Galeriden"), ("Takaslı", "Hayır"),
                         ("İlan Tarihi", "Son 7 gün içinde")]:
        spec = tax.enums[group]
        applied = [v for k, v in pairs if k == spec["param"]]
        check(f"{group} = {label}", spec["options"][label] in applied, True)

    # Two spot-checks against ids read straight out of the sample page, so a
    # harvest that silently mapped labels onto the wrong ids cannot pass.
    check("Dizel is a17=22", "22" in [v for k, v in pairs if k == "a17"], True)
    check("Ağır hasar 'Hayır' is a116445=1263354", got.get("a116445"), "1263354")
    check("unpainted", got.get("unpaintedParts"), "true")
    check("unchanged", got.get("unchangingTracks"), "true")
    check("has video", got.get("hasVideo"), "true")
    check("has clip", got.get("hasClip"), "true")
    check("keyword", got.get("query_text"), "hatasız")
    check("keyword covers descriptions", got.get("query_desc"), "true")

    print("\n4. currency picks the unit the price bounds are read in")
    usd = tax.build_url("BMW", currency="usd", max_price=30_000)
    check("price_currency applied", dict(query(usd["url"])).get("price_currency"), "2")
    check("reported back", usd["currency"], "USD")
    check("unknown currency is refused",
          tax.build_url("BMW", currency="yen").get("options"),
          ["EUR", "GBP", "TL", "USD"])

    print("\n5. refusals — a dropped filter would answer about the wrong market")
    two_sellers = tax.build_url("Fiat", enums={"seller": ["owner", "dealer"]})
    check("two values for a radio group refused",
          "takes one value" in (two_sellers.get("error") or ""), True)
    check("district without city refused",
          "needs --city" in (tax.build_url("Fiat", district="Keçiören").get("error") or ""),
          True)
    check("unknown enum value refused, with the valid ones",
          tax.build_url("Fiat", enums={"fuel": ["plutonium"]}).get("options"),
          sorted(tax.enums["Yakıt / Motor Tipi"]["options"]))
    check("unknown flag refused",
          sorted(tax.build_url("Fiat", flags={"has_sunroof": True}).get("options") or []),
          ["has_clip", "has_video", "unchanged", "unpainted"])

    print("\n6. Turkish or English, however it is spelled")
    for spelling in ("Dizel", "diesel", "dizel", "motorin", "DİZEL"):
        url = tax.build_url("vw", enums={"fuel": [spelling]})["url"]
        check(f"--fuel {spelling}", dict(query(url)).get("a17"), "22")
    # 'diz' prefixes both Dizel and Dizel (Hafif Hibrit). Picking either would
    # answer about a market the caller did not ask for, so it is an error that
    # names the candidates.
    ambiguous = tax.build_url("vw", enums={"fuel": ["diz"]})
    check("an ambiguous prefix lists its candidates",
          ambiguous.get("options"), ["Dizel", "Dizel (Hafif Hibrit)"])

    print("\n7. filters with no id for this category fall back to the rows")
    bare = Taxonomy(Path(tempfile.mkdtemp()) / "empty.json")
    bare.brands = {"Volkswagen": "1"}
    bare._brand_slugs = {"volkswagen": "Volkswagen"}
    unharvested = bare.build_url("Volkswagen", min_year=2019, min_km=1000,
                                 max_km=150_000)
    check("year and km become post-filters",
          unharvested["post_filters"],
          {"min_year": 2019, "min_km": 1000, "max_km": 150_000})
    rows = [{"attributes": {"year": "2020", "km": "120.000"}},
            {"attributes": {"year": "2016", "km": "120.000"}},
            {"attributes": {"year": "2020", "km": "900"}},
            {"attributes": {"year": "2020", "km": "200.000"}}]
    check("post-filters applied to parsed rows",
          len(apply_post_filters(rows, unharvested["post_filters"])), 1)

    print("\n8. prices carry their own currency")
    check("lira", parse_price("850.000 TL"), (850000, "TL"))
    check("dollars", parse_price("35.000 USD"), (35000, "USD"))
    check("euros", parse_price("29.500 EUR"), (29500, "EUR"))
    check("no price", parse_price(""), (None, None))
    parsed = parse_search((SAMPLES / "search_results_page.html")
                          .read_text(encoding="utf-8", errors="replace"))
    row = parsed["listings"][0]
    check("sample rows parse as lira", row["price_currency"], "TL")
    check("lira rows still fill price_try", row["price_try"], row["price_amount"])

    print("\n" + "=" * 62)
    print("FAILURES:" if failures else "all checks passed",
          *failures, sep="\n  " if failures else "")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
