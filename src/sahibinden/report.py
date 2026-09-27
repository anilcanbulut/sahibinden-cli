"""Search results as a Markdown page: what was searched, then one row per car.

The plain table `search` prints is sized for a terminal and gone with the
scrollback. This one is for keeping and clicking through: open it in anything
that renders Markdown and every row links to its listing.

When the search read damage diagrams, the page splits in three: the cars that
passed the body-part check, with what is painted or replaced on each, the ones
that failed and why, and the ones not checked yet. The last are kept apart
rather than dropped or passed, since nothing is known about their panels.
"""

from __future__ import annotations

import datetime as dt

from .panels import PANELS, STATUS_WORDS, panel_name
from .parse.util import parse_turkish_number
from .taxonomy import slugify

ORDER_NAMES = {"price_asc": "cheapest first", "price_desc": "most expensive first",
               "date_desc": "newest first", "date_asc": "oldest first"}


def cell(value) -> str:
    """A value that stays inside its cell: a bare | would start the next one."""
    text = "" if value is None else " ".join(str(value).split())
    return text.replace("\\", "\\\\").replace("|", "\\|")


def span(low: int | None, high: int | None, unit: str = "", *,
         grouped: bool = True) -> str | None:
    """(2018, None) -> 'from 2018'; (None, 150000, 'km') -> 'up to 150,000 km'."""
    if low is None and high is None:
        return None
    number = (lambda n: f"{n:,}") if grouped else str
    suffix = f" {unit}" if unit else ""
    if low is not None and high is not None:
        both = number(low) if low == high else f"{number(low)} – {number(high)}"
        return both + suffix
    if low is not None:
        return f"from {number(low)}{suffix}"
    return f"up to {number(high)}{suffix}"


# ------------------------------------------------------------------ columns
# (heading, alignment, value). Numbers are right-aligned so prices and km line
# up by magnitude.

def _km(x):
    km = parse_turkish_number((x.get("attributes") or {}).get("km"))
    return f"{km:,}" if km is not None else None


def _price(x):
    # A foreign-currency row keeps its own unit rather than being shown as
    # lira, as in the terminal table.
    return f"{x['price_try']:,} TL" if x.get("price_try") is not None else x.get("price_raw")


def _count(*statuses):
    return lambda x: sum(1 for s in (x.get("panels") or {}).values() if s in statuses)


def _heavy_damage(x):
    return {"evet": "YES", "hayir": "no"}.get(slugify(x.get("heavy_damage_record") or ""), "?")


def _not_original(x):
    """'replaced: front left fender · painted: rear bumper, trunk'."""
    parts = x.get("panels") or {}
    groups = []
    for status in ("changed", "painted", "local_painted", "unknown"):
        names = [panel_name(p) for p in PANELS if parts.get(p) == status]
        if names:
            groups.append(f"{STATUS_WORDS[status]}: {', '.join(names)}")
    return " · ".join(groups) or "none"


CAR = [
    ("ID", "---", lambda x: x["listing_id"]),
    ("Title", "---", lambda x: x.get("title")),
    ("Trim", "---", lambda x: x.get("trim")),
    ("Year", "--:", lambda x: (x.get("attributes") or {}).get("year")),
    ("KM", "--:", _km),
    ("Colour", "---", lambda x: (x.get("attributes") or {}).get("color")),
    ("Price", "--:", _price),
    ("Location", "---", lambda x: " ".join(p for p in (x.get("city"), x.get("district")) if p)),
    ("Listed", "---", lambda x: x.get("listed_date") or x.get("listed_date_raw")),
]
BODY = [
    ("Replaced", "--:", _count("changed")),
    ("Painted", "--:", _count("painted")),
    ("Local paint", "--:", _count("local_painted")),
    ("Heavy damage", "---", _heavy_damage),
    ("Not original", "---", _not_original),
]
BRIEF = [c for c in CAR if c[0] in ("ID", "Title", "Year", "KM", "Price", "Location")]


def _table(rows: list[dict], columns: list, limit: int | None) -> list[str]:
    shown = rows if limit is None else rows[:limit]
    lines = ["| # | " + " | ".join(c[0] for c in columns) + " | Link |",
             "|--:|" + "|".join(c[1] for c in columns) + "|---|"]
    for number, x in enumerate(shown, 1):
        link = f"[open]({x['url']})" if x.get("url") else ""
        lines.append(f"| {number} | " + " | ".join(cell(c[2](x)) for c in columns)
                     + f" | {link} |")
    if len(shown) < len(rows):
        lines += ["", f"_{len(rows) - len(shown)} more not shown._"]
    return lines


def _filters(resolved: dict, query) -> list[str]:
    """What the search was narrowed by, in the order the sidebar lists it."""
    parts = []
    place = " ".join(x for x in (resolved.get("city"), resolved.get("district")) if x)
    if place:
        parts.append(f"Location: {place}")
    ranges = (
        ("Year", span(getattr(query, "min_year", None),
                      getattr(query, "max_year", None), grouped=False)),
        ("KM", span(getattr(query, "min_km", None), getattr(query, "max_km", None))),
        ("Price", span(getattr(query, "min_price", None),
                       getattr(query, "max_price", None),
                       resolved.get("currency") or "TL")),
    )
    parts.extend(f"{label}: {text}" for label, text in ranges if text)
    for group, labels in (resolved.get("filters_applied") or {}).items():
        shown = ", ".join(labels) if isinstance(labels, list) else labels
        parts.append(f"{group}: {shown}")
    parts.extend(resolved.get("flags_applied") or [])
    if resolved.get("keyword"):
        parts.append(f"Title has: {resolved['keyword']}")
    return parts


def _body_rules(check: dict) -> list[str]:
    rules = list(check.get("requirements") or [])
    if check.get("max_painted") is not None:
        rules.append(f"at most {check['max_painted']} painted panels")
    if check.get("max_changed") is not None:
        rules.append(f"at most {check['max_changed']} replaced panels")
    return rules


def markdown(result: dict, query, *, order: str, limit: int | None = None,
             when: dt.datetime | None = None) -> str:
    """The whole page for one `search` result."""
    r, s = result["resolved"], result["summary"]
    check = result.get("panel_check")
    when = when or dt.datetime.now()
    name = " ".join(x for x in (r.get("brand"), r.get("model")) if x)
    lines = [f"## {name or 'sahibinden search'}", "",
             f"Searched {when:%d %b %Y %H:%M}, {ORDER_NAMES.get(order, order)}.", ""]

    lines.append(f"- **Search:** [open on sahibinden]({r['url']})")
    if r.get("category_label"):
        lines.append(f"- **Category:** {r['category_label']}")
    filters = _filters(r, query)
    if filters:
        lines.append(f"- **Filters:** {' · '.join(cell(f) for f in filters)}")
    total = result.get("total_results")
    lines.append(f"- **Found:** {total if total is not None else '?'} on the site, "
                 f"{s['matched']} read ({result.get('pages_fetched') or 0} of "
                 f"{result.get('pages_available') or 0} pages)")
    if s.get("median_price"):
        lines.append(f"- **Price:** {s['min_price']:,} – {s['max_price']:,} TL, "
                     f"median {s['median_price']:,} TL")
    if s.get("other_currencies"):
        listed = ", ".join(f"{n} in {unit}" for unit, n in s["other_currencies"].items())
        lines.append(f"- **Not in the price figures:** {listed}, as the seller "
                     "priced them in that currency")
    if check:
        rules = _body_rules(check)
        if rules:
            lines.append(f"- **Body parts:** {' · '.join(cell(x) for x in rules)}")
        read = check["passed"] + check["failed"]
        lines.append(f"- **Damage diagrams:** {read} read ({check['opened_now']} "
                     f"pages opened now, {check['stored_before']} known from "
                     f"earlier searches): {check['passed']} "
                     f"{'passed' if rules else 'shown'}, {check['failed']} did not "
                     f"pass, {check['unchecked']} not checked")
    if (result.get("pages_available") or 0) > (result.get("pages_fetched") or 0):
        seen = (result.get("pages_fetched") or 0) * 20
        lines += ["", f"> These are the first ~{seen} of {total}, "
                      f"{ORDER_NAMES.get(order, order)}, not every listing. Narrow "
                      "the filters or read more pages to see the rest."]
    if check and check.get("stopped"):
        lines += ["", f"> Opening listing pages stopped early: {cell(check['stopped'])}"]
    lines.append("")

    rows = result["listings"]
    if not rows:
        lines.append("_No listings matched._")
        return "\n".join(lines) + "\n"
    if not check:
        lines += _table(rows, CAR, limit)
        return "\n".join(lines) + "\n"

    passed = [x for x in rows if x["panel_verdict"] == "pass"]
    failed = [x for x in rows if x["panel_verdict"] == "fail"]
    unchecked = [x for x in rows if x["panel_verdict"] == "unchecked"]

    heading = "Passed the body-part check" if _body_rules(check) else "Checked"
    lines += [f"### {heading} ({len(passed)})", ""]
    lines += (_table(passed, CAR + BODY, limit) if passed
              else ["_None of the cars checked so far._"])
    if unchecked:
        lines += ["", f"### Not checked yet ({len(unchecked)})", "",
                  "Their damage diagram hasn't been read, so they may or may not "
                  "pass. Run the same search again to open the next ones: "
                  "diagrams already read are kept, so they cost nothing the "
                  "second time.", ""]
        lines += _table(unchecked, BRIEF + [("Why", "---",
                                             lambda x: x.get("unchecked_reason"))], limit)
    if failed:
        lines += ["", f"### Did not pass ({len(failed)})", ""]
        lines += _table(failed, BRIEF + [("Why", "---",
                                          lambda x: ", ".join(x.get("failed_on") or []))],
                        limit)
    return "\n".join(lines) + "\n"
