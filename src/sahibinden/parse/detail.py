"""Extract everything from a sahibinden.com vehicle detail page.

Strategy: the page carries the same facts in several places. We harvest all of
them rather than picking one, because they fail independently:

  - ``dfpTargetingTags``   ad-targeting JS vars; display-formatted, includes the
                           full Eurotax spec sheet (``etx_*``)
  - JSON attribute array   normalized slugs (``km=159000``), best for querying
  - ``classifiedInfoList`` the visible table; human labels, ground truth
  - ``.car-parts``         the 13-part damage diagram

Nothing is dropped or renamed on the way in. Interpretation happens later.
"""

from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

# Diagram part classes, in the DOM order sahibinden emits them.
PART_SLUGS = {
    "front-bumper": "front_bumper",
    "front-hood": "hood",
    "roof": "roof",
    "front-right-mudguard": "front_right_fender",
    "front-right-door": "front_right_door",
    "rear-right-door": "rear_right_door",
    "rear-right-mudguard": "rear_right_fender",
    "front-left-mudguard": "front_left_fender",
    "front-left-door": "front_left_door",
    "rear-left-door": "rear_left_door",
    "rear-left-mudguard": "rear_left_fender",
    "rear-hood": "trunk",
    "rear-bumper": "rear_bumper",
}

# The span text is authoritative; the class is a fallback for when they
# restyle the diagram but keep the markup.
STATUS_BY_SPAN = {"": "original", "LB": "local_painted", "B": "painted", "D": "changed"}
STATUS_BY_CLASS = {
    "original-new": "original",
    "localpainted-new": "local_painted",
    "painted-new": "painted",
    "changed-new": "changed",
}

# dfpTargetingTags come in three shapes: "string", 'string', and ["a","b"].
_DFP_STR_RE = re.compile(r"""dfpTargetingTags\['([^']+)'\]\s*=\s*["']([^"']*)["'];""")
_DFP_ARR_RE = re.compile(r"""dfpTargetingTags\['([^']+)'\]\s*=\s*(\[[^\]]*\]);""")
_ATTR_RE = re.compile(r'\{"name":"([a-z0-9_]+)","value":"([^"]*)"\}')
_CLASSIFIED_ID_RE = re.compile(r'data-classifiedid="(\d+)"')

# Equipment is split across four ad tags; we keep the split, it is meaningful.
EQUIPMENT_TAGS = ("ic_donanim", "dis_donanim", "guvenlik", "multimedya")


def _text(node) -> str:
    """Collapse a node to single-spaced text, dropping &nbsp; and stray tabs."""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True).replace("\xa0", " ")).strip()


def parse_dfp_tags(html: str) -> dict[str, str | list[str]]:
    tags: dict[str, str | list[str]] = {k: v for k, v in _DFP_STR_RE.findall(html)}
    for key, raw in _DFP_ARR_RE.findall(html):
        try:
            tags[key] = json.loads(raw)
        except json.JSONDecodeError:
            tags[key] = raw
    return tags


def parse_location(soup: BeautifulSoup) -> dict:
    """Address trail: il / ilçe / mahalle, plus the quarter id from the href.

    sahibinden marks these with data-click-label="Adres Breadcrumb | ..." which
    is far more stable than positional selectors in the header block.
    """
    parts = [
        _text(a)
        for a in soup.select('a[data-click-label^="Adres Breadcrumb"]')
        if _text(a)
    ]
    quarter_id = None
    for a in soup.select('a[data-click-label^="Adres Breadcrumb"]'):
        m = re.search(r"address_quarter=(\d+)", a.get("href", ""))
        if m:
            quarter_id = m.group(1)
    keys = ("city", "district", "quarter")
    located = dict(zip(keys, parts))
    located["path"] = " / ".join(parts) or None
    located["quarter_id"] = quarter_id
    return located


def parse_attributes(html: str) -> dict[str, str]:
    """Normalized slug attributes. Deduped — the blob repeats across the page."""
    return {k: v for k, v in _ATTR_RE.findall(html)}


def parse_info_list(soup: BeautifulSoup) -> dict[str, str]:
    """The visible <strong>label</strong><span>value</span> table."""
    info: dict[str, str] = {}
    for ul in soup.select("ul.classifiedInfoList"):
        for li in ul.find_all("li", recursive=False):
            label = li.find("strong")
            value = li.find("span")
            if label and value:
                key = _text(label)
                if key:
                    info[key] = _text(value)
    return info


def parse_damage(soup: BeautifulSoup) -> dict | None:
    """The 13-part diagram. Returns None when the section is absent.

    Absent is meaningfully different from all-original: some listings (and most
    non-car vehicles) never render the diagram, and treating that as a clean car
    would be the worst possible failure mode for this dataset.
    """
    container = soup.select_one("div.car-parts")
    if container is None:
        return None

    parts: dict[str, str] = {}
    for div in container.find_all("div", recursive=False):
        classes = div.get("class") or []
        part = next((PART_SLUGS[c] for c in classes if c in PART_SLUGS), None)
        if part is None:
            continue
        span = div.find("span")
        span_text = span.get_text(strip=True) if span else ""
        status = STATUS_BY_SPAN.get(span_text)
        if status is None:
            status = next((STATUS_BY_CLASS[c] for c in classes if c in STATUS_BY_CLASS), "unknown")
        parts[part] = status

    counts = {s: sum(1 for v in parts.values() if v == s) for s in
              ("original", "local_painted", "painted", "changed", "unknown")}
    return {
        "parts": parts,
        "counts": {k: v for k, v in counts.items() if v},
        "parts_found": len(parts),
    }


def parse_description(soup: BeautifulSoup) -> str | None:
    """Seller free text. Carries disclosures the structured fields omit."""
    node = soup.select_one("#classifiedDescription")
    if node is None:
        return None
    for br in node.find_all("br"):
        br.replace_with("\n")
    text = node.get_text("\n", strip=True).replace("\xa0", " ")
    return re.sub(r"\n{3,}", "\n\n", text).strip() or None


def parse_images(soup: BeautifulSoup) -> list[str]:
    urls: list[str] = []
    for img in soup.select("img[data-src], img[src]"):
        url = img.get("data-src") or img.get("src") or ""
        if "/photos/" in url or "large" in url:
            urls.append(url)
    return list(dict.fromkeys(urls))


def parse_detail(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    attributes = parse_attributes(html)
    dfp = parse_dfp_tags(html)
    info = parse_info_list(soup)

    classified_id = None
    node = soup.select_one("[data-classifiedid]")
    if node:
        classified_id = node["data-classifiedid"]
    else:
        m = _CLASSIFIED_ID_RE.search(html)
        classified_id = m.group(1) if m else None

    title_node = soup.select_one("h1")
    price = attributes.get("fiyat")

    return {
        "listing_id": classified_id,
        "title": _text(title_node) if title_node else None,
        "price_try": int(price) if price and price.isdigit() else None,
        "heavy_damage_record": attributes.get("agir_hasar_kayitli"),
        "damage": parse_damage(soup),
        "location": parse_location(soup),
        "description": parse_description(soup),
        "attributes": attributes,
        "info": info,
        "spec": {k[4:]: v for k, v in dfp.items() if k.startswith("etx_")},
        "equipment": {k: dfp[k] for k in EQUIPMENT_TAGS if isinstance(dfp.get(k), list)},
        "dfp": {k: v for k, v in dfp.items() if not k.startswith("etx_")},
        "images": parse_images(soup),
    }


if __name__ == "__main__":
    import sys
    from pathlib import Path

    for path in sys.argv[1:]:
        data = parse_detail(Path(path).read_text(encoding="utf-8", errors="replace"))
        print(json.dumps(data, ensure_ascii=False, indent=2))
