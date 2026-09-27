"""Offline checks for `ask` and the Markdown table. No network.

The questions are answered by a script, keyed by the question they answer, so
a check reads as the conversation it stands for. The sweep is replaced by one
built from rows written here, and nothing touches the working database,
taxonomy or results folder.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sahibinden import cli, crawl, saved  # noqa: E402
from sahibinden.ask import (Invalid, ask_search, parse_amount,  # noqa: E402
                            parse_range)
from sahibinden.config import Config, config, set_config  # noqa: E402
from sahibinden.panels import (PANELS, build_requirements,  # noqa: E402
                               condition_rank, explain)
from sahibinden.report import markdown  # noqa: E402
from sahibinden.store import Store  # noqa: E402
from sahibinden.taxonomy import SEED_PATH, Taxonomy  # noqa: E402

CAR, SUV = "otomobil", "arazi-suv-pickup"

#: Model lists as a brand page's sidebar would give them, as in test_filters.
MODELS = {"categories": {
    CAR: {"models": {"Volkswagen": {"complete": True, "models": {
        "Passat": "volkswagen-passat", "Golf": "volkswagen-golf"}}}},
    SUV: {"models": {"Volkswagen": {"complete": True, "models": {
        "Tiguan": "arazi-suv-pickup-volkswagen-tiguan"}}}},
}}


def taxonomy() -> Taxonomy:
    """The committed vocabulary with only the models above, in a throwaway file."""
    tax = Taxonomy(Path(tempfile.mkdtemp()) / "taxonomy.json", seed=SEED_PATH)
    for cat in tax.categories.values():
        cat["models"] = {}
    tax.merge(MODELS)
    return tax


def no_network(targets):
    raise AssertionError(f"a model list was fetched: {targets}")


def sitting(tax, answers: dict[str, list[str]], learn=no_network):
    """Answer each question by its title; anything not scripted gets Enter.

    Returns the search arguments, everything written, and the scripted answers
    no question asked for, which should be none.
    """
    queues = {title: list(given) for title, given in answers.items()}
    written: list[str] = []
    prompts: list[str] = []
    reads = 0

    def read(prompt):
        nonlocal reads
        reads += 1
        if reads > 200:
            raise AssertionError("the same question keeps coming back")
        title = prompt.strip()
        if title != ">":
            prompts.append(title)
        if title == ">":
            title = next(line.strip() for line in reversed(written)
                         if line.startswith("  ") and not line.startswith("   "))
        key = next((k for k in queues if title.startswith(k)), None)
        return queues[key].pop(0) if key and queues[key] else ""

    argv = ask_search(tax, budget=lambda: 30, default_pages=5, learn=learn,
                      read=read, write=written.append)
    unused = {k: v for k, v in queues.items() if v}
    return argv, "\n".join(written + prompts), unused


def listing(n: int, **overrides) -> dict:
    row = {"listing_id": str(1339324870 + n), "title": f"Temiz araç {n}",
           "trim": "1.6 TDI Comfortline", "price_try": 1_000_000 + n * 50_000,
           "price_raw": None, "price_currency": "TL", "city": "İstanbul",
           "district": "Kadıköy", "listed_date": "2026-09-24",
           "url": f"https://www.sahibinden.com/ilan/vasita-otomobil-{n}/detay",
           "attributes": {"year": "2018", "km": "167.000", "color": "Beyaz"}}
    return {**row, **overrides}


def diagram(**marked) -> dict[str, str]:
    """All 13 parts original, except the ones named: diagram(hood="painted")."""
    return {panel: marked.get(panel, "original") for panel in PANELS}


def search(*argv: str) -> int:
    """`search` as the CLI runs it, minus main(), which would reload the real
    config and with it the real database path."""
    args = cli.build_parser().parse_args(["search", *argv])
    args.json = getattr(args, "json", False)
    return cli.cmd_search(args)


def main() -> int:
    failures = []
    real_db = Path(Config.load().db_path)
    real_db_state = real_db.stat().st_mtime_ns if real_db.exists() else None
    # Nothing here may write where the real searches and tables are kept.
    real_searches = saved.SEARCHES_DIR
    real_searches_state = sorted(real_searches.glob("*")) if real_searches.exists() else None
    saved.SEARCHES_DIR = Path(tempfile.mkdtemp()) / "searches"
    cli.RESULTS_DIR = Path(tempfile.mkdtemp()) / "results"

    def check(label, got, want):
        ok = got == want
        if not ok:
            failures.append(label)
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<54} {got!r}"
              + ("" if ok else f"  (want {want!r})"))

    def refused(parse, *given):
        try:
            parse(*given)
        except Invalid:
            return True
        return False

    print("\n1. numbers are read the way people write them")
    for text, want in (("1500000", 1_500_000), ("1.500.000", 1_500_000),
                       ("1,500,000", 1_500_000), ("1.5m", 1_500_000),
                       ("1,5m", 1_500_000), ("850k", 850_000), ("150 bin", 150_000)):
        check(f"{text!r}", parse_amount(text), want)
    check("'1.5' is refused, not read as 15", refused(parse_amount, "1.5"), True)
    check("a range", parse_range("500k-1.5m", "max"), (500_000, 1_500_000))
    check("an open top", parse_range("2018-", "max"), (2018, None))
    check("an open bottom", parse_range("-150k", "min"), (None, 150_000))
    check("a bare price is an upper bound", parse_range("1.5m", "max"), (None, 1_500_000))
    check("a bare year is a lower bound", parse_range("2018", "min"), (2018, None))
    check("0 is no bound", parse_range("0-150k", "max"), (None, 150_000))
    check("backwards is refused", refused(parse_range, "2m-1m", "max"), True)

    print("\n2. only what was answered becomes a flag")
    tax = taxonomy()
    argv, _, unused = sitting(tax, {"Brand": ["vw"], "Model": ["passat"],
                                    "Price": ["-1.5m"], "Search now": ["y"]})
    check("every scripted answer was asked for", unused, {})
    check("the search", argv, ["--brand", "Volkswagen", "--model", "Passat",
                               "--max-price", "1500000", "--check", "15",
                               "--markdown"])

    print("\n3. a wrong answer is asked again, with what would work")
    argv, text, unused = sitting(tax, {
        "Brand": ["", "folksvgn", "volksvagen"], "Model": ["pasat"],
        "City": ["istambol", "istanbul"], "District": ["Kadıköy"],
        "Year": ["1900", "2018"], "Seller": ["owner, dealer", "owner"],
        "Search now": ["y"]})
    check("every scripted answer was asked for", unused, {})
    check("an empty brand is not allowed", "a brand is needed" in text, True)
    check("a far-off brand gets a suggestion", "Did you mean Volkswagen?" in text, True)
    check("  and a near one is matched", argv[:2], ["--brand", "Volkswagen"])
    check("a misspelled model is matched", argv[argv.index("--model") + 1], "Passat")
    check("a far-off city gets a suggestion", "Did you mean İstanbul" in text, True)
    check("the city is sahibinden's spelling", argv[argv.index("--city") + 1], "İstanbul")
    check("a year out of range is refused", "1900 is not a model year" in text, True)
    check("a single-choice filter refuses two", "--seller takes one value" in text, True)
    check("  and takes one", argv[argv.index("--seller") + 1], "Sahibinden")

    print("\n4. English, several values, and numbers standing for options")
    argv, _, unused = sitting(tax, {
        "Brand": ["vw"], "Model": ["golf"],
        "Fuel": ["diesel, hybrid"], "Gearbox": ["automatic"], "Colour": ["white, black"],
        "Heavy-damage": ["no"], "Paint": ["both"], "Posted within": ["24"],
        "More filters": ["y"], "Engine": ["1.6"], "Horsepower": ["110-160"],
        "Photos": ["video"], "Order": ["newest"], "Pages": ["2"], "Search now": ["y"]})
    check("every scripted answer was asked for", unused, {})

    def values(flag):
        return [argv[i + 1] for i, a in enumerate(argv) if a == flag]

    check("two fuels", values("--fuel"), ["Dizel", "Benzin (Hibrit)"])
    check("two colours", values("--color"), ["Beyaz", "Siyah"])
    check("no heavy damage", values("--heavy-damage"), ["Hayır"])
    check("both paint switches", "--undamaged" in argv, True)
    check("24 is the last 24 hours", values("--listed-within"), ["Son 24 saat"])
    check("1.6 litres is its cm3 band", values("--engine"), ["1301 - 1600 cm3"])
    check("110-160 HP ticks the bands it spans", values("--power"),
          ["101 - 125 HP", "126 - 150 HP", "151 - 175 HP"])
    check("order and pages", (values("--sort"), values("--pages")), (["date_desc"], ["2"]))

    print("\n5. an SUV model brings the SUV sidebar with it")
    argv, text, unused = sitting(tax, {"Brand": ["vw"], "Model": ["tiguan"],
                                       "Body": ["sedan", "suv"], "Search now": ["y"]})
    check("every scripted answer was asked for", unused, {})
    check("sedan is not an SUV body", "unknown Kasa Tipi value 'sedan'" in text, True)
    check("SUV is", argv[argv.index("--body") + 1], "SUV")
    check("no --category needed", "--category" in argv, False)
    argv, _, unused = sitting(tax, {"Brand": ["vw"], "Category": ["suv"],
                                    "Search now": ["y"]})
    check("without a model, the category is asked", values("--category"), ["suv"])

    print("\n6. a model list is read from sahibinden only when it is needed")
    calls = []

    def learn(targets):
        calls.append([t["brand"] for t in targets])
        tax.merge({"categories": {CAR: {"models": {"Toyota": {
            "complete": True, "models": {"Corolla": "toyota-corolla"}}}}}})
        tax.save()
        return {"ok": True}

    argv, _, _ = sitting(tax, {"Brand": ["toyota"], "Search now": ["y"]}, learn=learn)
    check("skipping the model reads nothing", calls, [])
    argv, _, unused = sitting(tax, {"Brand": ["toyota"], "Model": ["corolla"],
                                    "Search now": ["y"]}, learn=learn)
    check("naming one reads the list once", len(calls), 1)
    check("  and then finds the model", argv[argv.index("--model") + 1], "Corolla")
    argv, text, unused = sitting(tax, {"Brand": ["hyundai"], "Model": ["i20", ""],
                                       "Search now": ["y"]},
                                 learn=lambda t: {"error": "challenged", "reason": "blocked"})
    check("a refused read says so", "could not read the model list: challenged" in text, True)
    check("  and Enter searches every model", "--model" in argv, False)

    print("\n7. the search is shown, and can be declined")
    argv, text, _ = sitting(tax, {"Brand": ["vw"], "Words in the title": ["cam tavan"],
                                  "Search now": ["n"]})
    check("declined", argv, None)
    check("the search is shown first",
          "Your search" in text and "cam tavan" in text, True)

    print("\n8. the Markdown table")
    rows = [listing(1), listing(2, title="Hatasız | boyasız"),
            listing(3, price_try=None, price_raw="35.000 USD", price_currency="USD")]
    result = {"total_results": 3, "pages_fetched": 1, "pages_available": 1,
              "listings": rows,
              "resolved": {"url": "https://www.sahibinden.com/volkswagen-passat",
                           "brand": "Volkswagen", "model": "Passat",
                           "category_label": "Otomobil", "city": "İstanbul",
                           "filters_applied": {"Yakıt / Motor Tipi": ["Dizel"]}},
              "summary": {"matched": 3, "min_price": 1_050_000, "max_price": 1_100_000,
                          "median_price": 1_075_000, "other_currencies": {"USD": 1}}}
    query = argparse.Namespace(min_year=2018, max_year=None, min_km=None,
                               max_km=150_000, min_price=None, max_price=None)
    page = markdown(result, query, order="price_asc",
                    when=dt.datetime(2026, 9, 27, 14, 5))
    lines = page.splitlines()
    table = [line for line in lines if line.startswith("| ")]
    check("a header and one row per car", len(table), 4)
    check("every row has the same cells",
          {len(re.split(r"(?<!\\)\|", line)) for line in table}, {13})
    check("a | in a title is escaped", "Hatasız \\| boyasız" in page, True)
    check("each row links its listing",
          "[open](https://www.sahibinden.com/ilan/vasita-otomobil-1/detay)" in table[1], True)
    check("km with thousands", "| 167,000 |" in table[1], True)
    check("a foreign price keeps its unit", "| 35.000 USD |" in table[3], True)
    check("the filters are listed", "Year: from 2018 · KM: up to 150,000" in page, True)
    check("  with the sidebar's own", "Yakıt / Motor Tipi: Dizel" in page, True)
    check("--limit cuts the table", markdown(result, query, order="price_asc",
                                             limit=1).count("[open]"), 1)
    empty = {**result, "listings": [], "total_results": 0, "pages_available": 0,
             "summary": {"matched": 0}}
    check("no rows says so", "_No listings matched._" in markdown(empty, query,
                                                                   order="price_asc"), True)

    print("\n9. `ask` runs the answers as a search and saves the table")
    scratch = Path(tempfile.mkdtemp())
    real = (cli.sweep, cli.RESULTS_DIR, cli.BREAKER_PATH, cli.ask_search,
            cli.Taxonomy, config.db_path, saved.SEARCHES_DIR)
    try:
        cli.BREAKER_PATH = scratch / "breaker.json"
        cli.sweep = lambda url, **kw: {"base_url": url, "total_results": 2,
                                       "pages_fetched": 1, "pages_available": 1,
                                       "listings": [listing(1), listing(2)],
                                       "breaker": "test"}
        cli.RESULTS_DIR = scratch
        saved.SEARCHES_DIR = scratch / "searches"
        cli.Taxonomy = lambda: tax
        cli.ask_search = lambda tax, **kw: ["--brand", "Volkswagen", "--model",
                                            "Passat", "--markdown"]
        set_config(Config.load().override(db_path=str(scratch / "test.db")))
        code = cli.cmd_ask(argparse.Namespace(json=False))
        tables = list(scratch.glob("*_volkswagen-passat.md"))
        check("the search succeeds", code, cli.EXIT_OK)
        check("the table is saved", len(tables), 1)
        check("  with both cars", tables[0].read_text(encoding="utf-8").count("[open]")
              if tables else 0, 2)
        kept = scratch / "searches" / "volkswagen-passat.json"
        check("the filters are saved for next time",
              json.loads(kept.read_text(encoding="utf-8")) if kept.exists() else None,
              {"brand": "Volkswagen", "model": "Passat", "markdown": True})
    finally:
        (cli.sweep, cli.RESULTS_DIR, cli.BREAKER_PATH, cli.ask_search,
         cli.Taxonomy, db_path, saved.SEARCHES_DIR) = real
        set_config(Config.load().override(db_path=db_path))

    print("\n10. body parts are asked for by rule, part or group")
    argv, text, unused = sitting(tax, {
        "Brand": ["vw"], "Model": ["passat"],
        "Parts that must be original": ["hood, roof"],
        "Parts that may be painted": ["doorz", "structural"],
        "Parts that may have local paint": ["sol ön kapı"],
        "Most painted": ["two", "2"], "Most replaced": ["0"],
        "Order": ["condition"], "Pages": ["1"], "Cars to open": ["16", "10"],
        "Search now": ["y"]})
    check("every scripted answer was asked for", unused, {})
    check("hood and roof must be original", values("--original"), ["hood", "roof"])
    check("a misspelled part gets a suggestion", "Did you mean doors?" in text, True)
    check("a group is kept as a group", values("--not-changed"), ["structural"])
    check("Turkish part names work", values("--not-painted"), ["front_left_door"])
    check("whole-car limits", (values("--max-painted"), values("--max-changed")),
          (["2"], ["0"]))
    check("condition order ranks, and reads the cheapest",
          ("--best-first" in argv, "--sort" in argv), (True, False))
    check("no more pages than the cap", "from 0 to 15" in text, True)
    check("pages to open", values("--check"), ["10"])
    check("the cost is said before searching", "1 + 10 = 11 of today's 30" in text, True)
    parsed = cli.build_parser().parse_args(["search", *argv])
    check("the command runs as a search",
          build_requirements(original=parsed.original, not_changed=parsed.not_changed,
                             not_painted=parsed.not_painted)[1], None)

    print("\n11. why a car fails, and which is in better shape")
    rules, _ = build_requirements(original=["hood", "roof"], not_changed=["structural"])
    check("the part that broke the rule",
          explain(diagram(hood="painted", rear_bumper="changed"), rules),
          ["hood painted"])
    check("a whole-car limit", explain(diagram(trunk="painted", roof="local_painted"),
                                       {}, max_painted=1), ["2 painted (at most 1)"])
    ranked = sorted([("painted", condition_rank(diagram(hood="painted"))),
                     ("replaced", condition_rank(diagram(trunk="changed"))),
                     ("heavy damage", condition_rank(diagram(), "evet")),
                     ("clean", condition_rank(diagram())),
                     ("unknown", condition_rank(None)),
                     ("local", condition_rank(diagram(hood="local_painted")))],
                    key=lambda pair: pair[1])
    check("best first", [name for name, _ in ranked],
          ["clean", "local", "painted", "replaced", "heavy damage", "unknown"])

    print("\n12. diagrams are read once, and at most as many as allowed")
    scratch = Path(tempfile.mkdtemp())
    real_db_path, real_fetch = config.db_path, crawl.fetch_details
    set_config(Config.load().override(db_path=str(scratch / "test.db")))
    rows = [listing(n) for n in range(1, 7)]
    opened: list[list[str]] = []

    def fetch(ids, limit=None, attach=False):
        opened.append(list(ids))
        with Store(config.db_path) as store:
            for i in ids:
                n = int(i) - 1339324870
                parts = {1: diagram(hood="painted"), 3: None,
                         4: diagram(rear_bumper="changed")}.get(n, diagram())
                if n == 5:
                    continue  # the page could not be opened
                store.record_detail({"listing_id": i, "heavy_damage_record": "hayir",
                                     "damage": {"parts": parts} if parts else None})
        return {"fetched": [{"listing_id": i} for i in ids if not i.endswith("75")],
                "failures": [{"listing_id": rows[4]["listing_id"], "verdict": "challenged",
                              "reason": "blocked"}]}

    try:
        crawl.fetch_details = fetch
        with Store(config.db_path) as store:
            store.record_sweep({"base_url": "https://example/search", "layout": "vehicle",
                                "total_results": 6, "pages_fetched": 1,
                                "pages_available": 1, "listings": rows})
            store.record_detail({"listing_id": rows[1]["listing_id"],
                                 "heavy_damage_record": "evet",
                                 "damage": {"parts": diagram(front_left_door="painted")}})
        report = crawl.check_diagrams(rows, rules, fetch=4)
        verdicts = {r["listing_id"][-1]: r["panel_verdict"] for r in rows}
        check("a stored diagram is not opened again", opened, [[
            rows[0]["listing_id"], rows[2]["listing_id"], rows[3]["listing_id"],
            rows[4]["listing_id"]]])
        check("only as many opened as asked", report["opened_now"], 3)
        check("verdicts", verdicts, {"1": "fail", "2": "pass", "3": "unchecked",
                                     "4": "pass", "5": "unchecked", "6": "unchecked"})
        check("the failure says why", rows[0]["failed_on"], ["hood painted"])
        check("a painted door passes 'not replaced'", rows[1]["panels"]["front_left_door"],
              "painted")
        check("  and its heavy-damage record is kept", rows[1]["heavy_damage_record"], "evet")
        check("no diagram on the page", rows[2]["unchecked_reason"],
              "no damage diagram on its page")
        check("a page that could not be opened",
              rows[4]["unchecked_reason"], "could not open it (challenged)")
        check("past the cap", rows[5]["unchecked_reason"], "not opened yet")
        check("stopping is reported", report["stopped"], "challenged: blocked")

        def refuse(ids, limit=None, attach=False):
            raise RuntimeError("Daily budget spent (40/40). Resumes tomorrow.")

        crawl.fetch_details = refuse
        again = [listing(n) for n in range(1, 7)]
        report = crawl.check_diagrams(again, rules, fetch=15)
        check("a refusal does not lose the results", report["stopped"],
              "Daily budget spent (40/40). Resumes tomorrow.")
        check("  and what was read before still counts", (report["passed"], report["failed"]),
              (2, 1))

        print("\n13. `search` with body parts")
        real_sweep = cli.sweep
        cli.sweep = lambda url, **kw: {"base_url": url, "total_results": 6,
                                       "pages_fetched": 1, "pages_available": 1,
                                       "listings": [listing(n) for n in range(1, 7)],
                                       "breaker": "test"}
        real_tax, cli.Taxonomy = cli.Taxonomy, lambda: tax
        crawl.fetch_details = fetch
        try:
            code = search("--brand", "vw", "--original", "kaputt", "--json")
            check("a misspelled part stops before fetching", code, cli.EXIT_ERROR)
            code = search("--brand", "vw", "--original", "hood", "--no-store")
            check("checking needs the store", code, cli.EXIT_ERROR)
            out = scratch / "body.md"
            code = search("--brand", "vw", "--model", "passat",
                          "--original", "hood", "--original", "roof",
                          "--not-changed", "structural", "--check", "0",
                          "--best-first", "--save", str(out))
            page = out.read_text(encoding="utf-8")
            check("the search succeeds", code, cli.EXIT_OK)
            check("sections", [line for line in page.splitlines() if line.startswith("###")],
                  ["### Passed the body-part check (2)", "### Not checked yet (3)",
                   "### Did not pass (1)"])
            passed = page.split("### Passed")[1].split("###")[0]
            table = [line for line in passed.splitlines() if line.startswith("| ")]
            check("16 columns, 5 of them the body", table[0].count(" | ") + 1, 16)
            check("best shape first", [line.split(" | ")[1] for line in table[1:]],
                  [rows[3]["listing_id"], rows[1]["listing_id"]])
            check("what is not original", "painted: front left door" in table[2], True)
            check("heavy damage stands out", "| YES |" in table[2], True)
            check("a rear bumper is allowed to be replaced",
                  "replaced: rear bumper" in table[1], True)
        finally:
            cli.sweep, cli.Taxonomy = real_sweep, real_tax
    finally:
        crawl.fetch_details = real_fetch
        set_config(Config.load().override(db_path=real_db_path))

    print("\n14. a search saved as a file")
    search_parser = cli._subcommand(cli.build_parser(), "search")
    asked = ["--brand", "Skoda", "--model", "Scala", "--min-price", "1000000",
             "--max-price", "1300000", "--min-year", "2020", "--max-km", "70000",
             "--fuel", "Benzin", "--transmission", "Otomatik", "--heavy-damage", "Hayır",
             "--original", "hood", "--original", "roof", "--best-first",
             "--check", "15", "--markdown"]
    data = saved.to_file(asked, search_parser)
    check("numbers are numbers, lists are lists",
          (data["max-price"], data["original"], data["markdown"]),
          (1_300_000, ["hood", "roof"], True))
    path = saved.save(asked, search_parser, "skoda-scala")
    check("saved under its name", path.name, "skoda-scala.json")
    check("the same search saved again", saved.save(asked, search_parser, "skoda-scala"),
          path)
    other = saved.save(asked + ["--pages", "1"], search_parser, "skoda-scala")
    check("a different one never replaces it", other.name, "skoda-scala-2.json")

    def parsed(argv):
        return vars(cli.build_parser().parse_args(["search", *argv]))

    check("read back, it is the same search",
          parsed(saved.load("skoda-scala", search_parser)), parsed(asked))

    def refusal(data):
        try:
            saved.to_argv(data, search_parser, source="x.json")
        except ValueError as exc:
            return str(exc)
        return None

    check("a misspelled option", refusal({"max_prize": 1}).startswith(
          "x.json: 'max_prize' is not a search option (did you mean max-price"), True)
    check("underscores work too", saved.to_argv({"max_price": 5}, search_parser,
                                                source="x"), ["--max-price", "5"])
    check("one value, not a list", refusal({"brand": ["vw", "bmw"]}),
          "x.json: 'brand' takes one value, not a list")
    check("a number", refusal({"max-km": "lots"}),
          "x.json: 'max-km' must be a number, got 'lots'")
    check("a switch", refusal({"best-first": "yes"}),
          "x.json: 'best-first' is a switch: write true or false")
    check("a file cannot name another", refusal({"filters": "x"}) is not None, True)
    broken = saved.SEARCHES_DIR / "broken.json"
    broken.write_text('{"brand": "vw",}', encoding="utf-8")
    try:
        saved.load("broken", search_parser)
        said = None
    except ValueError as exc:
        said = str(exc)
    check("bad JSON says where", said and "line 1" in said, True)
    try:
        saved.load("nothing-here", search_parser)
        said = None
    except ValueError as exc:
        said = str(exc)
    check("a missing name lists the saved ones",
          said and said.endswith("Saved: broken, skoda-scala, skoda-scala-2"), True)

    parser = cli.build_parser()
    argv = cli.expand_filters(["--json", "search", "--filters", "skoda-scala",
                               "--pages", "2", "--fuel", "Dizel"], parser, "skoda-scala")
    run = vars(parser.parse_args(argv))
    check("typed flags change that run", (run["pages"], run["fuel"]),
          (2, ["Benzin", "Dizel"]))
    check("the rest comes from the file", (run["brand"], run["original"], run["check"]),
          ("Skoda", ["hood", "roof"], 15))
    check("the table is kept like ask's",
          Path(run["save"]).parent == cli.RESULTS_DIR
          and run["save"].endswith("_skoda-scala.md"), True)
    argv = cli.expand_filters(["search", "--filters", str(path), "--save", "mine.md"],
                              parser, str(path))
    check("a path works, and --save wins", vars(parser.parse_args(argv))["save"], "mine.md")

    saved.SEARCHES_DIR = real_searches
    check("the real searches/ was not touched",
          sorted(real_searches.glob("*")) if real_searches.exists() else None,
          real_searches_state)

    check("the real database was not touched",
          real_db.stat().st_mtime_ns if real_db.exists() else None, real_db_state)

    print("\n" + "=" * 62)
    print("FAILURES:" if failures else "all checks passed",
          *failures, sep="\n  " if failures else "")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
