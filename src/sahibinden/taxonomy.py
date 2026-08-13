"""Turn "2019 Golf 1.6 TDI in Ankara" into a sahibinden search URL.

sahibinden search URLs are slug paths, not id queries:

    https://www.sahibinden.com/skoda-scala
    https://www.sahibinden.com/skoda-scala-1.6-tdi-premium/hatay-defne-merkez

So resolution is mostly slugification. The hard part is validating that a
brand actually exists before spending a request on a URL that cannot match —
and the brand and province lists come free out of the dropdowns embedded in any
saved detail page, so no crawl is needed to build them.

Models are not free: sahibinden loads them per-brand over XHR. Rather than
crawling every brand's model list up front, a model slug is passed through and
*verified on first use* — the sweep reports total_results, which tells us
immediately whether the URL matched anything. Verified resolutions are cached,
so each model costs at most one request ever.
"""

from __future__ import annotations

import json
import re
import unicodedata
from difflib import get_close_matches
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TAXONOMY_PATH = PROJECT_ROOT / "data" / "taxonomy.json"
BASE_URL = "https://www.sahibinden.com"

#: Turkish letters that unicodedata would either drop or fold wrongly.
#: Notably 'ı' has no ASCII decomposition and 'İ' folds to 'i̇' (i + combining
#: dot), which would produce doubled characters in a slug.
TR_MAP = str.maketrans({
    "ı": "i", "İ": "i", "I": "i", "i": "i",
    "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
    "ü": "u", "Ü": "u", "ö": "o", "Ö": "o",
    "ç": "c", "Ç": "c", "â": "a", "Â": "a", "î": "i", "û": "u",
})


def slugify(value: str) -> str:
    """'Mercedes - Benz C Serisi' -> 'mercedes-benz-c-serisi'."""
    text = value.translate(TR_MAP).lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


#: Abbreviations people actually type that are not prefixes of the real name,
#: so neither prefix nor fuzzy matching would ever reach them.
BRAND_ALIASES = {
    "vw": "Volkswagen",
    "mb": "Mercedes-Benz",
    "merco": "Mercedes-Benz",
    "range rover": "Land Rover",
}


def _resolve(value: str, slugs: dict[str, str], aliases: dict[str, str],
             cutoff: float) -> tuple[str | None, list[str]]:
    """Resolve a user string against known names.

    Tried in order of confidence: alias, exact slug, unique prefix, then fuzzy.
    A prefix only wins when exactly one name matches — 'a' must not silently
    pick whichever brand happens to sort first. When a prefix is ambiguous the
    candidates come back so the caller can ask which one was meant, rather than
    guessing and returning a market summary for the wrong car.
    """
    raw = value.strip().lower()
    if raw in aliases:
        return aliases[raw], []

    slug = slugify(value)
    if not slug:
        return None, []
    if slug in slugs:
        return slugs[slug], []

    prefixed = sorted(name for key, name in slugs.items() if key.startswith(slug))
    if len(prefixed) == 1:
        return prefixed[0], []
    if len(prefixed) > 1:
        return None, prefixed

    near = get_close_matches(slug, list(slugs), n=1, cutoff=cutoff)
    return (slugs[near[0]] if near else None), []


#: CLI flag -> the group name sahibinden uses. Mapped by *name*, not by
#: attribute id: the ids are category-specific, so keying on them would break
#: the moment the tool is pointed at anything but cars.
ENUM_FLAGS = {
    "fuel": "Yakıt / Motor Tipi",
    "transmission": "Vites",
    "condition": "Araç Durumu",
    "body": "Kasa Tipi",
    "power": "Motor Gücü",
    "engine": "Motor Hacmi",
    "drive": "Çekiş",
    "color": "Renk",
    "warranty": "Garanti",
    "heavy_damage": "Ağır Hasar Kayıtlı",
    "plate": "Plaka / Uyruk",
    "seller": "Kimden",
    "tradeable": "Takaslı",
    "listed_within": "İlan Tarihi",
}

#: English input people will reach for, mapped onto sahibinden's own labels.
ENUM_SYNONYMS = {
    "gasoline": "Benzin", "petrol": "Benzin", "diesel": "Dizel",
    "motorin": "Dizel", "hybrid": "Benzin (Hibrit)", "electric": "Elektrik",
    "automatic": "Otomatik", "manual": "Manuel",
    "yes": "Evet", "no": "Hayır", "true": "Evet", "false": "Hayır",
    "second hand": "İkinci El", "used": "İkinci El", "new": "Sıfır",
    "imported": "Yurtdışından İthal Sıfır",
    "white": "Beyaz", "black": "Siyah", "grey": "Gri", "gray": "Gri",
    "red": "Kırmızı", "blue": "Mavi", "silver": "Gümüş Gri",
    "owner": "Sahibinden", "dealer": "Galeriden", "authorised": "Yetkili Bayiden",
    "fwd": "Önden Çekiş", "rwd": "Arkadan İtiş",
}


class Taxonomy:
    def __init__(self, path: Path | str = TAXONOMY_PATH) -> None:
        self.path = Path(path)
        data = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists() else {"brands": {}, "cities": {}, "filters": {}, "enums": {}}
        )
        self.brands: dict[str, str] = data.get("brands", {})
        self.cities: dict[str, str] = data.get("cities", {})
        #: Range filters discovered from sahibinden's own search form, keyed by
        #: its Turkish label ("Yıl", "KM", "Fiyat").
        self.filters: dict[str, dict] = data.get("filters", {})
        #: Enumerated filters harvested from the site's facet links,
        #: keyed by its own Turkish group name ("Yakıt / Motor Tipi").
        self.enums: dict[str, dict] = data.get("enums", {})
        self._brand_slugs = {slugify(name): name for name in self.brands}
        self._city_slugs = {slugify(name): name for name in self.cities}

    # ------------------------------------------------------------- building

    @staticmethod
    def extract(html: str) -> dict:
        """Pull brands, provinces and range-filter ids out of a saved page.

        Brands and provinces come from the dropdowns on a detail page. The
        range filters come from the search page's own filter form, which is the
        only trustworthy source: sahibinden addresses numeric attributes as
        a{id}_min / a{id}_max where the ids are category-specific and carry no
        meaning. Guessing them is worse than useless — an id that belongs to a
        different attribute still returns HTTP 200 and a filtered-looking page,
        so the caller gets confidently wrong numbers.
        """
        from bs4 import BeautifulSoup

        from .parse.util import text  # noqa: F401  (local import keeps deps lazy)

        soup = BeautifulSoup(html, "lxml")
        found: dict[str, dict] = {"brands": {}, "cities": {}, "filters": {}, "enums": {}}

        for select in soup.select("select"):
            key = {"brand": "brands", "city": "cities"}.get(select.get("name") or "")
            if not key:
                continue
            for option in select.select("option"):
                value, label = option.get("value"), option.get_text(strip=True)
                # Skip the "please choose" placeholder, which has no id.
                if value and value.isdigit() and label:
                    found[key][label] = value

        # Enumerated filters (fuel, transmission, colour, body, damage record,
        # seller type, listing age...). sahibinden addresses these as
        # a{attributeId}={valueId}; both numbers are opaque and category
        # specific, so the only safe source is the site's own facet links.
        # Each `ul.facetedSearchList` carries its parameter as a CSS class and
        # sits under a `dt.collapseTitle` naming the group, so the whole
        # vocabulary can be lifted verbatim rather than guessed.
        for section in soup.select("div.search-filter-section"):
            title_el = section.select_one("dt.collapseTitle")
            title = text(title_el)
            if not title:
                continue
            for ul in section.select("ul.facetedSearchList"):
                classes = [c for c in (ul.get("class") or [])
                           if c not in ("facetedSearchList", "scroll-pane")]
                options: dict[str, str] = {}
                param_used = None
                for link in ul.select("a[href]"):
                    query = dict(parse_qsl(urlparse(link["href"]).query))
                    for candidate in classes:
                        if candidate in query:
                            label = text(link)
                            if label:
                                options[label] = query[candidate]
                                param_used = candidate
                            break
                if options and param_used:
                    found["enums"][title] = {"param": param_used, "options": options}

        # Faceted search never offers the filter you already have applied, so a
        # page saved with "Agir Hasar Kayitli: Hayir" advertises only "Evet" —
        # and the most useful value would be permanently unharvestable. The
        # applied-filter chips fill that gap: each chip's link *removes* its own
        # filter, so the parameter missing from a chip's href is the one that
        # chip represents, and the chip's text is its label.
        chips = soup.select(".currentFilters a[href]")
        chip_params: list[tuple[str, dict]] = []
        for chip in chips:
            href = chip.get("href") or ""
            if not href:
                continue
            chip_params.append((text(chip), dict(parse_qsl(urlparse(href).query))))

        if len(chip_params) > 1:
            everything: dict[str, str] = {}
            for _, query in chip_params:
                everything.update(query)
            known_params = {spec["param"]: group
                            for group, spec in found["enums"].items()}
            for label, query in chip_params:
                removed = {k: v for k, v in everything.items() if k not in query}
                # Exactly one is the signature of a real filter chip. The
                # "clear all" control drops every parameter and the "hide
                # selections" toggle drops none; both would otherwise be
                # harvested as filter values named after their button text.
                if len(removed) != 1 or not label:
                    continue
                param, value = next(iter(removed.items()))
                group = known_params.get(param)
                # Only merge into groups we already recognise; the boolean
                # paint/change flags are handled separately and would
                # otherwise appear as bogus one-option enums.
                if group:
                    found["enums"][group]["options"].setdefault(label, value)

        for inp in soup.select("input[name$=_min], input[name$=_max]"):
            label = (inp.get("data-label") or "").strip()
            name = inp.get("name") or ""
            if not label or not name:
                continue
            found["filters"][label] = {
                "param": name.rsplit("_", 1)[0],
                "min": inp.get("data-min"),
                "max": inp.get("data-max"),
                "type": inp.get("data-type"),
            }
        return found

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"brands": self.brands, "cities": self.cities,
                        "filters": self.filters, "enums": self.enums},
                       ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def merge(self, found: dict) -> dict:
        added = {"brands": 0, "cities": 0, "filters": 0, "enums": 0}
        for key in ("brands", "cities", "filters"):
            target = getattr(self, key)
            for label, value in found.get(key, {}).items():
                if label not in target:
                    target[label] = value
                    added[key] += 1

        # Enum groups merge option-by-option: a later page may reveal values an
        # earlier one had applied (and therefore hid), so replacing the group
        # wholesale would keep losing them.
        for group, spec in found.get("enums", {}).items():
            existing = self.enums.setdefault(group, {"param": spec["param"],
                                                     "options": {}})
            for label, value in spec["options"].items():
                if label not in existing["options"]:
                    existing["options"][label] = value
                    added["enums"] += 1
        self._brand_slugs = {slugify(n): n for n in self.brands}
        self._city_slugs = {slugify(n): n for n in self.cities}
        return added

    # ------------------------------------------------------------ resolving

    def match_brand(self, value: str, *, detail: bool = False):
        """Resolve to a known brand. See _resolve for the matching order."""
        name, ambiguous = _resolve(value, self._brand_slugs, BRAND_ALIASES, 0.75)
        return (name, ambiguous) if detail else name

    def match_city(self, value: str, *, detail: bool = False):
        name, ambiguous = _resolve(value, self._city_slugs, {}, 0.8)
        return (name, ambiguous) if detail else name

    def resolve_enum(self, flag: str, value: str) -> dict:
        """Turn --fuel diesel into ('a17', '22'), or explain why it cannot.

        Never falls back to a guess. An unrecognised option returns the valid
        ones instead, because a silently-dropped filter would give the caller a
        confident answer about a market they did not ask for.
        """
        group = ENUM_FLAGS.get(flag)
        spec = self.enums.get(group) if group else None
        if not spec:
            return {"error": f"no harvested filter for --{flag.replace('_','-')}",
                    "hint": "run: sahibinden-cli filters"}

        options = spec["options"]
        wanted = ENUM_SYNONYMS.get(value.strip().lower(), value)
        lookup = {slugify(k): k for k in options}

        key = lookup.get(slugify(wanted))
        if key is None:
            prefixed = sorted(k for s, k in lookup.items()
                              if s.startswith(slugify(wanted)))
            if len(prefixed) == 1:
                key = prefixed[0]
            elif len(prefixed) > 1:
                return {"error": f"{value!r} matches {len(prefixed)} options for "
                                 f"--{flag.replace('_','-')}", "options": prefixed}
        if key is None:
            near = get_close_matches(slugify(wanted), list(lookup), n=3, cutoff=0.6)
            return {"error": f"unknown {group} value {value!r}",
                    "options": [lookup[n] for n in near] or sorted(options)}

        return {"param": spec["param"], "value": options[key], "label": key,
                "group": group}

    def build_url(
        self,
        brand: str,
        model: str | None = None,
        *,
        city: str | None = None,
        min_year: int | None = None,
        max_year: int | None = None,
        max_km: int | None = None,
        min_price: int | None = None,
        max_price: int | None = None,
        undamaged_only: bool = False,
        sort: str = "price_asc",
        enums: dict[str, str] | None = None,
    ) -> dict:
        """Compose a search URL, reporting what matched and what was guessed."""
        known_brand, ambiguous = self.match_brand(brand, detail=True)
        if known_brand is None:
            return {
                "error": (f"{brand!r} matches {len(ambiguous)} brands"
                          if ambiguous else f"unknown brand {brand!r}"),
                "did_you_mean": ambiguous or [
                    self._brand_slugs[s] for s in get_close_matches(
                        slugify(brand), list(self._brand_slugs), n=5, cutoff=0.4)
                ],
            }

        path = slugify(known_brand)
        if model:
            path += "-" + slugify(model)

        known_city = None
        if city:
            known_city, ambiguous_city = self.match_city(city, detail=True)
            if known_city is None:
                return {"error": (f"{city!r} matches {len(ambiguous_city)} cities"
                                  if ambiguous_city else f"unknown city {city!r}"),
                        "did_you_mean": ambiguous_city or [
                            self._city_slugs[s] for s in get_close_matches(
                                slugify(city), list(self._city_slugs), n=5, cutoff=0.4)]}
            path += "/" + slugify(known_city)

        # Range parameters come from the filter map harvested out of
        # sahibinden's own search form, never from guesswork: the ids are
        # category-specific and an id belonging to a different attribute still
        # returns HTTP 200 and a filtered-looking page, so a wrong guess yields
        # confidently wrong numbers rather than an error.
        params: list[tuple[str, str]] = []
        unsupported: dict[str, int] = {}

        def add_range(label: str, low: int | None, high: int | None) -> None:
            spec = self.filters.get(label)
            if spec is None:
                if low:
                    unsupported[f"min_{label}"] = low
                if high:
                    unsupported[f"max_{label}"] = high
                return
            if low:
                params.append((f"{spec['param']}_min", str(low)))
            if high:
                params.append((f"{spec['param']}_max", str(high)))

        add_range("Yıl", min_year, max_year)
        add_range("KM", None, max_km)
        add_range("Fiyat", min_price, max_price)
        applied_enums = {}
        for flag, value in (enums or {}).items():
            if value is None:
                continue
            resolved_enum = self.resolve_enum(flag, value)
            if resolved_enum.get("error"):
                return resolved_enum
            params.append((resolved_enum["param"], resolved_enum["value"]))
            applied_enums[resolved_enum["group"]] = resolved_enum["label"]

        if undamaged_only:
            params += [("unpaintedParts", "true"), ("unchangingTracks", "true")]
        if sort:
            params.append(("sorting", sort))

        from urllib.parse import urlencode

        url = f"{BASE_URL}/{path}"
        if params:
            url += "?" + urlencode(params)

        # Anything the filter map did not cover still has to be honoured, so it
        # falls back to filtering the parsed rows. Normally empty.
        post_filters = {
            "min_year": unsupported.get("min_Yıl"),
            "max_year": unsupported.get("max_Yıl"),
            "max_km": unsupported.get("max_KM"),
        }
        post_filters = {k: v for k, v in post_filters.items() if v}

        return {
            "url": url,
            "brand": known_brand,
            "model": model,
            "city": known_city,
            # The brand and city came from sahibinden's own lists; the model
            # slug is a guess until a sweep returns a non-zero total_results.
            "verified": False,
            "model_is_guess": bool(model),
            "post_filters": post_filters,
            "filters_applied": applied_enums,
        }


def apply_post_filters(listings: list[dict], filters: dict) -> list[dict]:
    """Apply year/km constraints to parsed search rows.

    Search rows carry year and km as display strings ('2020', '159.000'), so
    both are normalised here rather than trusted as numbers.
    """
    if not filters:
        return listings

    def keep(row: dict) -> bool:
        attrs = row.get("attributes") or {}
        year = re.sub(r"\D", "", attrs.get("year", "") or "")
        km = re.sub(r"\D", "", attrs.get("km", "") or "")
        if filters.get("min_year") and (not year or int(year) < filters["min_year"]):
            return False
        if filters.get("max_year") and (not year or int(year) > filters["max_year"]):
            return False
        if filters.get("max_km") and (not km or int(km) > filters["max_km"]):
            return False
        return True

    return [row for row in listings if keep(row)]
