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


class Taxonomy:
    def __init__(self, path: Path | str = TAXONOMY_PATH) -> None:
        self.path = Path(path)
        data = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists() else {"brands": {}, "cities": {}, "filters": {}}
        )
        self.brands: dict[str, str] = data.get("brands", {})
        self.cities: dict[str, str] = data.get("cities", {})
        #: Range filters discovered from sahibinden's own search form, keyed by
        #: its Turkish label ("Yıl", "KM", "Fiyat").
        self.filters: dict[str, dict] = data.get("filters", {})
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

        soup = BeautifulSoup(html, "lxml")
        found: dict[str, dict] = {"brands": {}, "cities": {}, "filters": {}}

        for select in soup.select("select"):
            key = {"brand": "brands", "city": "cities"}.get(select.get("name") or "")
            if not key:
                continue
            for option in select.select("option"):
                value, label = option.get("value"), option.get_text(strip=True)
                # Skip the "please choose" placeholder, which has no id.
                if value and value.isdigit() and label:
                    found[key][label] = value

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
                        "filters": self.filters},
                       ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def merge(self, found: dict) -> dict:
        added = {"brands": 0, "cities": 0, "filters": 0}
        for key in ("brands", "cities", "filters"):
            target = getattr(self, key)
            for label, value in found.get(key, {}).items():
                if label not in target:
                    target[label] = value
                    added[key] += 1
        self._brand_slugs = {slugify(n): n for n in self.brands}
        self._city_slugs = {slugify(n): n for n in self.cities}
        return added

    # ------------------------------------------------------------ resolving

    def match_brand(self, value: str) -> str | None:
        """Exact slug match, else nearest known brand — 'wolkswagen' still lands."""
        slug = slugify(value)
        if slug in self._brand_slugs:
            return self._brand_slugs[slug]
        near = get_close_matches(slug, list(self._brand_slugs), n=1, cutoff=0.75)
        return self._brand_slugs[near[0]] if near else None

    def match_city(self, value: str) -> str | None:
        slug = slugify(value)
        if slug in self._city_slugs:
            return self._city_slugs[slug]
        near = get_close_matches(slug, list(self._city_slugs), n=1, cutoff=0.8)
        return self._city_slugs[near[0]] if near else None

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
    ) -> dict:
        """Compose a search URL, reporting what matched and what was guessed."""
        known_brand = self.match_brand(brand)
        if known_brand is None:
            return {
                "error": f"unknown brand {brand!r}",
                "did_you_mean": get_close_matches(
                    slugify(brand), list(self._brand_slugs), n=5, cutoff=0.4
                ),
            }

        path = slugify(known_brand)
        if model:
            path += "-" + slugify(model)

        known_city = None
        if city:
            known_city = self.match_city(city)
            if known_city is None:
                return {"error": f"unknown city {city!r}",
                        "did_you_mean": get_close_matches(
                            slugify(city), list(self._city_slugs), n=5, cutoff=0.4)}
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
