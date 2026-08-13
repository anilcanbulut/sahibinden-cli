"""Extract a sahibinden.com search results page.

This is the cheap tier: ~20 listings per request, versus one request per car on
the detail pages. A sweep runs here; detail fetches are rationed against it.

The results table has no <thead>, so the middle columns are positional and their
meaning is category-dependent (cars: yıl / km / renk; property: m² / oda / yaş).
We therefore keep every cell verbatim in ``columns`` and layer a named mapping on
top, so a category we have not modelled yet still round-trips its data intact.
"""

from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from .util import parse_turkish_date, parse_turkish_number, text

BASE_URL = "https://www.sahibinden.com"

# Positional meaning of the searchResultsAttributeValue cells, per category.
COLUMN_LAYOUTS = {
    "vehicle": ("year", "km", "color"),
    "property": ("square_meters", "room_count", "building_age"),
}

_TOTAL_RE = re.compile(r"([\d.]+)\s+ilan\s+bulundu")


def _row_kind(classes: list[str]) -> str:
    """Organic, promoted (paid placement but a real listing), or a true ad."""
    joined = " ".join(classes)
    if "nativeAd" in joined:
        return "ad"
    if "searchResultsPromo" in joined:
        return "promoted"
    return "organic"


def parse_row(row, layout: str = "vehicle") -> dict | None:
    listing_id = row.get("data-id")
    if not listing_id:
        return None

    cells = row.select("td")
    columns = [text(td) for td in cells]

    # a.classifiedTitle is the real permalink; the title cell also holds
    # favourite/ignore anchors that are all href="#".
    link = row.select_one("a.classifiedTitle[href]") or row.select_one("a[href*='/ilan/']")
    href = link.get("href") if link else None
    url = f"{BASE_URL}{href}" if href and href.startswith("/") else href

    store = row.select_one("a.store-icon[href], a.titleIcon[href^='http']")

    thumb = row.select_one("td.searchResultsLargeThumbnail img")
    date_raw = text(row.select_one("td.searchResultsDateValue"))
    location_raw = text(row.select_one("td.searchResultsLocationValue"))
    # Location renders as "İstanbul Eyüpsultan" with a <br> between the two.
    location_bits = location_raw.split(" ", 1) if location_raw else []

    attrs = [text(td) for td in row.select("td.searchResultsAttributeValue")]
    named = dict(zip(COLUMN_LAYOUTS.get(layout, ()), attrs))

    return {
        "listing_id": listing_id,
        "url": url,
        "title": text(row.select_one("td.searchResultsTitleValue")),
        "trim": text(row.select_one("td.searchResultsTagAttributeValue")) or None,
        "price_try": parse_turkish_number(text(row.select_one("td.searchResultsPriceValue"))),
        "price_raw": text(row.select_one("td.searchResultsPriceValue")) or None,
        "listed_date": parse_turkish_date(date_raw),
        "listed_date_raw": date_raw or None,
        "city": location_bits[0] if location_bits else None,
        "district": location_bits[1] if len(location_bits) > 1 else None,
        "thumbnail": (thumb.get("data-src") or thumb.get("src")) if thumb else None,
        "store_name": store.get("title") if store else None,
        "store_url": store.get("href") if store else None,
        "placement": _row_kind(row.get("class") or []),
        "attributes": {k: v for k, v in named.items() if v},
        "columns": columns,
    }


def parse_search(html: str, layout: str = "vehicle") -> dict:
    soup = BeautifulSoup(html, "lxml")

    result_text = text(soup.select_one(".result-text"))
    total_match = _TOTAL_RE.search(result_text)
    total = parse_turkish_number(total_match.group(1)) if total_match else None

    raw_rows = soup.select("#searchResultsTable tbody tr")
    listings = [x for x in (parse_row(r, layout) for r in raw_rows) if x]
    # Native ads carry no data-id, so they drop out at parse_row; count them
    # from the source rows instead, otherwise a layout change that silently
    # kills every row would look like "no ads" rather than a parse failure.
    skipped = len(raw_rows) - len(listings)

    pages = [text(a) for a in soup.select(".pageNaviButtons a")]
    page_numbers = [int(p) for p in pages if p.isdigit()]

    return {
        "query_text": result_text or None,
        "total_results": total,
        "layout": layout,
        "listings": [x for x in listings if x["placement"] != "ad"],
        "rows_seen": len(raw_rows),
        "rows_skipped": skipped,
        "max_page_seen": max(page_numbers) if page_numbers else None,
        "has_next": any("Sonraki" in p for p in pages),
    }


if __name__ == "__main__":
    import sys
    from pathlib import Path

    for path in sys.argv[1:]:
        data = parse_search(Path(path).read_text(encoding="utf-8", errors="replace"))
        print(json.dumps(data, ensure_ascii=False, indent=2))
