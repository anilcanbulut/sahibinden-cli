"""Paginated sweep of a sahibinden search — the cheap tier.

One request returns ~20 listings here, versus one listing per request on the
detail pages. Everything the "what is this car worth" question needs (year, km,
colour, price, location, listing date) is already in these rows, so sweeps are
routine and detail fetches stay rationed.

The sweep never decides on its own to spend more than the remaining budget, and
it stops at the first unusable response rather than pushing through.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .config import config
from .fetch.fetcher import BREAKER_PATH, fetch_pages
from .fetch.guard import Breaker, Verdict
from .parse.detail import parse_detail
from .parse.search import parse_search
from .store import Store
from .taxonomy import Taxonomy

#: sahibinden paginates by row offset, not page number.
PAGE_SIZE = 20

#: Their pager stops offering pages past this depth; deeper offsets return the
#: same tail repeatedly, so crawling further burns budget for nothing.


def page_url(base_url: str, offset: int) -> str:
    """Rewrite (or add) pagingOffset on a search URL, preserving its filters."""
    parts = urlparse(base_url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "pagingOffset"]
    if offset:
        query.append(("pagingOffset", str(offset)))
    return urlunparse(parts._replace(query=urlencode(query)))


def plan(base_url: str, total_results: int | None, max_pages: int | None) -> list[str]:
    """URLs to fetch, bounded by the site's own result count and our budget."""
    if total_results:
        needed = min(-(-total_results // PAGE_SIZE), config.max_offset // PAGE_SIZE)
    else:
        needed = max_pages or 1
    if max_pages:
        needed = min(needed, max_pages)
    return [page_url(base_url, i * PAGE_SIZE) for i in range(needed)]


def learn_from(html: str, *, complete: tuple[str, str] | None = None) -> None:
    """Merge whatever a fetched page teaches into the local vocabulary.

    Every search page carries its category tree and its filter ids, so the
    vocabulary keeps itself current as a side effect of searching instead of
    needing a separate harvest. Never allowed to fail a fetch.
    """
    try:
        tax = Taxonomy()
        tax.merge(Taxonomy.extract(html))
        if complete:
            category, brand = complete
            # Only a page that actually listed this brand's models may vouch
            # for the list; an empty one would make every model "unknown".
            if tax.categories.get(category, {}).get("models", {}).get(brand, {}).get("models"):
                tax.mark_models_complete(category, brand)
        tax.save()
    except Exception as exc:  # the listings matter more than the vocabulary
        print(f"  (could not update the filter list: {exc})")


def learn_models(targets: list[dict], *, attach: bool = False) -> dict:
    """Fetch brand pages so a model can be placed in its category.

    One request per category the brand has listings in, once per brand: a
    brand page's sidebar lists every model with its exact path, and the list is
    kept. Much cheaper than the alternative of trying the Otomobil path, taking
    the 404, and trying SUV next — every time.
    """
    brand = targets[0]["brand"]
    print(f"  learning {brand}'s model list — {len(targets)} page(s), "
          f"only needed once", flush=True)
    results = fetch_pages([t["url"] for t in targets], expect="search",
                          attach=attach)
    for target, result in zip(targets, results):
        if not result.ok:
            return {"error": result.assessment.verdict.value,
                    "reason": result.assessment.reason}
        learn_from(result.html, complete=(target["category"], target["brand"]))
    if len(results) < len(targets):
        return {"error": "stopped", "reason": "not every brand page was fetched"}
    return {"ok": True}


def sweep(
    base_url: str,
    *,
    max_pages: int | None = None,
    layout: str = "vehicle",
    daily_limit: int | None = None,
    attach: bool = False,
) -> dict:
    """Crawl a search from page 1, discovering depth from the first response."""
    daily_limit = daily_limit or config.daily_budget
    breaker = Breaker(BREAKER_PATH, daily_limit=daily_limit)
    affordable = breaker.remaining
    if affordable <= 0:
        return {"error": "no budget remaining today", "listings": []}
    if max_pages is None or max_pages > affordable:
        max_pages = affordable

    # Page 1 first: its total_results tells us how deep the search actually
    # goes, so we never guess at page count.
    first = fetch_pages([page_url(base_url, 0)], expect="search",
                        daily_limit=daily_limit, attach=attach)
    if first and first[0].assessment.verdict is Verdict.EMPTY:
        # Nothing matches. That is an answer, not a failure.
        return {
            "base_url": base_url,
            "layout": layout,
            "total_results": 0,
            "pages_fetched": 1,
            "pages_available": 0,
            "listings": [],
            "duplicates_dropped": 0,
            "breaker": Breaker(BREAKER_PATH, daily_limit=daily_limit).status(),
        }
    if not first or not first[0].ok:
        verdict = first[0].assessment if first else None
        return {
            "error": verdict.verdict.value if verdict else "no response",
            "reason": verdict.reason if verdict else "",
            "listings": [],
        }

    learn_from(first[0].html)
    head = parse_search(first[0].html, layout)
    total = head["total_results"]
    urls = plan(base_url, total, max_pages)[1:]

    pages = [head]
    if urls:
        for result in fetch_pages(urls, expect="search", daily_limit=daily_limit,
                                  attach=attach):
            if not result.ok:
                break
            pages.append(parse_search(result.html, layout))

    # Later pages re-list promoted rows, so dedupe on listing_id and keep the
    # first sighting.
    seen: dict[str, dict] = {}
    for page in pages:
        for listing in page["listings"]:
            seen.setdefault(listing["listing_id"], listing)

    return {
        "base_url": base_url,
        "layout": layout,
        "total_results": total,
        "pages_fetched": len(pages),
        "pages_available": -(-total // PAGE_SIZE) if total else None,
        "listings": list(seen.values()),
        "duplicates_dropped": sum(len(p["listings"]) for p in pages) - len(seen),
        # Read fresh: fetch_pages counts on its own Breaker, so the one made
        # above still holds the count from before this sweep.
        "breaker": Breaker(BREAKER_PATH, daily_limit=daily_limit).status(),
    }


def fetch_details(
    listing_ids: list[str] | None = None,
    *,
    limit: int | None = None,
    db_path: str | None = None,
    attach: bool = False,
    **scope,
) -> dict:
    """Fetch detail pages — the expensive tier — and store what they hold.

    A detail page costs one request and returns one listing, versus ~20 for a
    search page. It is also the only place the damage diagram and
    ``agir_hasar_kayitli`` appear, and those two disagree often enough to
    matter: a car can show twelve original panels and still be registered as a
    write-off. So this is rationed separately from the sweep budget rather than
    sharing it, and it never runs unbounded.

    Without explicit ids it works the queue: active listings that have no
    detail record yet, cheapest first.
    """
    store = Store(db_path or config.db_path)
    try:
        cap = min(limit or config.detail_budget, config.detail_budget)
        breaker = Breaker(BREAKER_PATH)
        cap = min(cap, breaker.remaining)
        if cap <= 0:
            return {"error": "no budget remaining today", "fetched": []}

        if listing_ids:
            rows = [
                dict(r) for r in (
                    store.db.execute(
                        "SELECT listing_id, url, last_price_try FROM listings"
                        f" WHERE listing_id IN ({','.join('?' * len(listing_ids))})",
                        listing_ids,
                    ).fetchall()
                )
            ]
            missing = set(listing_ids) - {r["listing_id"] for r in rows}
        else:
            rows = [dict(r) for r in store.needs_detail(cap, **scope)]
            missing = set()

        targets = [r for r in rows if r.get("url")][:cap]
        if not targets:
            return {"error": "nothing to fetch", "fetched": [], "unknown_ids": sorted(missing)}

        results = fetch_pages([r["url"] for r in targets], expect="detail",
                              attach=attach)

        fetched, failures = [], []
        for row, result in zip(targets, results):
            if not result.ok:
                failures.append({"listing_id": row["listing_id"],
                                 "verdict": result.assessment.verdict.value,
                                 "reason": result.assessment.reason})
                continue
            detail = parse_detail(result.html)
            store.record_detail(detail, str(result.raw_path) if result.raw_path else None)
            counts = (detail.get("damage") or {}).get("counts") or {}
            fetched.append({
                "listing_id": detail.get("listing_id") or row["listing_id"],
                "title": detail.get("title"),
                "price_try": detail.get("price_try"),
                "heavy_damage_record": detail.get("heavy_damage_record"),
                "damage_counts": counts,
                # The headline signal: the diagram looks clean but the vehicle
                # carries a registered heavy-damage record.
                "clean_looking_but_flagged": (
                    detail.get("heavy_damage_record") == "evet"
                    and counts.get("changed", 0) == 0
                    and counts.get("painted", 0) <= 1
                ),
            })

        return {
            "requested": len(targets),
            "fetched": fetched,
            "failures": failures,
            "unknown_ids": sorted(missing),
            "breaker": Breaker(BREAKER_PATH).status(),
        }
    finally:
        store.close()
