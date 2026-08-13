"""Command line interface — the surface a person or a coding agent drives.

Design notes for agent use:

* Every command supports ``--json``. Agents should use it: the human tables are
  for reading, the JSON is stable and typed.
* ``capabilities --json`` describes every command and argument in one call, so
  an agent can discover the tool without scraping ``--help`` text.
* Exit codes are meaningful: 0 success, 1 error, 2 refused (budget spent,
  cooling down, or a session problem). A 2 is not a failure to retry — it means
  waiting is the correct response.
* Nothing is hardcoded to a particular search. Brand, model, city, filters,
  budgets and pacing are all arguments or config.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from .config import CONFIG_PATH, Config, config, set_config
from .crawl import fetch_details, sweep
from .parse.detail import parse_detail
from .fetch.fetcher import BREAKER_PATH
from .fetch.guard import Breaker
from .store import Store
from .taxonomy import Taxonomy, apply_post_filters

EXIT_OK, EXIT_ERROR, EXIT_REFUSED = 0, 1, 2


def emit(data: dict, as_json: bool, human) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        human(data)


def _money(value) -> str:
    return f"{value:,} TL" if isinstance(value, int) else "-"


# --------------------------------------------------------------- commands


def cmd_search(args) -> int:
    """Resolve a query to a sahibinden URL, sweep it, store and summarise."""
    tax = Taxonomy()

    if args.url:
        url, resolved, post = args.url, {"url": args.url, "source": "explicit"}, {}
    else:
        if not args.brand:
            print("error: need --brand (or --url). See: sahibinden brands", file=sys.stderr)
            return EXIT_ERROR
        resolved = tax.build_url(
            args.brand, args.model, city=args.city,
            min_year=args.min_year, max_year=args.max_year, max_km=args.max_km,
            min_price=args.min_price, max_price=args.max_price,
            undamaged_only=args.undamaged, sort=args.sort,
        )
        if resolved.get("error"):
            print(json.dumps(resolved, ensure_ascii=False, indent=2)
                  if args.json else
                  f"error: {resolved['error']}\ndid you mean: "
                  f"{', '.join(resolved.get('did_you_mean') or []) or '(no close match)'}",
                  file=sys.stderr)
            return EXIT_ERROR
        url, post = resolved["url"], resolved["post_filters"]

    result = sweep(url, max_pages=args.pages or config.default_pages,
                   layout=args.layout)
    if result.get("error"):
        print(f"refused: {result['error']} {result.get('reason','')}", file=sys.stderr)
        return EXIT_REFUSED

    matched = apply_post_filters(result["listings"], post)
    result["listings"] = matched
    result["post_filters_applied"] = post
    result["resolved"] = resolved

    stored = None
    if not args.no_store:
        with Store(config.db_path) as store:
            stored = store.record_sweep(result)
    result["stored"] = stored

    prices = [x["price_try"] for x in matched if x["price_try"]]
    result["summary"] = {
        "matched": len(matched),
        "min_price": min(prices) if prices else None,
        "max_price": max(prices) if prices else None,
        "median_price": int(statistics.median(prices)) if prices else None,
    }

    def human(d):
        r = d["resolved"]
        print(f"\n  url            : {r['url']}")
        if r.get("model_is_guess"):
            print(f"  model slug     : guessed from {r['model']!r}; "
                  f"{'confirmed by results' if d['total_results'] else 'NO RESULTS — check spelling'}")
        print(f"  total on site  : {d['total_results']}")
        print(f"  pages fetched  : {d['pages_fetched']} of {d['pages_available']}")
        if d["post_filters_applied"]:
            print(f"  post-filters   : {d['post_filters_applied']} "
                  f"(applied to rows; sahibinden cannot filter these in-URL)")
        s = d["summary"]
        print(f"  matched        : {s['matched']}")
        if s["median_price"]:
            print(f"  price range    : {_money(s['min_price'])} - {_money(s['max_price'])}")
            print(f"  median         : {_money(s['median_price'])}")
        if d.get("stored"):
            st = d["stored"]
            print(f"  stored         : {st['new']} new, "
                  f"{len(st['price_changes'])} price changes, "
                  f"{len(st['delisted'])} delisted")
        print(f"  breaker        : {d['breaker']}\n")
        print(f"  {'id':<12}{'year':<6}{'km':<10}{'price':>12}  location")
        print("  " + "-" * 62)
        for x in d["listings"][:args.limit]:
            a = x.get("attributes") or {}
            print(f"  {x['listing_id']:<12}{a.get('year',''):<6}{a.get('km',''):<10}"
                  f"{x['price_try'] or 0:>12,}  {x.get('city') or ''} {x.get('district') or ''}")
        if len(d["listings"]) > args.limit:
            print(f"  ... {len(d['listings']) - args.limit} more (use --limit)")

    emit(result, args.json, human)
    return EXIT_OK


def cmd_status(args) -> int:
    breaker = Breaker(BREAKER_PATH)
    with Store(config.db_path) as store:
        stats = store.stats()
    data = {
        "budget_remaining": breaker.remaining,
        "daily_budget": breaker.daily_limit,
        "cooling_down": breaker.cooling_down,
        "breaker": breaker.status(),
        "delay_multiplier": round(breaker.delay_multiplier, 2),
        "in_quiet_hours": config.in_quiet_hours(),
        "config": config.describe(),
        "config_path": str(CONFIG_PATH),
        "database": stats,
    }

    def human(d):
        print(f"  breaker    : {d['breaker']}")
        print(f"  quiet hours: {'yes' if d['in_quiet_hours'] else 'no'} "
              f"(safest time to sweep — an IP ban then costs you nothing)")
        print(f"  config     : {d['config']}")
        print("  database   : " + ", ".join(f"{k}={v}" for k, v in d["database"].items()))

    emit(data, args.json, human)
    return EXIT_REFUSED if breaker.cooling_down else EXIT_OK


def cmd_brands(args) -> int:
    tax = Taxonomy()
    names = sorted(tax.brands)
    if args.filter:
        needle = args.filter.lower()
        names = [n for n in names if needle in n.lower()]
    data = {"count": len(names), "brands": names}
    emit(data, args.json, lambda d: print("  " + "\n  ".join(d["brands"])))
    return EXIT_OK


def cmd_cities(args) -> int:
    tax = Taxonomy()
    names = sorted(tax.cities)
    if args.filter:
        needle = args.filter.lower()
        names = [n for n in names if needle in n.lower()]
    data = {"count": len(names), "cities": names}
    emit(data, args.json, lambda d: print("  " + "\n  ".join(d["cities"])))
    return EXIT_OK


def cmd_history(args) -> int:
    with Store(config.db_path) as store:
        rows = [dict(r) for r in store.price_history(args.listing_id)]
    data = {"listing_id": args.listing_id, "points": rows}

    def human(d):
        if not d["points"]:
            print(f"  no price history for {d['listing_id']}")
            return
        first = d["points"][0]["price_try"]
        for row in d["points"]:
            delta = row["price_try"] - first
            print(f"  {row['observed_at']}  {_money(row['price_try'])}"
                  + (f"   ({delta:+,} from first seen)" if delta else ""))

    emit(data, args.json, human)
    return EXIT_OK


def cmd_config(args) -> int:
    if args.set:
        updates = {}
        for pair in args.set:
            if "=" not in pair:
                print(f"error: --set expects key=value, got {pair!r}", file=sys.stderr)
                return EXIT_ERROR
            key, value = pair.split("=", 1)
            updates[key.strip()] = value.strip()
        new = Config.load().override(**updates)
        path = new.save()
        set_config(new)
        print(f"saved {path}")

    current = Config.load()
    data = {k: v for k, v in vars(current).items() if not k.startswith("_")}
    data["_path"] = str(CONFIG_PATH)
    emit(data, args.json,
         lambda d: print("\n".join(f"  {k:<18} {v}" for k, v in d.items())))
    return EXIT_OK



def cmd_details(args) -> int:
    """Fetch detail pages (damage records) for queued or named listings."""
    result = fetch_details(args.listing_id or None, limit=args.limit,
                           max_price=args.max_price)
    if result.get("error"):
        print(f"refused: {result['error']}", file=sys.stderr)
        return EXIT_REFUSED

    def human(d):
        print(f"\n  fetched {len(d['fetched'])} of {d['requested']} requested")
        if d.get("unknown_ids"):
            print(f"  unknown ids (not in database): {', '.join(d['unknown_ids'])}")
        for f in d["failures"]:
            print(f"  FAILED {f['listing_id']}: {f['verdict']} — {f['reason']}")
        print()
        print(f"  {'id':<12}{'price':>12}  {'agir hasar':<12}{'damage':<34}flag")
        print("  " + "-" * 78)
        for f in d["fetched"]:
            c = f["damage_counts"]
            summary = ", ".join(f"{k}:{v}" for k, v in c.items()) or "no diagram"
            flag = "<-- LOOKS CLEAN BUT FLAGGED" if f["clean_looking_but_flagged"] else ""
            print(f"  {f['listing_id']:<12}{f['price_try'] or 0:>12,}  "
                  f"{str(f['heavy_damage_record']):<12}{summary:<34}{flag}")
        print(f"\n  breaker: {d['breaker']}")

    emit(result, args.json, human)
    return EXIT_OK


def cmd_listing(args) -> int:
    """Everything stored about one listing, search row plus detail."""
    with Store(config.db_path) as store:
        row = store.db.execute(
            "SELECT l.*, d.heavy_damage_record, d.damage, d.description,"
            " d.original_count, d.local_painted_count, d.painted_count,"
            " d.changed_count, d.spec, d.equipment, d.fetched_at AS detail_fetched_at"
            " FROM listings l LEFT JOIN listing_details d"
            " ON d.listing_id = l.listing_id WHERE l.listing_id = ?",
            (args.listing_id,),
        ).fetchone()
        if row is None:
            print(f"error: {args.listing_id} not in database. Run a search first.",
                  file=sys.stderr)
            return EXIT_ERROR
        data = dict(row)
        for key in ("attributes", "damage", "spec", "equipment"):
            if data.get(key):
                try:
                    data[key] = json.loads(data[key])
                except (TypeError, ValueError):
                    pass
        data["price_history"] = [dict(r) for r in store.price_history(args.listing_id)]

    def human(d):
        print(f"\n  {d['title']}")
        print(f"  {d['listing_id']}   {_money(d['last_price_try'])}   "
              f"{d.get('city') or ''} {d.get('district') or ''}")
        attrs = d.get("attributes") or {}
        if attrs:
            print("  " + "   ".join(f"{k}={v}" for k, v in attrs.items()))
        print(f"  first seen {d['first_seen_at']}   last seen {d['last_seen_at']}"
              + (f"   DELISTED {d['delisted_at']}" if d.get("delisted_at") else ""))
        if len(d["price_history"]) > 1:
            print("\n  price history:")
            for p in d["price_history"]:
                print(f"    {p['observed_at']}  {_money(p['price_try'])}")
        if d.get("detail_fetched_at"):
            print(f"\n  agir hasar kayitli : {d['heavy_damage_record']}")
            print(f"  panels             : original={d['original_count']} "
                  f"local_painted={d['local_painted_count']} "
                  f"painted={d['painted_count']} changed={d['changed_count']}")
            # The two sources disagree often enough that showing them together
            # is the point of fetching details at all.
            if d["heavy_damage_record"] == "evet" and not d["changed_count"]:
                print("  NOTE: diagram looks clean but the vehicle carries a "
                      "registered heavy-damage record.")
            if d.get("description"):
                body = d["description"][:600].replace("\n", "\n    ")
                print(f"\n  seller description:\n    {body}")
        else:
            print("\n  no detail fetched yet — run: sahibinden details "
                  f"--listing-id {d['listing_id']}")

    emit(data, args.json, human)
    return EXIT_OK



def cmd_reparse(args) -> int:
    """Re-run the current parsers over stored raw HTML. No network.

    Every fetched page is written to disk before parsing, so improving a parser
    or fixing a field never requires re-fetching anything — which matters when
    requests are rationed and an IP ban costs an hour of your own browsing.
    """
    updated, missing, failed = 0, 0, []
    with Store(config.db_path) as store:
        rows = store.db.execute(
            "SELECT listing_id, raw_path FROM listing_details"
            " WHERE raw_path IS NOT NULL"
        ).fetchall()
        for row in rows:
            path = Path(row["raw_path"])
            if not path.exists():
                missing += 1
                continue
            try:
                detail = parse_detail(path.read_text(encoding="utf-8", errors="replace"))
                store.record_detail(detail, str(path))
                updated += 1
            except Exception as exc:
                failed.append({"listing_id": row["listing_id"],
                               "error": f"{type(exc).__name__}: {exc}"})
        stats = store.stats()

    data = {"candidates": len(rows), "reparsed": updated,
            "raw_file_missing": missing, "failed": failed, "database": stats}

    def human(d):
        print(f"  reparsed {d['reparsed']} of {d['candidates']} stored pages"
              + (f", {d['raw_file_missing']} raw files missing" if d["raw_file_missing"] else ""))
        for f in d["failed"]:
            print(f"  FAILED {f['listing_id']}: {f['error']}")

    emit(data, args.json, human)
    return EXIT_OK



def cmd_price(args) -> int:
    """Answer a market question from stored data. No network, no budget cost."""
    with Store(config.db_path) as store:
        data = store.market(
            brand=args.brand, model=args.model,
            min_year=args.min_year, max_year=args.max_year, max_km=args.max_km,
            city=args.city, exclude_damaged=args.clean_only,
            include_delisted=args.include_delisted,
        )
    data["query"] = {k: v for k, v in vars(args).items()
                     if k not in ("func", "json", "command") and v}

    def human(d):
        if not d["matched"]:
            print("  no stored listings match. Run a search first:")
            print("    sahibinden-cli search --brand <brand> --model <model>")
            return
        p_ = d["price"]
        print(f"\n  matched      : {d['matched']} listings")
        print(f"  price        : min {_money(p_['min'])}   p25 {_money(p_['p25'])}   "
              f"median {_money(p_['median'])}   p75 {_money(p_['p75'])}   "
              f"max {_money(p_['max'])}")
        if d["km_median"]:
            print(f"  median km    : {d['km_median']:,}")
        dm = d["damage"]
        print(f"  damage       : {dm['checked']} checked "
              f"({dm['heavy_damage_flagged']} heavy-damage flagged, "
              f"{dm['any_panel_changed']} with changed panels), "
              f"{dm['unchecked']} unchecked")
        t_ = d["turnover"]
        if t_["avg_days_to_delist"]:
            print(f"  turnover     : {t_['delisted_observed']} delisted, "
                  f"avg {t_['avg_days_to_delist']} days listed")
        else:
            print(f"  turnover     : not enough history yet "
                  f"(needs repeated complete sweeps)")
        print("\n  cheapest matches:")
        print(f"  {'id':<12}{'year':<6}{'km':>10}{'price':>12}  {'hasar':<7}city")
        print("  " + "-" * 62)
        for c in d["cheapest"]:
            flag = c["heavy_damage_record"] or "?"
            print(f"  {c['listing_id']:<12}{c['year'] or '':<6}{c['km'] or 0:>10,}"
                  f"{c['price_try']:>12,}  {flag:<7}{c['city'] or ''}")

    emit(data, args.json, human)
    return EXIT_OK



def cmd_nightly(args) -> int:
    """Refresh every search already tracked, then spend the detail budget.

    This is the unattended job. It re-sweeps each distinct search seen before,
    completely where the budget allows — completeness is what lets the store
    infer delisting, and delisting is the closest observable proxy for a sale.
    A partial sweep still records prices, it just cannot conclude anything from
    absence.
    """
    with Store(config.db_path) as store:
        searches = [r["base_url"] for r in store.db.execute(
            "SELECT base_url, MAX(started_at) AS last FROM sweep_runs"
            " GROUP BY base_url ORDER BY last"
        ).fetchall()]

    if not searches:
        print("nothing tracked yet — run a search first", file=sys.stderr)
        return EXIT_ERROR

    breaker = Breaker(BREAKER_PATH)
    report = {"started_budget": breaker.remaining, "searches": [], "details": None}

    for url in searches:
        if Breaker(BREAKER_PATH).remaining <= config.detail_budget:
            report["searches"].append({"url": url, "skipped": "budget reserved for details"})
            continue
        result = sweep(url, max_pages=args.pages)
        if result.get("error"):
            report["searches"].append({"url": url, "error": result["error"]})
            break
        with Store(config.db_path) as store:
            stored = store.record_sweep(result)
        report["searches"].append({
            "url": url,
            "complete": stored["complete"],
            "seen": stored["seen"],
            "new": stored["new"],
            "price_changes": len(stored["price_changes"]),
            "delisted": len(stored["delisted"]),
        })

    if not args.no_details:
        report["details"] = fetch_details(limit=config.detail_budget)

    report["ending_budget"] = Breaker(BREAKER_PATH).remaining

    def human(d):
        print(f"  budget {d['started_budget']} -> {d['ending_budget']}")
        for s in d["searches"]:
            if s.get("skipped") or s.get("error"):
                print(f"  - {s.get('skipped') or s['error']}: {s['url'][:60]}")
                continue
            print(f"  - {'complete' if s['complete'] else 'partial '} "
                  f"{s['seen']:>4} seen  {s['new']:>3} new  "
                  f"{s['price_changes']:>3} price changes  "
                  f"{s['delisted']:>3} delisted   {s['url'][:52]}")
        det = d.get("details") or {}
        if det.get("fetched"):
            flagged = sum(1 for f in det["fetched"] if f["clean_looking_but_flagged"])
            print(f"  - details: {len(det['fetched'])} fetched, "
                  f"{flagged} clean-looking but heavy-damage flagged")

    emit(report, args.json, human)
    return EXIT_OK



def cmd_sql(args) -> int:
    """Read-only SQL against the local database.

    Opened with mode=ro at the URI level rather than by pattern-matching the
    query text: a denylist of keywords is trivially bypassed and would still
    let a malformed query damage months of collected data. sqlite refuses the
    write itself, so there is nothing to get wrong here.
    """
    import sqlite3

    if args.schema:
        query = ("SELECT name, sql FROM sqlite_master "
                 "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    elif args.tables:
        query = ("SELECT name FROM sqlite_master WHERE type='table' "
                 "AND name NOT LIKE 'sqlite_%' ORDER BY name")
    elif args.query:
        query = args.query
    else:
        print("error: give a query, or --tables / --schema", file=sys.stderr)
        return EXIT_ERROR

    path = Path(config.db_path)
    if not path.exists():
        print(f"error: no database at {path}. Run a search first.", file=sys.stderr)
        return EXIT_ERROR

    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        rows = db.execute(query).fetchmany(args.limit)
        columns = [d[0] for d in db.execute(query).description]
    except sqlite3.Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        try:
            db.close()
        except Exception:
            pass

    data = {"query": query, "columns": columns,
            "row_count": len(rows), "rows": [dict(r) for r in rows]}

    def human(d):
        if not d["rows"]:
            print("  (no rows)")
            return
        if args.schema:
            for r in d["rows"]:
                print(f"\n{r['sql']};")
            return
        widths = {c: max(len(c), *(len(str(r[c])) for r in d["rows"])) for c in d["columns"]}
        widths = {c: min(w, 34) for c, w in widths.items()}
        print("  " + "  ".join(c[:widths[c]].ljust(widths[c]) for c in d["columns"]))
        print("  " + "  ".join("-" * widths[c] for c in d["columns"]))
        for r in d["rows"]:
            print("  " + "  ".join(str(r[c])[:widths[c]].ljust(widths[c])
                                   for c in d["columns"]))
        print(f"\n  {d['row_count']} row(s)")

    emit(data, args.json, human)
    return EXIT_OK


def cmd_capabilities(args) -> int:
    """Machine-readable description of the whole CLI, for agent discovery."""
    parser = build_parser()
    commands = {}
    for action in parser._subparsers._group_actions[0].choices.items():  # noqa: SLF001
        name, sub = action
        commands[name] = {
            "description": (sub.description or "").strip(),
            "arguments": [
                {
                    "flags": a.option_strings or [a.dest],
                    "help": a.help,
                    "required": a.required,
                    "default": a.default,
                }
                for a in sub._actions  # noqa: SLF001
                if a.dest != "help"
            ],
        }
    data = {
        "tool": "sahibinden-cli",
        "summary": "Query sahibinden.com vehicle and property listings from a "
                   "local cache, with rate-limited fetching.",
        "exit_codes": {"0": "success", "1": "error",
                       "2": "refused — budget spent or cooling down; wait, do not retry"},
        "notes": [
            "Pass --json on any command for stable machine-readable output.",
            "Year and km filters are applied to results, not the URL: sahibinden "
            "filters those by category-specific attribute ids that cannot be "
            "derived, and an invented parameter is silently ignored.",
            "Requests are budgeted per day and paced; a refused call means wait.",
        ],
        "commands": commands,
    }
    emit(data, True, lambda d: None)
    return EXIT_OK


# ----------------------------------------------------------------- parser


def _global_flags() -> argparse.ArgumentParser:
    """Flags valid on either side of the subcommand.

    argparse would otherwise accept them only before it, and every caller —
    human or agent — reaches for `sahibinden status --json` first. SUPPRESS is
    what makes both positions work: without it the subparser's own default
    overwrites a value already set by the top-level parser.
    """
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="emit JSON instead of a human table")
    shared.add_argument("--daily-budget", type=int, default=argparse.SUPPRESS,
                        help="override requests allowed today")
    shared.add_argument("--min-delay", type=float, default=argparse.SUPPRESS,
                        help="override minimum seconds between requests")
    shared.add_argument("--max-delay", type=float, default=argparse.SUPPRESS,
                        help="override maximum seconds between requests")
    return shared


def build_parser() -> argparse.ArgumentParser:
    shared = _global_flags()
    parser = argparse.ArgumentParser(
        prog="sahibinden-cli",
        parents=[shared],
        description="Fetch and query sahibinden.com listings. Sweeps are cheap "
                    "(~20 listings/request); detail pages are one request each "
                    "and separately capped.",
        epilog="Global flags (--json, --daily-budget, --min-delay, --max-delay) "
               "work before or after the subcommand. Run "
               "'sahibinden-cli capabilities' for a machine-readable description.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    p = sub.add_parser("search", parents=[shared], help="search listings and store them",
                       description="Resolve brand/model/city to a sahibinden URL, "
                                   "sweep it, store the results and summarise prices.",
                       formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--brand", help="e.g. Volkswagen (fuzzy-matched; see 'brands')")
    p.add_argument("--model", help="e.g. Golf (slug is guessed, verified by results)")
    p.add_argument("--city", help="e.g. Ankara (see 'cities')")
    p.add_argument("--url", help="explicit sahibinden search URL, bypassing the resolver")
    p.add_argument("--min-year", type=int, help="applied to results, not the URL")
    p.add_argument("--max-year", type=int, help="applied to results, not the URL")
    p.add_argument("--max-km", type=int, help="applied to results, not the URL")
    p.add_argument("--min-price", type=int, help="applied in the URL")
    p.add_argument("--max-price", type=int, help="applied in the URL")
    p.add_argument("--undamaged", action="store_true",
                   help="only unpainted/unchanged parts (sahibinden-side filter)")
    p.add_argument("--sort", default="price_asc",
                   choices=["price_asc", "price_desc", "date_desc", "date_asc"])
    p.add_argument("--pages", type=int, help="pages to fetch (default: config)")
    p.add_argument("--layout", default="vehicle", choices=["vehicle", "property"],
                   help="column layout of the results table")
    p.add_argument("--limit", type=int, default=20, help="rows to print")
    p.add_argument("--no-store", action="store_true", help="do not write to the database")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("status", parents=[shared], help="budget, breaker and database state",
                       description="Show remaining request budget, cooldown state "
                                   "and what is in the local database.")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("brands", parents=[shared], help="list known vehicle brands",
                       description="Brands taken from sahibinden's own dropdowns.")
    p.add_argument("--filter", help="substring match")
    p.set_defaults(func=cmd_brands)

    p = sub.add_parser("cities", parents=[shared], help="list known provinces",
                       description="Provinces taken from sahibinden's own dropdowns.")
    p.add_argument("--filter", help="substring match")
    p.set_defaults(func=cmd_cities)

    p = sub.add_parser("history", parents=[shared], help="price history for one listing",
                       description="Every recorded price change for a listing id.")
    p.add_argument("listing_id")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("config", parents=[shared], help="show or change configuration",
                       description="Show effective config, or persist changes "
                                   "with --set key=value.")
    p.add_argument("--set", action="append", metavar="KEY=VALUE",
                   help="persist a setting, repeatable")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("details", parents=[shared],
                       help="fetch damage records for listings",
                       description="Fetch detail pages, which are the only source "
                                   "of the 13-panel damage diagram and the "
                                   "agir_hasar_kayitli flag. One request per "
                                   "listing, capped separately from sweeps.",
                       formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--listing-id", action="append",
                   help="specific listing id, repeatable (default: work the queue)")
    p.add_argument("--limit", type=int, help="max listings (capped by detail_budget)")
    p.add_argument("--max-price", type=int, help="only queue listings at or below this")
    p.set_defaults(func=cmd_details)

    p = sub.add_parser("listing", parents=[shared],
                       help="show everything stored about one listing",
                       description="Search row, price history and damage record "
                                   "for a single listing id.")
    p.add_argument("listing_id")
    p.set_defaults(func=cmd_listing)

    p = sub.add_parser("price", parents=[shared],
                       help="market summary from stored data (no network)",
                       description="Aggregate stored listings into price "
                                   "percentiles, damage split and turnover. "
                                   "Reads the local database only — costs no "
                                   "requests and works while cooling down.",
                       formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--brand")
    p.add_argument("--model")
    p.add_argument("--city")
    p.add_argument("--min-year", type=int)
    p.add_argument("--max-year", type=int)
    p.add_argument("--max-km", type=int)
    p.add_argument("--clean-only", action="store_true",
                   help="only listings checked and found without heavy damage "
                        "or changed panels (unchecked listings are excluded, "
                        "since no record is not a clean record)")
    p.add_argument("--include-delisted", action="store_true",
                   help="include listings that have disappeared")
    p.set_defaults(func=cmd_price)

    p = sub.add_parser("nightly", parents=[shared],
                       help="unattended refresh of every tracked search",
                       description="Re-sweep every search seen before and spend "
                                   "the detail budget. Intended for a scheduled "
                                   "run during quiet hours.",
                       formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--pages", type=int, default=None,
                   help="cap pages per search (default: complete, budget allowing)")
    p.add_argument("--no-details", action="store_true",
                   help="sweeps only, skip detail fetching")
    p.set_defaults(func=cmd_nightly)

    p = sub.add_parser("sql", parents=[shared],
                       help="read-only SQL against the local database",
                       description="Run a query against data/sahibinden.db. "
                                   "The connection is opened read-only, so "
                                   "writes are refused by sqlite itself.",
                       formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("query", nargs="?", help="SQL to run")
    p.add_argument("--tables", action="store_true", help="list tables")
    p.add_argument("--schema", action="store_true", help="dump CREATE statements")
    p.add_argument("--limit", type=int, default=50, help="max rows returned")
    p.set_defaults(func=cmd_sql)

    p = sub.add_parser("reparse", parents=[shared],
                       help="re-parse stored raw HTML (no network)",
                       description="Re-run current parsers over every saved page. "
                                   "Use after a parser fix to backfill fields "
                                   "without spending requests.")
    p.set_defaults(func=cmd_reparse)

    p = sub.add_parser("capabilities", parents=[shared], help="machine-readable command description",
                       description="Dump every command and argument as JSON, for "
                                   "agent discovery.")
    p.set_defaults(func=cmd_capabilities)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # SUPPRESS means a flag is absent from the namespace unless the caller
    # actually passed it — in either position. getattr keeps that working
    # without the subparser's defaults clobbering a value set before it.
    args.json = getattr(args, "json", False)
    set_config(Config.load().override(
        daily_budget=getattr(args, "daily_budget", None),
        min_delay=getattr(args, "min_delay", None),
        max_delay=getattr(args, "max_delay", None),
    ))

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_ERROR
    except RuntimeError as exc:  # breaker refusals surface here
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
