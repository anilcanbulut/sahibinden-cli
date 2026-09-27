"""Offline checks for the filter surface. No network.

Everything here runs against the committed ``taxonomy_seed.json`` and a
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
from sahibinden.taxonomy import (SEED_PATH, Taxonomy,  # noqa: E402
                                 apply_post_filters)

SAMPLES = ROOT / "samples"

CAR, SUV = "otomobil", "arazi-suv-pickup"

#: Model lists as a brand page's sidebar would give them. The seed carries
#: brands, not every brand's models — those are learned per brand — so the
#: tests supply their own rather than depending on what was learned so far.
MODELS = {"categories": {
    CAR: {"models": {"Volkswagen": {"complete": True, "models": {
        "Passat": "volkswagen-passat",
        "Passat Variant": "volkswagen-passat-variant",
        "Golf": "volkswagen-golf"}}}},
    SUV: {"models": {"Volkswagen": {"complete": True, "models": {
        "Tiguan": "arazi-suv-pickup-volkswagen-tiguan",
        "T-Roc": "arazi-suv-pickup-volkswagen-t-roc"}}}},
}}

#: A sidebar category tree as a brand page draws it: the breadcrumb path as
#: `li > div > a`, the page's children as `li > a` with a count beside them.
TREE_PAGE = """<html><head>
<link rel="canonical" href="https://www.sahibinden.com/arazi-suv-pickup-dacia"></head>
<body><div id="search_cats"><ul>
<li class="cl0"><div><a href="/vasita" title="Vasıta"><h2>Vasıta</h2></a></div></li>
<li class="cl1"><div><a href="/arazi-suv-pickup" title="Arazi, SUV &amp; Pickup">
<h2>Arazi, SUV &amp; Pickup</h2></a></div></li>
<li class="cl2"><div><a href="/arazi-suv-pickup-dacia" title="Dacia"><h2>Dacia</h2></a></div></li>
<li class="cl3"><a href="/arazi-suv-pickup-dacia-duster" title="Duster"><h2>Duster</h2></a><span>(812)</span></li>
<li class="cl3"><a href="/arazi-suv-pickup-dacia-bigster?a277_min=2020" title="Bigster"><h2>Bigster</h2></a><span>(9)</span></li>
</ul></div>
<div class="multiple-model-selection"><ul><li class="cl2"><a href="#">Dacia</a></li></ul></div>
</body></html>"""


def seed_only() -> Taxonomy:
    """The committed vocabulary with its learned model lists removed, so the
    checks below mean the same thing however many brands the seed has learned."""
    tax = Taxonomy(Path(tempfile.mkdtemp()) / "taxonomy.json", seed=SEED_PATH)
    for cat in tax.categories.values():
        cat["models"] = {}
    return tax


def build_taxonomy() -> Taxonomy:
    """The committed vocabulary, plus the test's model lists."""
    tax = seed_only()
    tax.merge(MODELS)
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
    car = tax.categories[CAR]

    print("\n1. every sidebar section is in the seed, per category")
    for group in ("Yakıt / Motor Tipi", "Vites", "Araç Durumu", "Kasa Tipi",
                  "Motor Gücü", "Motor Hacmi", "Çekiş", "Renk", "Servis Garantisi",
                  "Ağır Hasar Kayıtlı", "Plaka / Uyruk", "Kimden", "Takaslı",
                  "İlan Tarihi"):
        check(f"{group} harvested", group in car["enums"], True)
    for label in ("Fiyat", "Yıl", "KM"):
        check(f"{label} range harvested", label in car["filters"], True)
    check("Boya, Değişen Parça switches",
          sorted((car["flags"].get("Boya, Değişen Parça") or {}).values()),
          ["unchangingTracks", "unpaintedParts"])
    check("Fotoğraf, Video switches",
          sorted((car["flags"].get("Fotoğraf, Video") or {}).values()),
          ["hasClip", "hasVideo"])
    check("currency units", sorted(car["units"].get("price_currency") or {}),
          ["EUR", "GBP", "TL", "USD"])
    check("81 provinces, no placeholder", (len(tax.cities), "Seçiniz" in tax.cities),
          (81, False))
    # The old source, a detail page's brand dropdown, had 45 brands and none
    # of Tofaş, Chevrolet, Mazda or Lada.
    for brand in ("Tofaş", "Chevrolet", "Mazda", "Lada", "Volkswagen"):
        check(f"{brand} is a car brand", CAR in tax.brands.get(brand, []), True)
    for brand in ("Jeep", "Land Rover", "Volkswagen"):
        check(f"{brand} is an SUV brand", SUV in tax.brands.get(brand, []), True)

    print("\n2. checkbox groups repeat, radio groups do not")
    # The site draws multi-select as checkboxes and single-select as radios;
    # taking its word for it is what keeps --seller from sending two values.
    check("fuel is multi-select", car["enums"]["Yakıt / Motor Tipi"]["multi"], True)
    check("colour is multi-select", car["enums"]["Renk"]["multi"], True)
    check("seller is single choice", car["enums"]["Kimden"]["multi"], False)
    check("listing age is single choice", car["enums"]["İlan Tarihi"]["multi"], False)

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
    check("filed under Otomobil", built["category"], CAR)
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
                         ("Servis Garantisi", "Hayır"), ("Ağır Hasar Kayıtlı", "Hayır"),
                         ("Plaka / Uyruk", "Türkiye (TR) Plakalı"),
                         ("Kimden", "Galeriden"), ("Takaslı", "Hayır"),
                         ("İlan Tarihi", "Son 7 gün içinde")]:
        spec = car["enums"][group]
        applied = [v for k, v in pairs if k == spec["param"]]
        check(f"{group} = {label}", spec["options"][label] in applied, True)

    # Two spot-checks against ids read straight out of a real page, so a
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
          sorted(car["enums"]["Yakıt / Motor Tipi"]["options"]))
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
    bare = Taxonomy(Path(tempfile.mkdtemp()) / "empty.json", seed=None)
    bare.merge({"categories": {CAR: {"label": "Otomobil",
                                     "brands": {"Volkswagen": "volkswagen"}}}})
    unharvested = bare.build_url("Volkswagen", min_year=2019, min_km=1000,
                                 max_km=150_000, max_price=900_000)
    # Price used to be dropped here without a word.
    check("year, km and price become post-filters",
          unharvested["post_filters"],
          {"min_year": 2019, "min_km": 1000, "max_km": 150_000, "max_price": 900_000})
    rows = [{"attributes": {"year": "2020", "km": "120.000"}, "price_try": 800_000},
            {"attributes": {"year": "2016", "km": "120.000"}, "price_try": 800_000},
            {"attributes": {"year": "2020", "km": "900"}, "price_try": 800_000},
            {"attributes": {"year": "2020", "km": "200.000"}, "price_try": 800_000},
            {"attributes": {"year": "2020", "km": "120.000"}, "price_try": 950_000},
            {"attributes": {"year": "2020", "km": "120.000"}, "price_try": None}]
    check("post-filters applied to parsed rows",
          len(apply_post_filters(rows, unharvested["post_filters"])), 1)

    print("\n8. prices carry their own currency")
    check("lira", parse_price("850.000 TL"), (850000, "TL"))
    check("dollars", parse_price("35.000 USD"), (35000, "USD"))
    check("euros", parse_price("29.500 EUR"), (29500, "EUR"))
    check("no price", parse_price(""), (None, None))
    sample = SAMPLES / "search_results_page.html"
    if not sample.exists():
        print("  SKIP  no samples/search_results_page.html (never committed)")
    else:
        parsed = parse_search(sample.read_text(encoding="utf-8", errors="replace"))
        row = parsed["listings"][0]
        check("sample rows parse as lira", row["price_currency"], "TL")
        check("lira rows still fill price_try", row["price_try"], row["price_amount"])

    print("\n9. SUVs are their own category, with their own ids")
    tiguan = tax.build_url("vw", "tiguan", min_year=2018, max_km=150_000,
                           enums={"fuel": ["diesel"], "transmission": ["automatic"]})
    check("no error", tiguan.get("error"), None)
    check("SUV path, from the model list",
          urlparse(tiguan["url"]).path, "/arazi-suv-pickup-volkswagen-tiguan")
    check("filed under SUV", tiguan["category"], SUV)
    suv_pairs = dict(query(tiguan["url"]))
    suv = tax.categories[SUV]
    # The whole reason for keeping categories apart: a5 on an SUV search is an
    # attribute id that means something else, answered with HTTP 200.
    check("year is the SUV id, not a5", suv_pairs.get(f"{suv['filters']['Yıl']['param']}_min"),
          "2018")
    check("  and a5 is absent", "a5_min" in suv_pairs, False)
    check("SUV year id differs from the car one",
          suv["filters"]["Yıl"]["param"] != car["filters"]["Yıl"]["param"], True)
    check("fuel uses the SUV parameter",
          suv_pairs.get(suv["enums"]["Yakıt / Motor Tipi"]["param"]),
          suv["enums"]["Yakıt / Motor Tipi"]["options"]["Dizel"])
    check("with a city, SUV path keeps its prefix",
          urlparse(tax.build_url("vw", "tiguan", city="istanbul")["url"]).path,
          "/arazi-suv-pickup-volkswagen-tiguan/istanbul")
    check("a brand that is only SUVs goes there on its own",
          urlparse(tax.build_url("jeep")["url"]).path, "/arazi-suv-pickup-jeep")
    vw = tax.build_url("volkswagen")
    check("a brand on its own defaults to cars", urlparse(vw["url"]).path, "/volkswagen")
    check("  and says the SUVs are a separate search", vw["other_categories"],
          ["Arazi, SUV & Pickup"])
    check("--category suv picks the SUV page",
          urlparse(tax.build_url("volkswagen", category="suv")["url"]).path,
          "/arazi-suv-pickup-volkswagen")
    walled = tax.build_url("jeep", category="car")
    check("a brand not in the asked category is refused",
          "no listings under Otomobil" in (walled.get("error") or ""), True)
    check("  pointing at the right one", "--category suv" in (walled.get("hint") or ""),
          True)
    check("Tofaş builds its own path", urlparse(tax.build_url("tofas")["url"]).path,
          "/tofas")

    print("\n10. models are matched against the brand's own list")
    check("exact name wins over a longer one",
          tax.build_url("vw", "passat")["model"], "Passat")
    check("an ambiguous prefix names the candidates",
          sorted(tax.build_url("vw", "pass").get("did_you_mean") or []),
          ["Passat", "Passat Variant"])
    check("a typo is corrected", tax.build_url("vw", "tiguann").get("model"), "Tiguan")
    unknown = tax.build_url("vw", "beetle")
    check("an unknown model is an error, not a 404",
          unknown.get("error"), "unknown Volkswagen model 'beetle'")
    check("  listing what exists", "T-Roc" in (unknown.get("options") or []), True)
    check("--category car does not reach into SUVs",
          tax.build_url("vw", "tiguan", category="car").get("error"),
          "unknown Volkswagen model 'tiguan'")
    learn = tax.build_url("fiat", "egea")
    check("a brand with no model list asks for its pages",
          sorted(t["url"] for t in learn.get("learn") or []),
          ["https://www.sahibinden.com/arazi-suv-pickup-fiat",
           "https://www.sahibinden.com/fiat"])
    partial = seed_only()
    partial.merge({"categories": {CAR: {"models": {"Volkswagen": {
        "models": {"Passat": "volkswagen-passat"}}}}}})
    check("a partial list confirms an exact name",
          partial.build_url("vw", "passat").get("model"), "Passat")
    # "Passat CC" is 0.8 similar to "Passat": fuzzy against a partial list
    # would search the wrong car instead of learning the real list.
    check("but never fuzzy-matches against one",
          bool(partial.build_url("vw", "passat cc").get("learn")), True)

    print("\n11. the sidebar tree is read, and the widget copy ignored")
    found = Taxonomy.extract(TREE_PAGE)
    check("category from the tree", list(found["categories"]), [SUV])
    tree = found["categories"][SUV]
    check("category label", tree["label"], "Arazi, SUV & Pickup")
    check("brand with its path", tree["brands"], {"Dacia": "arazi-suv-pickup-dacia"})
    check("models with their paths, query stripped", tree["models"]["Dacia"]["models"],
          {"Duster": "arazi-suv-pickup-dacia-duster",
           "Bigster": "arazi-suv-pickup-dacia-bigster"})
    check("an unfiltered brand page's list is complete",
          tree["models"]["Dacia"].get("complete"), True)
    # Once a filter is applied every tree link becomes /arama/flt/?category=…
    # These once filed a Tiguan search's SUV ids and an Egea search's car ids
    # together under a category called "arama".
    filtered = TREE_PAGE.replace('href="/arazi-suv-pickup-dacia-duster"',
                                 'href="/arama/flt/?category=1&a277_min=2020"')
    for bare_path in ("/vasita", "/arazi-suv-pickup", "/arazi-suv-pickup-dacia"):
        filtered = filtered.replace(f'href="{bare_path}"',
                                    'href="/arama/flt/?category=2&a277_min=2020"')
    found = Taxonomy.extract(filtered)
    check("a filtered page is placed by its category's name",
          list(found["categories"]), [SUV])
    check("  and names no brand paths it was not given",
          found["categories"][SUV]["brands"], {})
    check("  nor claims a complete model list",
          found["categories"][SUV]["models"].get("Dacia", {}).get("complete"), None)
    check("a page with no tree files nothing under a category",
          Taxonomy.extract("<html><body><select name='city'>"
                           "<option value='6'>Ankara</option>"
                           "<option value='0'>Seçiniz</option></select>"
                           "<div class='search-filter-section'></div></body></html>"),
          {"cities": {"Ankara": "6"}, "categories": {}})

    print("\n" + "=" * 62)
    print("FAILURES:" if failures else "all checks passed",
          *failures, sep="\n  " if failures else "")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
