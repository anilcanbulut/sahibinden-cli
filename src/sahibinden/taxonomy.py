"""Turn "2019 Golf 1.6 TDI in Ankara" into a sahibinden search URL.

sahibinden search URLs are slug paths, not id queries:

    https://www.sahibinden.com/skoda-scala
    https://www.sahibinden.com/arazi-suv-pickup-volkswagen-tiguan/istanbul

Vehicles live in separate categories — Otomobil, Arazi SUV & Pickup, Minivan
& Panelvan — and a model belongs to exactly one of them. A Tiguan under the
Otomobil path is a 404, not an empty search. The categories also have their
own filter ids ("Yıl" is a5 under Otomobil and a277 under SUV), so everything
here is kept per category and never mixed.

Brands, models and their exact paths come from the category tree in every
search page's sidebar: a category page lists its brands, a brand page lists
its models. Brand lists ship in ``taxonomy_seed.json``. A brand's model list
costs one request per category the first time that brand is searched with a
model, and is kept from then on, so a model is resolved against the site's own
list instead of guessed.
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
#: Committed vocabulary — brands, provinces and every category's filter ids —
#: so a fresh clone can search before it has fetched anything.
SEED_PATH = Path(__file__).resolve().with_name("taxonomy_seed.json")
BASE_URL = "https://www.sahibinden.com"

#: What people type for --category, mapped onto the category's own path.
CATEGORY_ALIASES = {
    "car": "otomobil", "cars": "otomobil", "otomobil": "otomobil",
    "suv": "arazi-suv-pickup", "pickup": "arazi-suv-pickup",
    "arazi": "arazi-suv-pickup", "arazi-suv-pickup": "arazi-suv-pickup",
    "minivan": "minivan-panelvan", "panelvan": "minivan-panelvan",
    "van": "minivan-panelvan", "minivan-panelvan": "minivan-panelvan",
}

#: Tried in this order when the caller does not name a category, so a model
#: that exists in two of them resolves to the ordinary car first.
CATEGORY_ORDER = ("otomobil", "arazi-suv-pickup", "minivan-panelvan")

#: The --category spelling for each, for messages that suggest one.
CATEGORY_NAMES = {"otomobil": "car", "arazi-suv-pickup": "suv",
                  "minivan-panelvan": "minivan"}

#: Dropdown placeholders that carry a numeric value like a real option.
PLACEHOLDERS = {"seciniz", "tumu", "hepsi"}

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
#: the moment the tool is pointed at anything but cars. A tuple lists names the
#: site has used over time, current first ("Garanti" became "Servis Garantisi").
ENUM_FLAGS = {
    "fuel": "Yakıt / Motor Tipi",
    "transmission": "Vites",
    "condition": "Araç Durumu",
    "body": "Kasa Tipi",
    "power": "Motor Gücü",
    "engine": "Motor Hacmi",
    "drive": "Çekiş",
    "color": "Renk",
    "warranty": ("Servis Garantisi", "Garanti"),
    "heavy_damage": "Ağır Hasar Kayıtlı",
    "plate": "Plaka / Uyruk",
    "seller": "Kimden",
    "tradeable": "Takaslı",
    "listed_within": "İlan Tarihi",
}

#: CLI flag -> sahibinden's own boolean parameter. Unlike the enum attributes
#: these are addressed by name rather than by an opaque numeric id, and the
#: names are lifted verbatim from the checkboxes in the sidebar's "Boya,
#: Değişen Parça" and "Fotoğraf, Video" sections. Harvesting confirms them and
#: supplies the labels; it is not required for them to work, so a stale
#: taxonomy degrades to "unverified" rather than to a dropped filter.
FLAG_FILTERS = {
    "unpainted": "unpaintedParts",
    "unchanged": "unchangingTracks",
    "has_video": "hasVideo",
    "has_clip": "hasClip",
}

#: The unit tabs above the price inputs. The parameter is named but its values
#: are opaque ids, so those come from the harvest and are never assumed.
CURRENCY_PARAM = "price_currency"

CURRENCY_SYNONYMS = {
    "try": "TL", "tl": "TL", "lira": "TL", "₺": "TL",
    "usd": "USD", "dollar": "USD", "dolar": "USD", "$": "USD",
    "eur": "EUR", "euro": "EUR", "€": "EUR",
    "gbp": "GBP", "pound": "GBP", "sterlin": "GBP", "£": "GBP",
}

#: Free-text filter ("Kelime ile filtrele") at the foot of the sidebar.
KEYWORD_PARAM = "query_text"
KEYWORD_DESC_PARAM = "query_desc"

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


def _empty_category(label: str | None = None) -> dict:
    return {"label": label, "brands": {}, "models": {}, "filters": {},
            "enums": {}, "flags": {}, "units": {}}


def _ordered(keys) -> list[str]:
    """Vehicle categories in preference order, then anything else harvested."""
    keys = list(keys)
    return ([k for k in CATEGORY_ORDER if k in keys]
            + sorted(k for k in keys if k not in CATEGORY_ORDER))


class Taxonomy:
    def __init__(self, path: Path | str = TAXONOMY_PATH,
                 seed: Path | str | None = SEED_PATH) -> None:
        self.path = Path(path)
        self.cities: dict[str, str] = {}
        #: Keyed by the category's own URL path ("otomobil",
        #: "arazi-suv-pickup"). Each holds:
        #:   brands  — name -> path segment ("Volkswagen" -> "volkswagen")
        #:   models  — brand -> {"models": {name: segment}, "complete": bool}
        #:   filters — range inputs by Turkish label ("Yıl" -> a5)
        #:   enums   — facet groups by Turkish name ("Yakıt / Motor Tipi")
        #:   flags   — boolean checkboxes ("Boyasız" -> unpaintedParts)
        #:   units   — unit tabs ("price_currency" -> {"TL": "1"})
        self.categories: dict[str, dict] = {}
        # The local file first, so what this machine harvested wins; the seed
        # then fills in whatever it has not seen. A file from before categories
        # existed contributes its provinces only: its filter ids carry no
        # category, and guessing one is exactly how ids get crossed.
        for source in (self.path, seed):
            if source and Path(source).exists():
                self.merge(json.loads(Path(source).read_text(encoding="utf-8")))
        self._reindex()

    def _reindex(self) -> None:
        self._brand_slugs = {slugify(name): name for name in self.brands}
        self._city_slugs = {slugify(name): name for name in self.cities}

    @property
    def brands(self) -> dict[str, list[str]]:
        """Every known brand, with the categories it has listings in."""
        out: dict[str, list[str]] = {}
        for key in _ordered(self.categories):
            for name in self.categories[key]["brands"]:
                out.setdefault(name, []).append(key)
        return out

    def brand_paths(self) -> list[tuple[str, str]]:
        """(path segment, brand) for every category, longest segment first.

        Longest first because 'mercedes-benz-c-serisi' must not resolve to a
        brand called 'mercedes', nor 'arazi-suv-pickup-volkswagen-tiguan' to
        anything shorter than its own brand.
        """
        pairs = {(seg, name) for cat in self.categories.values()
                 for name, seg in cat["brands"].items()}
        return sorted(pairs, key=lambda pair: -len(pair[0]))

    # ------------------------------------------------------------- building

    @staticmethod
    def _category_tree(soup) -> tuple[str | None, str | None, list[tuple], bool]:
        """Read the sidebar's category tree: (category key, label, nodes, bare).

        Every search page carries it. Its ancestors (``li > div > a``) are the
        page's own breadcrumb — Vasıta, then the category, brand, model — and
        its leaves (``li > a``) are the page's children, so a category page
        lists every brand and a brand page every model.

        On an unfiltered page the links are the site's own paths. Once any
        filter or location is applied they all become /arama/flt/?category=ID
        instead, which name no path — so those nodes contribute their names
        only, and ``bare`` says which kind of page this was. Nodes are
        (level, name, path segment or "", is_ancestor).
        """
        from .parse.util import text

        root = soup.select_one("#search_cats")
        if root is None:
            return None, None, [], False
        nodes = []
        for li in root.select("li"):
            level = next((int(c[2:]) for c in li.get("class") or []
                          if re.fullmatch(r"cl\d", c)), None)
            link = li.find("a", href=True)
            if level is None or link is None:
                continue
            path = urlparse(link["href"]).path.strip("/")
            segment = "" if path.startswith("arama") else path.split("/")[0]
            name = (link.get("title") or text(link) or "").strip()
            if name and (segment or path.startswith("arama")):
                nodes.append((level, name, segment,
                              li.find("div", recursive=False) is not None))
        category = next((node for node in nodes if node[0] == 1), None)
        if category is None:
            return None, None, nodes, False
        bare = all(node[2] for node in nodes)
        # The category's own path is its name slugified — "Arazi, SUV &
        # Pickup" is /arazi-suv-pickup — which is what places a filtered page.
        return category[2] or slugify(category[1]), category[1], nodes, bare

    @staticmethod
    def extract(html: str) -> dict:
        """Pull provinces, brands, models and filter ids out of a saved page.

        Provinces come from the dropdowns on a detail page. Everything else is
        filed under the category the page belongs to, read from its sidebar
        tree, because the filter ids are category-specific and carry no
        meaning: sahibinden addresses numeric attributes as a{id}_min /
        a{id}_max, and "Yıl" is a5 under Otomobil but a277 under SUV. Guessing
        them is worse than useless — an id that belongs to a different
        attribute still returns HTTP 200 and a filtered-looking page, so the
        caller gets confidently wrong numbers. A page whose category cannot be
        read contributes no filter ids at all.
        """
        from bs4 import BeautifulSoup

        from .parse.util import text

        soup = BeautifulSoup(html, "lxml")
        found: dict = {"cities": {}, "categories": {}}

        for select in soup.select("select[name=city]"):
            for option in select.select("option"):
                value, label = option.get("value"), option.get_text(strip=True)
                # Skip the "please choose" placeholder, which may carry an id.
                if (value and value.isdigit() and label
                        and slugify(label) not in PLACEHOLDERS):
                    found["cities"][label] = value

        key, label, nodes, bare = Taxonomy._category_tree(soup)
        if key is None:
            return found
        cat = found["categories"][key] = _empty_category(label)

        brands = [(name, seg) for lvl, name, seg, _ in nodes if lvl == 2 and seg]
        for name, seg in brands:
            cat["brands"][name] = seg
        # Models are only attributable on a page that sits under one brand,
        # and only an unfiltered brand page lists all of them: a filtered one
        # shows just the models with matching listings, and a model page just
        # itself. So "complete" is claimed only for the first case.
        if len(brands) == 1:
            models = {name: seg for lvl, name, seg, _ in nodes if lvl == 3 and seg}
            deepest = max((lvl for lvl, _, _, ancestor in nodes if ancestor), default=None)
            if models:
                cat["models"][brands[0][0]] = {"models": models}
                if bare and deepest == 2:
                    cat["models"][brands[0][0]]["complete"] = True

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
            # The unit tabs above the price inputs ("TL | USD | EUR | GBP").
            # The parameter is in data-id and the value in data-value; the
            # value is an opaque id, so it is lifted rather than assumed.
            for tab in section.select("a.js-unit-tab[data-id][data-value]"):
                tab_label = (tab.get("title") or text(tab)).strip()
                if tab_label:
                    cat["units"].setdefault(tab["data-id"], {})[tab_label] = tab["data-value"]

            for ul in section.select("ul.facetedSearchList"):
                classes = [c for c in (ul.get("class") or [])
                           if c not in ("facetedSearchList", "scroll-pane")]
                options: dict[str, str] = {}
                param_used = None
                multi = False
                for link in ul.select("a[href]"):
                    query = dict(parse_qsl(urlparse(link["href"]).query))
                    for candidate in classes:
                        if candidate in query:
                            option_label = text(link)
                            if option_label:
                                options[option_label] = query[candidate]
                                param_used = candidate
                                # Whether a group accepts several values at
                                # once is not a guess either: sahibinden draws
                                # multi-select groups as checkboxes and
                                # single-select ones (Kimden, İlan Tarihi) as
                                # radios, and says which in the link's class.
                                multi = "facetedCheckbox" in (link.get("class") or [])
                            break
                    else:
                        # No shared parameter: this list is a set of boolean
                        # flags, where each option *is* its own parameter
                        # (Boyasız -> unpaintedParts, Videolu ilanlar ->
                        # hasVideo) rather than a value of a common one.
                        flag = link.get("name") or link.get("data-id")
                        option_label = text(link)
                        if flag and option_label and link.get("data-value") == "true":
                            cat["flags"].setdefault(title, {})[option_label] = flag
                if options and param_used:
                    cat["enums"][title] = {"param": param_used, "options": options,
                                           "multi": multi}

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
                            for group, spec in cat["enums"].items()}
            for chip_label, query in chip_params:
                removed = {k: v for k, v in everything.items() if k not in query}
                # Exactly one is the signature of a real filter chip. The
                # "clear all" control drops every parameter and the "hide
                # selections" toggle drops none; both would otherwise be
                # harvested as filter values named after their button text.
                if len(removed) != 1 or not chip_label:
                    continue
                param, value = next(iter(removed.items()))
                group = known_params.get(param)
                # Only merge into groups we already recognise; the boolean
                # paint/change flags are handled separately and would
                # otherwise appear as bogus one-option enums.
                if group:
                    cat["enums"][group]["options"].setdefault(chip_label, value)

        for inp in soup.select("input[name$=_min], input[name$=_max]"):
            range_label = (inp.get("data-label") or "").strip()
            name = inp.get("name") or ""
            if not range_label or not name:
                continue
            cat["filters"][range_label] = {
                "param": name.rsplit("_", 1)[0],
                "min": inp.get("data-min"),
                "max": inp.get("data-max"),
                "type": inp.get("data-type"),
            }
        return found

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"cities": self.cities, "categories": self.categories},
                       ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def merge(self, found: dict) -> dict:
        added = {"cities": 0, "brands": 0, "models": 0, "filters": 0,
                 "enums": 0, "flags": 0, "units": 0}
        for label, value in (found.get("cities") or {}).items():
            if slugify(label) not in PLACEHOLDERS and label not in self.cities:
                self.cities[label] = value
                added["cities"] += 1

        for key, incoming in (found.get("categories") or {}).items():
            cat = self.categories.setdefault(key, _empty_category())
            for field_name, default in _empty_category().items():
                cat.setdefault(field_name, default)
            cat["label"] = cat["label"] or incoming.get("label")

            for name, seg in (incoming.get("brands") or {}).items():
                if name not in cat["brands"]:
                    cat["brands"][name] = seg
                    added["brands"] += 1

            for brand, entry in (incoming.get("models") or {}).items():
                known = cat["models"].setdefault(brand, {"models": {}})
                known["complete"] = bool(known.get("complete") or entry.get("complete"))
                for name, seg in (entry.get("models") or {}).items():
                    if name not in known["models"]:
                        known["models"][name] = seg
                        added["models"] += 1

            for label, spec in (incoming.get("filters") or {}).items():
                if label not in cat["filters"]:
                    cat["filters"][label] = spec
                    added["filters"] += 1

            # Enum groups merge option-by-option: a later page may reveal values
            # an earlier one had applied (and therefore hid), so replacing the
            # group wholesale would keep losing them.
            for group, spec in (incoming.get("enums") or {}).items():
                existing = cat["enums"].setdefault(group, {"param": spec["param"],
                                                           "options": {}})
                # Checkbox-vs-radio is refreshed rather than merged: the page
                # just read is the authority on whether the group takes several
                # values, and a group recorded before that was tracked has no
                # answer.
                existing["multi"] = bool(spec.get("multi"))
                for label, value in spec["options"].items():
                    if label not in existing["options"]:
                        existing["options"][label] = value
                        added["enums"] += 1

            for group, options in (incoming.get("flags") or {}).items():
                known_flags = cat["flags"].setdefault(group, {})
                for label, param in options.items():
                    if label not in known_flags:
                        known_flags[label] = param
                        added["flags"] += 1

            for param, options in (incoming.get("units") or {}).items():
                known_units = cat["units"].setdefault(param, {})
                for label, value in options.items():
                    if label not in known_units:
                        known_units[label] = value
                        added["units"] += 1

        self._reindex()
        return added

    def mark_models_complete(self, category: str, brand: str) -> None:
        """Record that a brand's model list was read off its own bare page."""
        entry = self.categories[category]["models"].setdefault(brand, {"models": {}})
        entry["complete"] = True

    # ------------------------------------------------------------ resolving

    def resolve_category(self, value: str | None) -> tuple[list[str], dict | None]:
        """The categories to search, in preference order — or an error."""
        vehicles = [k for k in CATEGORY_ORDER if k in self.categories]
        if not value:
            return vehicles, None
        key = CATEGORY_ALIASES.get(value.strip().lower(), slugify(value))
        if key in self.categories:
            return [key], None
        error = {"error": f"unknown category {value!r}",
                 "options": [CATEGORY_NAMES.get(k, k) for k in vehicles]}
        if key in CATEGORY_ORDER:
            error = {"error": f"no filter list for {key} yet",
                     "hint": "one page teaches it: sahibinden-cli search --url "
                             f"{BASE_URL}/{key} --pages 1 --no-store"}
        return [], error

    def category_label(self, key: str) -> str:
        return self.categories.get(key, {}).get("label") or key

    def match_brand(self, value: str, *, detail: bool = False):
        """Resolve to a known brand. See _resolve for the matching order."""
        name, ambiguous = _resolve(value, self._brand_slugs, BRAND_ALIASES, 0.75)
        return (name, ambiguous) if detail else name

    def match_city(self, value: str, *, detail: bool = False):
        name, ambiguous = _resolve(value, self._city_slugs, {}, 0.8)
        return (name, ambiguous) if detail else name

    def _match_option(self, options: dict[str, str], value: str,
                      flag: str, group: str) -> tuple[str | None, dict | None]:
        """Match one user string against a group's labels, or explain."""
        wanted = ENUM_SYNONYMS.get(value.strip().lower(), value)
        lookup = {slugify(k): k for k in options}

        key = lookup.get(slugify(wanted))
        if key is None:
            prefixed = sorted(k for sl, k in lookup.items()
                              if sl.startswith(slugify(wanted)))
            if len(prefixed) == 1:
                key = prefixed[0]
            elif len(prefixed) > 1:
                return None, {"error": f"{value!r} matches {len(prefixed)} options for "
                                       f"--{flag.replace('_','-')}", "options": prefixed}
        if key is None:
            near = get_close_matches(slugify(wanted), list(lookup), n=3, cutoff=0.6)
            return None, {"error": f"unknown {group} value {value!r}",
                          "options": [lookup[n] for n in near] or sorted(options)}
        return key, None

    def resolve_enum(self, flag: str, values: str | list[str],
                     category: str = "otomobil") -> dict:
        """Turn --fuel diesel --fuel hybrid into ('a17', ['22', '1312756']).

        Never falls back to a guess. An unrecognised option returns the valid
        ones instead, because a silently-dropped filter would give the caller a
        confident answer about a market they did not ask for.

        Several values are only accepted where sahibinden itself accepts them.
        The sidebar draws multi-select groups as checkboxes and single-select
        ones (Kimden, İlan Tarihi) as radios; passing two values to a radio
        group would send two parameters and let the site pick, which is exactly
        the silent narrowing this refuses to do elsewhere.
        """
        if isinstance(values, str):
            values = [values]
        values = [v for v in values if v is not None]
        if not values:
            return {}

        group = self.enum_group(flag, category)
        enums = self.categories.get(category, {}).get("enums", {})
        spec = enums.get(group) if group else None
        if not spec:
            return {"error": f"no harvested filter for --{flag.replace('_','-')} "
                             f"under {self.category_label(category)}",
                    "hint": "run: sahibinden-cli harvest"}

        if len(values) > 1 and not spec.get("multi"):
            return {"error": f"--{flag.replace('_','-')} takes one value: sahibinden "
                             f"offers {group} as a single choice, not a checkbox list",
                    "given": values,
                    "hint": "if the sidebar shows checkboxes here, the taxonomy "
                            "predates multi-select tracking: run "
                            "sahibinden-cli harvest",
                    "options": sorted(spec["options"])}

        resolved, labels = [], []
        for value in values:
            key, error = self._match_option(spec["options"], value, flag, group)
            if error:
                return error
            resolved.append(spec["options"][key])
            labels.append(key)

        return {"param": spec["param"], "values": resolved, "labels": labels,
                "group": group, "multi": bool(spec.get("multi"))}

    def enum_group(self, flag: str, category: str = "otomobil") -> str | None:
        """The sidebar group a flag means in this category, by its current name."""
        names = ENUM_FLAGS.get(flag)
        if names is None:
            return None
        names = (names,) if isinstance(names, str) else names
        enums = self.categories.get(category, {}).get("enums", {})
        return next((name for name in names if name in enums), names[0])

    def resolve_currency(self, value: str, category: str = "otomobil") -> dict:
        """'usd' -> price_currency=2. The ids are opaque, so they are harvested."""
        units = self.categories.get(category, {}).get("units", {})
        options = units.get(CURRENCY_PARAM) or {}
        if not options:
            return {"error": "no harvested currency ids",
                    "hint": "run: sahibinden-cli harvest"}
        wanted = CURRENCY_SYNONYMS.get(value.strip().lower(), value.strip())
        for label, ident in options.items():
            if slugify(label) == slugify(wanted):
                return {"param": CURRENCY_PARAM, "value": ident, "label": label}
        return {"error": f"unknown currency {value!r}", "options": sorted(options)}

    def flag_label(self, param: str, category: str = "otomobil") -> str | None:
        """The sidebar wording for a boolean parameter, when it was harvested."""
        for group, options in self.categories.get(category, {}).get("flags", {}).items():
            for label, name in options.items():
                if name == param:
                    return f"{group}: {label}"
        return None

    def resolve_model(self, brand: str, model: str,
                      homes: list[str]) -> dict:
        """Find a model in the brand's lists: {category, model} or why not.

        Exact names are tried across every category first, so "Egea" is never
        answered with a fuzzy "Egea Cross" from the next category over. An
        incomplete list — models seen on individual model pages — can confirm
        an exact name but cannot rule one out, so a miss against it asks for
        the brand page instead of guessing.
        """
        want = slugify(model)
        lists = [(key, self.categories[key]["models"].get(brand) or {})
                 for key in homes]
        for key, entry in lists:
            exact = {slugify(n): n for n in entry.get("models") or {}}.get(want)
            if exact:
                return {"category": key, "model": exact}

        missing = [key for key, entry in lists if not entry.get("complete")]
        if missing:
            return {"learn": [{"category": key, "brand": brand,
                               "url": f"{BASE_URL}/{self.categories[key]['brands'][brand]}"}
                              for key in missing]}

        union: dict[str, tuple[str, str]] = {}
        for key, entry in lists:
            for name in entry.get("models") or {}:
                union.setdefault(slugify(name), (key, name))
        hit, ambiguous = _resolve(model, {s: s for s in union}, {}, 0.8)
        if hit:
            key, name = union[hit]
            return {"category": key, "model": name}
        if ambiguous:
            return {"error": f"{model!r} matches {len(ambiguous)} {brand} models",
                    "did_you_mean": [union[s][1] for s in ambiguous]}
        near = get_close_matches(want, list(union), n=5, cutoff=0.5)
        return {"error": f"unknown {brand} model {model!r}",
                "did_you_mean": [union[s][1] for s in near],
                "options": sorted(name for _, name in union.values())}

    def build_url(
        self,
        brand: str,
        model: str | None = None,
        *,
        category: str | None = None,
        city: str | None = None,
        district: str | None = None,
        min_year: int | None = None,
        max_year: int | None = None,
        min_km: int | None = None,
        max_km: int | None = None,
        min_price: int | None = None,
        max_price: int | None = None,
        currency: str | None = None,
        keyword: str | None = None,
        keyword_in_description: bool = False,
        undamaged_only: bool = False,
        flags: dict[str, bool] | None = None,
        sort: str = "price_asc",
        enums: dict[str, str | list[str]] | None = None,
    ) -> dict:
        """Compose a search URL, reporting what matched and what was guessed.

        Returns ``{"learn": [...]}`` instead when the model can only be placed
        after reading the brand's own page; the caller fetches those pages,
        merges them, and asks again.
        """
        keys, error = self.resolve_category(category)
        if error:
            return error
        if not keys:
            return {"error": "no vehicle categories known",
                    "hint": f"is {SEED_PATH.name} missing from the install?"}

        # Matched against every vehicle category, not just the one asked for,
        # so "jeep" under --category car is "Jeep is an SUV brand" rather than
        # a fuzzy guess at some car brand that happens to look similar.
        in_scope = {slugify(n): n for k in self.resolve_category(None)[0]
                    for n in self.categories[k]["brands"]}
        known_brand, ambiguous = _resolve(brand, in_scope, BRAND_ALIASES, 0.75)
        if known_brand is None:
            return {
                "error": (f"{brand!r} matches {len(ambiguous)} brands"
                          if ambiguous else f"unknown brand {brand!r}"),
                "did_you_mean": ambiguous or [
                    in_scope[s] for s in get_close_matches(
                        slugify(brand), list(in_scope), n=5, cutoff=0.4)
                ],
            }

        homes = [k for k in keys if known_brand in self.categories[k]["brands"]]
        if not homes:
            elsewhere = [k for k in self.categories
                         if known_brand in self.categories[k]["brands"]]
            return {"error": f"{known_brand} has no listings under "
                             f"{', '.join(self.category_label(k) for k in keys)}",
                    "hint": (f"it is listed under "
                             f"{', '.join(self.category_label(k) for k in elsewhere)}"
                             f" — use --category "
                             f"{CATEGORY_NAMES.get(elsewhere[0], elsewhere[0])}"
                             if elsewhere else None)}

        if model:
            placed = self.resolve_model(known_brand, model, homes)
            if "category" not in placed:
                return {**placed, "brand": known_brand}
            key, known_model = placed["category"], placed["model"]
            path = self.categories[key]["models"][known_brand]["models"][known_model]
        else:
            key, known_model = homes[0], None
            path = self.categories[key]["brands"][known_brand]
        cat = self.categories[key]

        known_city = None
        if district and not city:
            return {"error": f"--district {district!r} needs --city: sahibinden "
                             "addresses a district as part of its province "
                             "(/ankara-cankaya), never on its own"}
        if city:
            known_city, ambiguous_city = self.match_city(city, detail=True)
            if known_city is None:
                return {"error": (f"{city!r} matches {len(ambiguous_city)} cities"
                                  if ambiguous_city else f"unknown city {city!r}"),
                        "did_you_mean": ambiguous_city or [
                            self._city_slugs[s] for s in get_close_matches(
                                slugify(city), list(self._city_slugs), n=5, cutoff=0.4)]}
            # Districts are a second path segment glued onto the province
            # (/ankara-cankaya). They are loaded per-parent over XHR, so the
            # slug is passed through and verified by whether the sweep returns
            # anything, rather than validated up front.
            path += "/" + slugify(known_city)
            if district:
                path += "-" + slugify(district)

        # Range parameters come from the filter map harvested out of
        # sahibinden's own search form, never from guesswork: the ids are
        # category-specific and an id belonging to a different attribute still
        # returns HTTP 200 and a filtered-looking page, so a wrong guess yields
        # confidently wrong numbers rather than an error.
        params: list[tuple[str, str]] = []
        unsupported: dict[str, int] = {}

        def add_range(label: str, low: int | None, high: int | None) -> None:
            spec = cat["filters"].get(label)
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
        add_range("KM", min_km, max_km)
        add_range("Fiyat", min_price, max_price)

        # The currency tab governs how price_min/price_max are read, so it goes
        # in whenever it was asked for -- including with no price bounds, where
        # it still changes which listings the site considers in range.
        applied_currency = None
        if currency:
            resolved_currency = self.resolve_currency(currency, key)
            if resolved_currency.get("error"):
                return resolved_currency
            params.append((resolved_currency["param"], resolved_currency["value"]))
            applied_currency = resolved_currency["label"]

        applied_enums: dict[str, list[str]] = {}
        for flag, value in (enums or {}).items():
            if not value:
                continue
            resolved_enum = self.resolve_enum(flag, value, key)
            if resolved_enum.get("error"):
                return resolved_enum
            # Multi-select groups repeat the parameter, which is how the site's
            # own checkbox links do it (a116445=1263354&a116445=1263353).
            for ident in resolved_enum["values"]:
                params.append((resolved_enum["param"], ident))
            applied_enums[resolved_enum["group"]] = resolved_enum["labels"]

        # Boolean checkboxes are their own parameter rather than a value of a
        # shared one. --undamaged is the pair of them, kept because it is the
        # combination anyone actually wants.
        wanted_flags = {k: v for k, v in (flags or {}).items() if v}
        if undamaged_only:
            wanted_flags["unpainted"] = True
            wanted_flags["unchanged"] = True
        applied_flags, unverified = [], []
        for name, on in wanted_flags.items():
            if not on:
                continue
            param = FLAG_FILTERS.get(name)
            if param is None:
                return {"error": f"unknown flag {name!r}",
                        "options": sorted(FLAG_FILTERS)}
            params.append((param, "true"))
            label = self.flag_label(param, key)
            applied_flags.append(label or param)
            if label is None and cat["flags"]:
                unverified.append(param)

        if keyword:
            params.append((KEYWORD_PARAM, keyword))
            if keyword_in_description:
                params.append((KEYWORD_DESC_PARAM, "true"))

        if sort:
            params.append(("sorting", sort))

        url = f"{BASE_URL}/{path}"
        if params:
            url += "?" + urlencode(params)

        # Anything the filter map did not cover still has to be honoured, so it
        # falls back to filtering the parsed rows. Normally empty. Price only
        # falls back in lira, the one unit the parsed rows are compared in.
        post_filters = {
            "min_year": unsupported.get("min_Yıl"),
            "max_year": unsupported.get("max_Yıl"),
            "min_km": unsupported.get("min_KM"),
            "max_km": unsupported.get("max_KM"),
        }
        if not applied_currency:
            post_filters["min_price"] = unsupported.get("min_Fiyat")
            post_filters["max_price"] = unsupported.get("max_Fiyat")
        post_filters = {k: v for k, v in post_filters.items() if v}

        return {
            "url": url,
            "brand": known_brand,
            "model": known_model,
            "category": key,
            "category_label": self.category_label(key),
            # Where else a brand-only search could have gone: VW's SUVs are a
            # separate search from its cars, and the caller should say so.
            "other_categories": [self.category_label(k) for k in homes if k != key]
                                if not model else [],
            "city": known_city,
            "district": district,
            # Brand, model and city came from sahibinden's own lists; only the
            # district slug is a guess until a sweep returns results.
            "verified": False,
            "model_is_guess": False,
            "district_is_guess": bool(district),
            "post_filters": post_filters,
            "filters_applied": applied_enums,
            "flags_applied": applied_flags,
            "unverified_flags": unverified,
            "currency": applied_currency,
            "keyword": keyword or None,
        }


def apply_post_filters(listings: list[dict], filters: dict) -> list[dict]:
    """Apply year/km/price constraints to parsed search rows.

    Search rows carry year and km as display strings ('2020', '159.000'), so
    both are normalised here rather than trusted as numbers. A row priced in
    another currency has no lira price and so never passes a lira bound.
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
        if filters.get("min_km") and (not km or int(km) < filters["min_km"]):
            return False
        if filters.get("max_km") and (not km or int(km) > filters["max_km"]):
            return False
        price = row.get("price_try")
        if filters.get("min_price") and (price is None or price < filters["min_price"]):
            return False
        if filters.get("max_price") and (price is None or price > filters["max_price"]):
            return False
        return True

    return [row for row in listings if keep(row)]
