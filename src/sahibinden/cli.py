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
from urllib.parse import parse_qsl, urlparse

from .config import CONFIG_PATH, Config, config, set_config
from .crawl import fetch_details, learn_models, sweep
from .panels import (PANEL_GROUPS, PANEL_LABELS, PANELS, STATUSES,
                     build_requirements, describe)
from .parse.detail import parse_detail
from .fetch.fetcher import BREAKER_PATH, RAW_DIR
from .fetch.guard import Breaker
from .store import Store
from .taxonomy import (CATEGORY_NAMES, ENUM_FLAGS, FLAG_FILTERS, Taxonomy,
                       apply_post_filters)

EXIT_OK, EXIT_ERROR, EXIT_REFUSED = 0, 1, 2

#: Committed pages the filter vocabulary can be rebuilt from with no
#: network at all, so a fresh clone is not blind until its first sweep.
SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"


def emit(data: dict, as_json: bool, human) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        human(data)


def _money(value) -> str:
    return f"{value:,} TL" if isinstance(value, int) else "-"


def print_resolve_error(resolved: dict, as_json: bool) -> None:
    """Report a refused filter with the values that would have worked.

    A resolver failure is the one place the tool is most useful out loud: the
    alternative to naming the valid values is a caller retrying blind, or
    worse, dropping the filter and reading a market summary for a query it
    never made.
    """
    if as_json:
        print(json.dumps(resolved, ensure_ascii=False, indent=2), file=sys.stderr)
        return
    print(f"error: {resolved['error']}", file=sys.stderr)
    if resolved.get("did_you_mean"):
        print(f"did you mean: {', '.join(resolved['did_you_mean'])}", file=sys.stderr)
    if resolved.get("options"):
        print(f"valid values: {', '.join(map(str, resolved['options']))}", file=sys.stderr)
    if resolved.get("hint"):
        print(f"hint: {resolved['hint']}", file=sys.stderr)


# --------------------------------------------------------------- commands


def cmd_search(args) -> int:
    """Resolve a query to a sahibinden URL, sweep it, store and summarise."""
    attach = getattr(args, "attach", False)

    if args.url:
        url, resolved, post = args.url, {"url": args.url, "source": "explicit"}, {}
        # The order is whatever the pasted URL asked for, not --sort's default.
        order = (dict(parse_qsl(urlparse(args.url).query)).get("sorting")
                 or "sahibinden's default order")
    else:
        if not args.brand:
            print("error: need --brand (or --url). See: sahibinden brands", file=sys.stderr)
            return EXIT_ERROR

        def resolve():
            return Taxonomy().build_url(
                args.brand, args.model, category=args.category,
                city=args.city, district=args.district,
                min_year=args.min_year, max_year=args.max_year,
                min_km=args.min_km, max_km=args.max_km,
                min_price=args.min_price, max_price=args.max_price,
                currency=args.currency, keyword=args.keyword,
                keyword_in_description=args.keyword_in_description,
                undamaged_only=args.undamaged, sort=args.sort,
                flags={name: getattr(args, name, False) for name in FLAG_FILTERS},
                enums={flag: getattr(args, flag, None) for flag in ENUM_FLAGS},
            )

        resolved = resolve()
        if resolved.get("learn"):
            # The model can only be placed after reading the brand's own page
            # once. That costs a request or two, so it is said out loud.
            learned = learn_models(resolved["learn"], attach=attach)
            if learned.get("error"):
                print(f"refused: could not read {resolved['brand']}'s model list: "
                      f"{learned['error']} {learned.get('reason', '')}", file=sys.stderr)
                return EXIT_ERROR if learned["error"] == "not_found" else EXIT_REFUSED
            resolved = resolve()
        if resolved.get("learn"):
            resolved = {"error": f"could not place model {args.model!r} "
                                 f"under {resolved['brand']}"}
        if resolved.get("error"):
            print_resolve_error(resolved, args.json)
            return EXIT_ERROR
        url, post = resolved["url"], resolved["post_filters"]
        order = args.sort

    result = sweep(url, max_pages=args.pages or config.default_pages,
                   layout=args.layout, attach=attach)
    if result.get("error"):
        if result["error"] == "not_found":
            # A wrong path is the caller's to fix, not a reason to wait.
            print(f"error: sahibinden has no page at {url}\n"
                  "       check --district (it is not validated), or the "
                  "URL you pasted", file=sys.stderr)
            return EXIT_ERROR
        print(f"refused: {result['error']} {result.get('reason','')}", file=sys.stderr)
        return EXIT_REFUSED

    matched = apply_post_filters(result["listings"], post)

    # When the search is city-scoped, sahibinden drops the province from the
    # location column and shows district + neighbourhood instead. Stored as-is
    # that puts "Çankaya" in the city column, and a later `price --city Ankara`
    # would silently miss every row it just collected. The province is known
    # from the query, so restore it and shift the parsed pair down one level.
    scoped_city = (resolved or {}).get("city")
    if scoped_city:
        for row in matched:
            row["neighbourhood"] = row.get("district")
            row["district"] = row.get("city")
            row["city"] = scoped_city

    result["listings"] = matched
    result["post_filters_applied"] = post
    result["resolved"] = resolved

    stored = None
    if not args.no_store:
        with Store(config.db_path) as store:
            stored = store.record_sweep(result)
    result["stored"] = stored

    prices = [x["price_try"] for x in matched if x["price_try"]]
    # Sellers may price in USD, EUR or GBP. Those rows are counted but kept out
    # of the lira statistics rather than converted at a rate we do not have —
    # and said out loud, so a median is never quietly a median of a subset.
    other_currencies: dict[str, int] = {}
    for row in matched:
        unit = row.get("price_currency")
        if unit and unit != "TL":
            other_currencies[unit] = other_currencies.get(unit, 0) + 1
    result["summary"] = {
        "matched": len(matched),
        "priced_in_lira": len(prices),
        "other_currencies": other_currencies,
        "min_price": min(prices) if prices else None,
        "max_price": max(prices) if prices else None,
        "median_price": int(statistics.median(prices)) if prices else None,
    }

    def human(d):
        r = d["resolved"]
        print(f"\n  url            : {r['url']}")
        if r.get("category_label"):
            print(f"  category       : {r['category_label']}")
        if r.get("other_categories"):
            print(f"  NOTE           : {r['brand']} is also listed under "
                  f"{', '.join(r['other_categories'])} — a separate search "
                  f"(--category)")
        if r.get("model_is_guess"):
            print(f"  model slug     : guessed from {r['model']!r}; "
                  f"{'confirmed by results' if d['total_results'] else 'NO RESULTS — check spelling'}")
        if r.get("district_is_guess"):
            print(f"  district slug  : guessed from {r['district']!r}; "
                  f"{'confirmed by results' if d['total_results'] else 'NO RESULTS — check spelling'}")
        print(f"  total on site  : {d['total_results']}")
        print(f"  pages fetched  : {d['pages_fetched']} of {d['pages_available']}")
        if (d.get("pages_available") or 0) > (d.get("pages_fetched") or 0):
            seen = (d["pages_fetched"] or 0) * 20
            print(f"  NOTE           : partial sweep — these are the first ~{seen} "
                  f"of {d['total_results']} by {order}, not the whole market.")
            print(f"                   Narrow with --min-price/--max-price/"
                  f"--min-year/--max-km/--city, or raise --pages.")
        for group, labels in (r.get("filters_applied") or {}).items():
            shown = ", ".join(labels) if isinstance(labels, list) else labels
            print(f"  {group:<15}: {shown}")
        if r.get("flags_applied"):
            print(f"  {'flags':<15}: {', '.join(r['flags_applied'])}")
        if r.get("currency"):
            print(f"  {'price unit':<15}: {r['currency']} "
                  f"(--min-price/--max-price are read in this currency)")
        if r.get("keyword"):
            print(f"  {'keyword':<15}: {r['keyword']}")
        for param in r.get("unverified_flags") or []:
            print(f"  NOTE           : {param} is not in the harvested filter "
                  f"list for this category; run 'sahibinden-cli harvest' if the "
                  f"result looks unfiltered.")
        if d["post_filters_applied"]:
            print(f"  post-filters   : {d['post_filters_applied']} "
                  f"(applied to rows; sahibinden cannot filter these in-URL)")
        s = d["summary"]
        print(f"  matched        : {s['matched']}")
        if s["median_price"]:
            print(f"  price range    : {_money(s['min_price'])} - {_money(s['max_price'])}")
            print(f"  median         : {_money(s['median_price'])}")
        if s.get("other_currencies"):
            listed = ", ".join(f"{n} in {unit}" for unit, n in s["other_currencies"].items())
            print(f"  NOTE           : {listed} — priced by the seller in that "
                  f"currency, so out of the lira figures above.")
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
            # Foreign-currency rows keep their own unit rather than being shown
            # as 0 (price_try) or silently as lira.
            shown = (f"{x['price_try']:,}" if x.get("price_try") is not None
                     else (x.get("price_raw") or "-"))
            print(f"  {x['listing_id']:<12}{a.get('year',''):<6}{a.get('km',''):<10}"
                  f"{shown:>12}  {x.get('city') or ''} {x.get('district') or ''}")
        if len(d["listings"]) > args.limit:
            print(f"  ... {len(d['listings']) - args.limit} more (use --limit)")

    emit(result, args.json, human)
    return EXIT_OK


def detail_command(args) -> str:
    """The `details` call that would fill the diagrams this scope is missing.

    Printed rather than run: detail pages cost one request each and are
    separately capped, so widening what gets fetched stays the caller's call.
    """
    parts = ["sahibinden-cli details"]
    for flag, value in (("--brand", args.brand), ("--model", args.model),
                        ("--min-year", args.min_year),
                        ("--max-km", args.max_km)):
        if value:
            parts.append(f"{flag} {value}")
    if len(parts) == 1:
        return ("sahibinden-cli details --brand <brand> --model <model>"
                "   # scope it, or a day of detail budget goes to the "
                "cheapest cars in the database")
    return " ".join(parts)


def cmd_panels(args) -> int:
    """The 13-part diagram: what can be filtered, and how much is known.

    Coverage is the first question, not an afterthought: a panel filter can
    only speak for listings whose detail page has been fetched, and this is
    where that ratio is visible before a query is built on it.
    """
    with Store(config.db_path) as store:
        rows = store.db.execute(
            "SELECT l.listing_id, d.damage FROM listings l"
            " LEFT JOIN listing_details d ON d.listing_id = l.listing_id"
            " WHERE l.delisted_at IS NULL"
        ).fetchall()

    per_panel = {panel: {status: 0 for status in STATUSES} for panel in PANELS}
    with_diagram = 0
    for row in rows:
        try:
            parts = json.loads(row["damage"]) if row["damage"] else None
        except (TypeError, ValueError):
            parts = None
        if not parts:
            continue
        with_diagram += 1
        for panel, status in parts.items():
            if panel in per_panel and status in per_panel[panel]:
                per_panel[panel][status] += 1

    data = {
        "panels": [{"name": panel, "label": PANEL_LABELS.get(panel),
                    "counts": per_panel[panel]} for panel in PANELS],
        "groups": {name: members for name, members in PANEL_GROUPS.items()},
        "statuses": list(STATUSES),
        "coverage": {"active_listings": len(rows), "with_diagram": with_diagram,
                     "without_diagram": len(rows) - with_diagram},
    }

    def human(d):
        c = d["coverage"]
        print(f"\n  diagrams stored: {c['with_diagram']} of {c['active_listings']} "
              f"active listings ({c['without_diagram']} not fetched yet)")
        print("  a panel filter can only speak for the fetched ones; the rest "
              "come back\n  counted as unchecked, never as clean.\n")
        print(f"  {'panel':<20}{'':<16}{'orij':>6}{'lokal':>7}{'boya':>6}"
              f"{'değişen':>9}{'?':>4}")
        print("  " + "-" * 68)
        for panel in d["panels"]:
            n = panel["counts"]
            print(f"  {panel['name']:<20}{panel['label'] or '':<16}"
                  f"{n['original']:>6}{n['local_painted']:>7}{n['painted']:>6}"
                  f"{n['changed']:>9}{n['unknown']:>4}")
        print("\n  groups (one argument instead of many)")
        collapsed: dict[tuple, list[str]] = {}
        for name, members in d["groups"].items():
            collapsed.setdefault(tuple(members), []).append(name)
        for members, names in collapsed.items():
            label = " / ".join(names)
            print(f"    {label:<22}{len(members):>3} panels: "
                  f"{', '.join(members[:4])}{' ...' if len(members) > 4 else ''}")
        print("\n  statuses: " + ", ".join(d["statuses"]))
        print("\n  examples")
        print("    sahibinden-cli price --brand VW --model Passat --original hood")
        print("    sahibinden-cli price --brand VW --original structural "
              "--not-changed bumpers")
        print("    sahibinden-cli price --brand VW --panel roof=original "
              "--panel front_left_door=original,local_painted")
        print("    sahibinden-cli price --brand VW --max-painted 2 --max-changed 0\n")

    emit(data, args.json, human)
    return EXIT_OK


SESSION_HELP = ("the browser session has expired — run "
                "`sahibinden-cli login` and sign in when Chrome opens")


def session_note(last: dict | None) -> tuple[str, str | None]:
    """Read the last verdict as a sentence about the session, plus a fix.

    Only LOGIN_WALL is a session problem. A trip or a markup change is
    something else entirely, and saying "log in again" about either would send
    the user off to fix the wrong thing.
    """
    if not last:
        return "unknown — no request recorded yet", None
    verdict, when = last["verdict"], last.get("when", "?")
    if verdict == "login_wall":
        return f"EXPIRED — last request was walled at {when}", SESSION_HELP
    if verdict == "ok":
        return f"signed in (last request ok at {when})", None
    return (f"last request was {verdict} at {when} ({last.get('reason', '')})",
            None)


def cmd_login(args) -> int:
    """Hand the keyboard to the user, then verify the session they made."""
    from .fetch.session import sign_in

    lines: list[str] = []

    def log(text=""):
        lines.append(str(text))
        if not args.json:
            print(text, flush=True)

    result = sign_in(timeout=args.timeout * 60, log=log,
                     attach=getattr(args, "attach", False))
    if args.json:
        emit({**result, "log": lines}, True, lambda d: None)
    if result["ok"]:
        return EXIT_OK
    # A refusal is the breaker talking, not a failed login; keep them distinct
    # so a caller does not retry into a cooldown.
    return EXIT_REFUSED if result["stage"] == "refused" else EXIT_ERROR


def cmd_export_session(args) -> int:
    """Dump the signed-in browser's cookies so another machine can use them."""
    from .fetch.portable import export_session

    lines: list[str] = []

    def log(text=""):
        lines.append(str(text))
        if not args.json:
            print(text, flush=True)

    try:
        result = export_session(args.out, attach=getattr(args, "attach", False),
                                log=log)
    except RuntimeError as exc:
        # connect() raises this with the full "start Chrome like so" recipe.
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR

    if args.json:
        emit({**result, "log": lines}, True, lambda d: None)
    elif result["ok"]:
        print("\nCopy it to the other machine, then run 'import-session' there.")
    return EXIT_OK if result["ok"] else EXIT_ERROR


def cmd_import_session(args) -> int:
    """Install a session exported from the machine that did the signing in."""
    from .fetch.portable import import_session

    lines: list[str] = []

    def log(text=""):
        lines.append(str(text))
        if not args.json:
            print(text, flush=True)

    result = import_session(args.file, log=log)
    if args.json:
        emit({**result, "log": lines}, True, lambda d: None)
    elif not result["ok"]:
        print(f"could not import: {result['reason']}", file=sys.stderr)
    return EXIT_OK if result["ok"] else EXIT_ERROR


def cmd_status(args) -> int:
    breaker = Breaker(BREAKER_PATH)
    with Store(config.db_path) as store:
        stats = store.stats()
        inventory = store.inventory() if args.verbose else None
    session, session_fix = session_note(breaker.last_verdict)
    data = {
        "session": session,
        "session_fix": session_fix,
        "last_verdict": breaker.last_verdict,
        "budget_remaining": breaker.remaining,
        "daily_budget": breaker.daily_limit,
        "cooling_down": breaker.cooling_down,
        "breaker": breaker.status(),
        "delay_multiplier": round(breaker.delay_multiplier, 2),
        "in_quiet_hours": config.in_quiet_hours(),
        "config": config.describe(),
        "config_path": str(CONFIG_PATH),
        "database": stats,
        "inventory": inventory,
    }

    def human(d):
        print(f"  session    : {d['session']}")
        if d["session_fix"]:
            print(f"               {d['session_fix']}")
        print(f"  breaker    : {d['breaker']}")
        print(f"  quiet hours: {'yes' if d['in_quiet_hours'] else 'no'} "
              f"(safest time to sweep — an IP ban then costs you nothing)")
        print(f"  config     : {d['config']}")
        print("  database   : " + ", ".join(f"{k}={v}" for k, v in d["database"].items()))
        inv = d.get("inventory")
        if not inv:
            print("\n  (run with --verbose to see what is recorded)")
            return
        print(f"  collecting : {inv['collecting_since']} -> {inv['last_updated']}")
        print("\n  recorded by model")
        print(f"  {'brand':<12}{'model':<10}{'rows':>6}{'active':>8}{'damage':>8}"
              f"{'cities':>8}  price range")
        print("  " + "-" * 74)
        for g in inv["by_model"]:
            rng = (f"{g['min_price']:,} - {g['max_price']:,} TL"
                   if g["min_price"] else "-")
            print(f"  {g['brand'] or '?':<12}{g['model'] or '?':<10}{g['listings']:>6}"
                  f"{g['active']:>8}{g['with_damage']:>8}{g['cities']:>8}  {rng}")
        print("\n  tracked searches (re-swept by `nightly`)")
        for s in inv["tracked_searches"]:
            depth = (f"{s['deepest_pages']}/{s['pages_available']} pages"
                     if s["pages_available"] else "?")
            state = "complete" if s["ever_complete"] else "PARTIAL"
            print(f"  - {state:<9} {depth:<14} {s['sweeps']} sweep(s), "
                  f"site has {s['site_total'] or '?'}")
            print(f"    {s['base_url'][:96]}")
        partial = [s for s in inv["tracked_searches"] if not s["ever_complete"]]
        if partial:
            print(f"\n  {len(partial)} search(es) never swept completely — "
                  "delisting and days-to-sell stay empty until one is.")

    emit(data, args.json, human)
    return EXIT_REFUSED if breaker.cooling_down else EXIT_OK


def cmd_brands(args) -> int:
    tax = Taxonomy()
    known = tax.brands
    names = sorted(known)
    if args.filter:
        needle = args.filter.lower()
        names = [n for n in names if needle in n.lower()]
    data = {"count": len(names), "brands": names,
            "categories": {n: [CATEGORY_NAMES.get(k, k) for k in known[n]]
                           for n in names}}
    emit(data, args.json, lambda d: print("\n".join(
        f"  {n:<22}{', '.join(d['categories'][n])}" for n in d["brands"])))
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
    scope = {k: getattr(args, k, None) for k in
             ("max_price", "min_price", "brand", "model", "min_year", "max_km")}
    result = fetch_details(args.listing_id or None, limit=args.limit,
                           attach=getattr(args, "attach", False),
                           **{k: v for k, v in scope.items() if v is not None})
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
    # Resolve through the same matcher `search` uses. Without this, `search
    # --brand merc` would work while `price --brand merc` silently returned
    # nothing, since the stored value is the canonical "Mercedes-Benz".
    tax = Taxonomy()
    brand = args.brand
    if brand:
        resolved, ambiguous = tax.match_brand(brand, detail=True)
        if resolved is None:
            print(f"error: {'ambiguous' if ambiguous else 'unknown'} brand {brand!r}"
                  + (f" — did you mean: {', '.join(ambiguous[:6])}" if ambiguous else ""),
                  file=sys.stderr)
            return EXIT_ERROR
        brand = resolved

    city = args.city
    if city:
        resolved_city, ambiguous_city = tax.match_city(city, detail=True)
        if resolved_city is None:
            print(f"error: {'ambiguous' if ambiguous_city else 'unknown'} city {city!r}"
                  + (f" — did you mean: {', '.join(ambiguous_city[:6])}" if ambiguous_city else ""),
                  file=sys.stderr)
            return EXIT_ERROR
        city = resolved_city

    # Per-panel requirements are resolved before touching the database, so a
    # typo in a panel name is an error rather than a market summary for a
    # filter that quietly did nothing.
    requirements, error = build_requirements(
        panel=args.panel, original=args.original,
        not_changed=args.not_changed, not_painted=args.not_painted)
    if error:
        print_resolve_error(error, args.json)
        return EXIT_ERROR

    with Store(config.db_path) as store:
        data = store.market(
            brand=brand, model=args.model,
            min_year=args.min_year, max_year=args.max_year,
            min_km=args.min_km, max_km=args.max_km,
            city=city, district=args.district,
            exclude_damaged=args.clean_only,
            include_delisted=args.include_delisted,
            panels=requirements, max_painted=args.max_painted,
            max_changed=args.max_changed, limit=args.limit,
        )
    data["panel_requirements"] = describe(requirements)
    data["query"] = {k: v for k, v in vars(args).items()
                     if k not in ("func", "json", "command") and v}

    def human(d):
        pf = d.get("panel_filter")
        for line in d.get("panel_requirements") or []:
            print(f"  panel        : {line}")
        if pf and (pf["max_painted"] is not None or pf["max_changed"] is not None):
            caps = [f"at most {pf[k]} {k.split('_')[1]}"
                    for k in ("max_painted", "max_changed") if pf[k] is not None]
            print(f"  panel        : {', '.join(caps)} across the whole car")
        if not d["matched"]:
            if pf and pf["unchecked"]:
                # Nothing passed, but the diagram was missing for most of the
                # scope — reporting "no matches" alone would read as "no such
                # car exists", which is a different and much stronger claim.
                print(f"  no match among the {pf['passed'] + pf['failed']} cars whose "
                      f"diagram is stored ({pf['failed']} failed the panel test).")
                print(f"  {pf['unchecked']} more match everything else but have no "
                      f"diagram yet:")
                print(f"    {detail_command(args)}")
                return
            print("  no stored listings match. Run a search first:")
            print("    sahibinden-cli search --brand <brand> --model <model>")
            return
        p_ = d["price"]
        print(f"\n  matched      : {d['matched']} listings")
        if d.get("priced_in_other_currency"):
            listed = ", ".join(f"{n} in {unit}"
                               for unit, n in d["priced_in_other_currency"].items())
            print(f"  not counted  : {listed} — seller priced them in that "
                  f"currency, so they are outside these lira figures")
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
        if pf:
            print(f"  panel test   : {pf['passed']} passed, {pf['failed']} failed, "
                  f"{pf['unchecked']} unchecked")
            if pf["unchecked"]:
                print(f"                 the unchecked ones match everything else "
                      f"asked for; their diagram is simply not fetched yet:")
                print(f"                   {detail_command(args)}")
        t_ = d["turnover"]
        if t_["avg_days_to_delist"]:
            print(f"  turnover     : {t_['delisted_observed']} delisted, "
                  f"avg {t_['avg_days_to_delist']} days listed")
        else:
            print(f"  turnover     : not enough history yet "
                  f"(needs repeated complete sweeps)")
        print("\n  cheapest matches:")
        print(f"  {'id':<12}{'year':<6}{'km':>10}{'price':>12}  {'hasar':<7}"
              f"{'panels':<26}city")
        print("  " + "-" * 88)
        for c in d["cheapest"]:
            flag = c["heavy_damage_record"] or "?"
            print(f"  {c['listing_id']:<12}{c['year'] or '':<6}{c['km'] or 0:>10,}"
                  f"{c['price_try']:>12,}  {flag:<7}"
                  f"{c.get('panels') or '?':<26}{c['city'] or ''}")

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
        # Oldest-swept first, so a capped run rotates through everything over
        # successive nights instead of always refreshing the same few.
        # Without a cap this job grows without bound: every ad-hoc `search`
        # adds a search to it permanently, and at 5 pages each, thirty of them
        # would want 150 requests a night.
        rows = store.db.execute(
            "SELECT base_url, MAX(started_at) AS last FROM sweep_runs"
            " GROUP BY base_url ORDER BY last"
        ).fetchall()
        if args.only:
            needle = args.only.lower()
            rows = [r for r in rows if needle in r["base_url"].lower()]
        tracked_total = len(rows)
        searches = [r["base_url"] for r in rows][:args.max_searches]

    if not searches:
        print("nothing tracked yet — run a search first", file=sys.stderr)
        return EXIT_ERROR

    breaker = Breaker(BREAKER_PATH)
    report = {"started_budget": breaker.remaining, "tracked_total": tracked_total,
              "selected": len(searches), "searches": [], "details": None}

    for url in searches:
        if Breaker(BREAKER_PATH).remaining <= config.detail_budget:
            report["searches"].append({"url": url, "skipped": "budget reserved for details"})
            continue
        # Same page cap as `search`. Sweeping to completion by default would
        # spend hundreds of requests on a broad search; narrowing is the
        # caller's job, exactly as it is on the website itself.
        result = sweep(url, max_pages=args.pages or config.default_pages,
                       attach=getattr(args, "attach", False))
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
        report["details"] = fetch_details(limit=config.detail_budget,
                                          attach=getattr(args, "attach", False))

    report["ending_budget"] = Breaker(BREAKER_PATH).remaining

    def human(d):
        print(f"  budget {d['started_budget']} -> {d['ending_budget']}")
        if d["selected"] < d["tracked_total"]:
            print(f"  refreshing {d['selected']} of {d['tracked_total']} tracked "
                  f"searches (oldest first; the rest rotate in on later runs)")
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



def cmd_filters(args) -> int:
    """List every filter harvested from sahibinden's own search form."""
    tax = Taxonomy()
    keys, error = tax.resolve_category(args.category or "car")
    if error:
        print_resolve_error(error, args.json)
        return EXIT_ERROR
    key = keys[0]
    cat = tax.categories[key]
    groups = {}
    for flag in ENUM_FLAGS:
        group = tax.enum_group(flag, key)
        spec = cat["enums"].get(group)
        if spec:
            groups[flag] = {"group": group, "param": spec["param"],
                            "multi": bool(spec.get("multi")),
                            "options": sorted(spec["options"])}
    flags = {}
    for name, param in FLAG_FILTERS.items():
        label = tax.flag_label(param, key)
        flags[name] = {"param": param, "label": label, "harvested": label is not None}
    data = {"category": key, "category_label": tax.category_label(key),
            "ranges": cat["filters"], "enums": groups, "flags": flags,
            "units": cat["units"],
            "text": {"keyword": "--keyword (query_text)",
                     "keyword_in_description": "--keyword-in-description (query_desc)"},
            "location": {"city": "il — matched against sahibinden's own list",
                         "district": "ilce — slug passed through, verified by "
                                     "whether the search returns anything"}}

    def human(d):
        others = [CATEGORY_NAMES.get(k, k) for k in tax.resolve_category(None)[0]
                  if k != d["category"]]
        print(f"\n  {d['category_label']} — the ids differ per category"
              + (f"; see also --category {' / '.join(others)}" if others else ""))
        print("\n  numeric ranges")
        for label, spec in d["ranges"].items():
            bounds = (f"  ({spec['min']}-{spec['max']})"
                      if spec.get("min") or spec.get("max") else "")
            print(f"    {label:<10} {spec['param']}_min / {spec['param']}_max{bounds}")
        units = d["units"].get("price_currency")
        if units:
            print(f"    {'currency':<10} --currency {'/'.join(sorted(units))}"
                  f"  (reads --min-price/--max-price in that unit)")
        print("\n  choices")
        for flag, spec in d["enums"].items():
            opts = ", ".join(spec["options"][:6])
            more = f"  (+{len(spec['options'])-6} more)" if len(spec["options"]) > 6 else ""
            takes = "repeatable" if spec["multi"] else "one value"
            print(f"    --{flag.replace('_','-'):<15} {spec['group']}  [{takes}]")
            print(f"      {opts}{more}")
        print("\n  switches")
        for name, spec in d["flags"].items():
            note = spec["label"] or f"{spec['param']} (not in the harvest yet)"
            print(f"    --{name.replace('_','-'):<15} {note}")
        print(f"    {'--undamaged':<17} both paint switches at once")
        print("\n  location")
        for name, note in d["location"].items():
            print(f"    --{name:<15} {note}")
        print("\n  free text")
        print(f"    {'--keyword TEXT':<17} Kelime ile filtrele")
        print("    --keyword-in-description")
        print(f"    {'':<17} also match --keyword inside ad descriptions")

    emit(data, args.json, human)
    return EXIT_OK


def cmd_harvest(args) -> int:
    """Rebuild the filter vocabulary from saved HTML. No network, no budget.

    Every filter the CLI can apply is read out of sahibinden's own sidebar
    rather than written down here: the attribute ids are category-specific and
    opaque, and an id that belongs to a different attribute still returns HTTP
    200 and a filtered-looking page. So a wrong guess produces confidently
    wrong numbers instead of an error, and guessing is never worth it.

    Search pages are the source: each is filed under the category its
    sidebar names, since the ids differ per category. A detail page adds the
    province list. Searching does this as it goes; this command re-reads pages
    already on disk.
    """
    tax = Taxonomy()
    sources = [Path(x) for x in (args.path or [])] or [SAMPLES_DIR, RAW_DIR]

    files: list[Path] = []
    for source in sources:
        if source.is_dir():
            files.extend(sorted(source.glob("*.html")))
        elif source.exists():
            files.append(source)

    totals = {"cities": 0, "brands": 0, "models": 0, "filters": 0,
              "enums": 0, "flags": 0, "units": 0}
    scanned, failed = 0, []
    for path in files:
        try:
            found = Taxonomy.extract(path.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:
            failed.append({"file": str(path), "error": f"{type(exc).__name__}: {exc}"})
            continue
        for key, count in tax.merge(found).items():
            totals[key] += count
        scanned += 1

    if not args.dry_run:
        tax.save()

    data = {
        "scanned": scanned,
        "sources": [str(x) for x in sources],
        "added": totals,
        "failed": failed,
        "saved_to": None if args.dry_run else str(tax.path),
        "vocabulary": {
            "cities": len(tax.cities),
            "categories": {
                key: {
                    "label": cat["label"],
                    "brands": len(cat["brands"]),
                    "model_lists": sorted(b for b, e in cat["models"].items()
                                          if e.get("complete")),
                    "ranges": {label: spec["param"]
                               for label, spec in sorted(cat["filters"].items())},
                    "enum_groups": {g: {"param": spec["param"],
                                        "values": len(spec["options"]),
                                        "multi": bool(spec.get("multi"))}
                                    for g, spec in sorted(cat["enums"].items())},
                    "flag_groups": cat["flags"],
                    "units": cat["units"],
                }
                for key, cat in tax.categories.items()
            },
        },
    }

    def human(d):
        print(f"\n  scanned {d['scanned']} page(s) from "
              f"{', '.join(d['sources'])}")
        print("  added      : " + ", ".join(f"{k}={v}" for k, v in d["added"].items()))
        v = d["vocabulary"]
        print(f"  cities {v['cities']}")
        for key, c in v["categories"].items():
            print(f"\n  {c['label']} ({key}): {c['brands']} brands, model lists "
                  f"for {', '.join(c['model_lists']) or 'none yet'}")
            print("    ranges   " + ", ".join(f"{k} {p}" for k, p in c["ranges"].items()))
            for group, spec in c["enum_groups"].items():
                print(f"    {group:<24}{spec['param']:<10}{spec['values']:>3} values"
                      f"   {'checkbox (repeatable)' if spec['multi'] else 'single choice'}")
            for group, options in c["flag_groups"].items():
                print(f"    {group:<24}{'switches':<10}"
                      f"   {', '.join(f'{k} -> {p}' for k, p in options.items())}")
            for param, options in c["units"].items():
                print(f"    {param:<24}{'units':<10}   {', '.join(sorted(options))}")
        for f in d["failed"]:
            print(f"  FAILED {f['file']}: {f['error']}")
        if d["saved_to"]:
            print(f"\n  saved to {d['saved_to']}\n")
        else:
            print("\n  dry run — nothing written\n")

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
            "Filters are applied in the URL using ids harvested from "
            "sahibinden's own sidebar ('harvest'); anything not harvested for "
            "the current category falls back to filtering the parsed rows and "
            "is reported as post_filters, so a caller never silently gets a "
            "wider market than it asked for.",
            "Cars, SUVs and minivans are separate sahibinden categories with "
            "separate filter ids. 'search --model' places a model in its "
            "category by reading the brand's model list (one request per "
            "category, once per brand); --category picks one explicitly.",
            "Per-panel damage filters (--original, --not-changed, --panel) "
            "read the detail-page diagram, so they only answer for listings "
            "whose details have been fetched. Cars without one are reported as "
            "unchecked, never as passing or failing; 'panels' shows coverage.",
            "Choice filters that sahibinden draws as checkboxes are repeatable "
            "(--fuel diesel --fuel hybrid); the ones it draws as radios "
            "(--seller, --listed-within) refuse a second value. 'filters' says "
            "which is which.",
            "Requests are budgeted per day and paced; a refused call means wait.",
            "Every fetch needs a signed-in browser profile. When the session "
            "expires the site returns a sign-in wall to everything, the run "
            "ends itself, and 'status' reports session EXPIRED; 'login' opens "
            "a window for a human to sign in and cannot be automated.",
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
    shared.add_argument("--attach", action="store_true", default=argparse.SUPPRESS,
                        help="drive an already-running Chrome over CDP at "
                             "127.0.0.1:9222 instead of launching our own "
                             "profile — the requests then originate from that "
                             "browser's machine and IP")
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
    p.add_argument("--model", help="e.g. Golf, Tiguan. Matched against the brand's "
                                   "own model list, which is fetched once per brand")
    p.add_argument("--category", help="car, suv or minivan. Optional: a model is "
                                      "placed in its own category automatically; "
                                      "a brand on its own defaults to car")
    p.add_argument("--city", help="il — e.g. Ankara (see 'cities')")
    p.add_argument("--district", help="ilce — e.g. Cankaya; needs --city. Slug is "
                                      "guessed, verified by results")
    p.add_argument("--url", help="explicit sahibinden search URL, bypassing the resolver")
    p.add_argument("--min-year", type=int, help="applied in the URL")
    p.add_argument("--max-year", type=int, help="applied in the URL")
    p.add_argument("--min-km", type=int, help="applied in the URL")
    p.add_argument("--max-km", type=int, help="applied in the URL")
    p.add_argument("--min-price", type=int, help="applied in the URL")
    p.add_argument("--max-price", type=int, help="applied in the URL")
    p.add_argument("--currency", help="TL/USD/EUR/GBP — the unit --min-price and "
                                      "--max-price are read in (default TL)")
    # Repeatable, because sahibinden's own sidebar draws most of these as
    # checkbox lists: --fuel diesel --fuel hybrid is two boxes ticked, one
    # search. The groups it draws as radios (Kimden, İlan Tarihi) reject a
    # second value rather than quietly sending two.
    p.add_argument("--fuel", dest="fuel", action="append", metavar="VALUE",
                   help="Benzin/Dizel/hybrid (repeatable)")
    p.add_argument("--transmission", dest="transmission", action="append",
                   metavar="VALUE", help="automatic/manual (repeatable)")
    p.add_argument("--condition", dest="condition", action="append",
                   metavar="VALUE", help="used/new/imported (repeatable)")
    p.add_argument("--body", dest="body", action="append", metavar="VALUE",
                   help="sedan/hatchback-5-kapi/... (repeatable)")
    p.add_argument("--power", dest="power", action="append", metavar="VALUE",
                   help="HP band, e.g. '101 - 125 HP' (repeatable)")
    p.add_argument("--engine", dest="engine", action="append", metavar="VALUE",
                   help="cm3 band (repeatable)")
    p.add_argument("--drive", dest="drive", action="append", metavar="VALUE",
                   help="fwd/rwd/4WD (repeatable)")
    p.add_argument("--color", dest="color", action="append", metavar="VALUE",
                   help="white/black/... (repeatable)")
    p.add_argument("--warranty", dest="warranty", action="append", metavar="VALUE",
                   help="yes/no (repeatable)")
    p.add_argument("--heavy-damage", dest="heavy_damage", action="append",
                   metavar="VALUE", help="yes/no — agir hasar kayitli (repeatable)")
    p.add_argument("--plate", dest="plate", action="append", metavar="VALUE",
                   help="plate nationality (repeatable)")
    p.add_argument("--seller", dest="seller", action="append", metavar="VALUE",
                   help="owner/dealer/authorised (one value)")
    p.add_argument("--tradeable", dest="tradeable", action="append", metavar="VALUE",
                   help="yes/no (repeatable)")
    p.add_argument("--listed-within", dest="listed_within", action="append",
                   metavar="VALUE", help="e.g. 'Son 3 gun icinde' (one value)")
    p.add_argument("--undamaged", action="store_true",
                   help="both paint switches: unpainted and unchanged")
    p.add_argument("--unpainted", action="store_true",
                   help="Boyasiz — no painted panel")
    p.add_argument("--unchanged", action="store_true",
                   help="Degisensiz — no replaced panel")
    p.add_argument("--has-video", action="store_true", help="Videolu ilanlar")
    p.add_argument("--has-clip", action="store_true", help="Klipli ilanlar")
    p.add_argument("--keyword", help="Kelime ile filtrele — free text over titles")
    p.add_argument("--keyword-in-description", action="store_true",
                   help="also match --keyword inside ad descriptions")
    p.add_argument("--sort", default="price_asc",
                   choices=["price_asc", "price_desc", "date_desc", "date_asc"])
    p.add_argument("--pages", type=int,
                   help="pages to fetch, 20 listings each "
                        "(default: config default_pages, currently 5)")
    p.add_argument("--layout", default="vehicle", choices=["vehicle", "property"],
                   help="column layout of the results table")
    p.add_argument("--limit", type=int, default=20, help="rows to print")
    p.add_argument("--no-store", action="store_true", help="do not write to the database")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("login", parents=[shared],
                       help="sign in to sahibinden in a real browser window",
                       description="Open Chrome on the automation profile and "
                                   "wait while you sign in by hand, then spend "
                                   "one request confirming the session actually "
                                   "clears the sign-in wall. Nothing is typed "
                                   "for you and stdin is never read, because the "
                                   "login is multi-step and any navigation of "
                                   "ours mid-flow cancels it. Use a burner "
                                   "account: bans here are IP-wide, so the "
                                   "account is not what absorbs them.")
    p.add_argument("--timeout", type=int, default=15, metavar="MINUTES",
                   help="how long to wait for you to finish signing in (default 15)")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("export-session", parents=[shared],
                       help="write this browser's sahibinden cookies to a portable file",
                       description="Run this on the machine that is signed in. "
                                   "The browser profile itself does not travel — "
                                   "Chrome encrypts its cookie store with keys "
                                   "the OS holds — but the decrypted cookies do, "
                                   "as plain JSON. Reads an already-open browser, "
                                   "so it opens no page and costs no requests.")
    p.add_argument("--out", default="session.json", metavar="FILE",
                   help="where to write the session (default session.json)")
    p.set_defaults(func=cmd_export_session)

    p = sub.add_parser("import-session", parents=[shared],
                       help="install a session exported from another machine",
                       description="Install cookies exported elsewhere so the "
                                   "fetcher on this host runs as that signed-in "
                                   "session. Validates structure and expiry only; "
                                   "it spends no request, so whether the site "
                                   "still honours the session from this address "
                                   "is settled by the next search, not by this.")
    p.add_argument("file", help="the exported session JSON")
    p.set_defaults(func=cmd_import_session)

    p = sub.add_parser("status", parents=[shared], help="budget, breaker and database state",
                       description="Show remaining request budget, cooldown state "
                                   "and what is in the local database.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="list what is recorded: models, coverage, tracked searches")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("filters", parents=[shared],
                       help="list available search filters and their values",
                       description="Every filter harvested from sahibinden's own "
                                   "search form, with the values each accepts.")
    p.add_argument("--category", help="car (default), suv or minivan")
    p.set_defaults(func=cmd_filters)

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
    p.add_argument("--min-price", type=int, help="only queue listings at or above this")
    p.add_argument("--brand", help="only this brand")
    p.add_argument("--model", help="only this model")
    p.add_argument("--min-year", type=int, help="only cars this year or newer")
    p.add_argument("--max-km", type=int, help="only cars at or below this km")
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
    p.add_argument("--district")
    p.add_argument("--min-year", type=int)
    p.add_argument("--max-year", type=int)
    p.add_argument("--min-km", type=int)
    p.add_argument("--max-km", type=int)
    # Per-panel filters. These read the stored diagram, so they answer for
    # fetched details only — the rest are reported as unchecked, not dropped.
    p.add_argument("--panel", action="append", metavar="PANEL=STATUS",
                   help="e.g. hood=original, roof=original,local_painted "
                        "(repeatable; see 'panels')")
    p.add_argument("--original", action="append", metavar="PANELS",
                   help="panel or group that must be untouched, e.g. hood, "
                        "structural, doors (repeatable)")
    p.add_argument("--not-changed", action="append", metavar="PANELS",
                   help="panel or group that must not be replaced; paint is "
                        "allowed (repeatable)")
    p.add_argument("--not-painted", action="append", metavar="PANELS",
                   help="panel or group with no full repaint; local touch-up "
                        "allowed (repeatable)")
    p.add_argument("--max-painted", type=int,
                   help="most painted panels allowed across the whole car")
    p.add_argument("--max-changed", type=int,
                   help="most replaced panels allowed across the whole car")
    p.add_argument("--limit", type=int, default=10,
                   help="matches to list")
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
                   help="pages per search (default: config default_pages)")
    p.add_argument("--no-details", action="store_true",
                   help="sweeps only, skip detail fetching")
    p.add_argument("--max-searches", type=int, default=4,
                   help="how many tracked searches to refresh, oldest first")
    p.add_argument("--only", help="only searches whose URL contains this text")
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

    p = sub.add_parser("panels", parents=[shared],
                       help="the 13-part damage diagram: names, groups, coverage",
                       description="What the per-panel filters accept, and how "
                                   "many stored listings actually have a diagram "
                                   "to test. Panel status comes from the detail "
                                   "page, so it is only known for listings whose "
                                   "details have been fetched.")
    p.set_defaults(func=cmd_panels)

    p = sub.add_parser("harvest", parents=[shared],
                       help="rebuild the filter vocabulary from saved HTML",
                       description="Read brands, provinces, ranges and every "
                                   "sidebar filter out of saved sahibinden pages "
                                   "and merge them into data/taxonomy.json. No "
                                   "network. Run it after a sweep to pick up "
                                   "filter values that page happened to expose.")
    p.add_argument("path", nargs="*",
                   help="HTML files or directories (default: samples/ and data/raw/)")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be learned without writing")
    p.set_defaults(func=cmd_harvest)

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
