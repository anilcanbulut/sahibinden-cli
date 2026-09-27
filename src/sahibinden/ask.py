"""Ask for a search one filter at a time, the way the website's sidebar offers them.

`ask` is `search` for someone who would rather not learn the flags. Each answer
is checked as it is typed, against the same vocabulary `search` resolves with,
so a typo is caught at its own question with the valid values beside it rather
than after a minute of fetching. Enter skips a filter, as an empty sidebar box
does. The answers become ordinary `search` arguments, which the command saves
as a file under searches/ so the same search can be rerun without the questions.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import textwrap
from difflib import get_close_matches
from typing import Callable

from .panels import PANEL_GROUPS, PANEL_LABELS, PANELS, panel_name, resolve_panels
from .report import span
from .taxonomy import (CATEGORY_ALIASES, CATEGORY_NAMES, ENUM_SYNONYMS, Taxonomy,
                       slugify)

#: Shown as examples only; any brand or province sahibinden lists is accepted.
POPULAR_BRANDS = ("Renault", "Fiat", "Volkswagen", "Toyota", "Ford", "Hyundai",
                  "BMW", "Mercedes-Benz")
POPULAR_CITIES = ("İstanbul", "Ankara", "İzmir", "Bursa", "Antalya")

#: The words the order question accepts, for the --sort value each means.
#: "condition" is ours: the cheapest are read first, then ranked by body.
SORTS = {"cheapest": "price_asc", "newest": "date_desc",
         "expensive": "price_desc", "oldest": "date_asc",
         "condition": "condition"}

#: The part groups worth offering, in English; panels.py also takes Turkish.
PART_GROUPS = ("structural", "doors", "fenders", "bumpers", "front", "rear",
               "left", "right", "all")

#: The three per-part rules, loosest last: --flag, question, example, meaning.
PART_RULES = (
    ("original", "Parts that must be original (orijinal)", "hood, roof",
     "must be original"),
    ("not-changed", "Parts that may be painted but not replaced (değişen olmasın)",
     "structural   = every part but the bumpers", "painted is fine, not replaced"),
    ("not-painted", "Parts that may have local paint only (boya olmasın)", "doors",
     "local paint is fine, not painted or replaced"),
)

#: Choice filters, as the sidebar lists them: flag -> (question, examples to
#: offer). The first example valid in the search's category is shown, since
#: body types and drives differ between cars and SUVs.
CHOICES = {
    "condition": ("Condition", ("used",)),
    "fuel": ("Fuel", ("diesel   or several: diesel, hybrid",)),
    "transmission": ("Gearbox", ("automatic",)),
    "body": ("Body type", ("sedan", "suv")),
    "color": ("Colour", ("white   or several: white, black",)),
    "seller": ("Seller", ("owner   (dealer = galeriden)",)),
    "heavy_damage": ("Heavy-damage record", ("no   = only cars without one",)),
    "listed_within": ("Posted within", ("7   = the last 7 days (24 = the last 24 hours)",)),
    "engine": ("Engine size", ("1.6   = the 1301-1600 cm3 band, or a range: 1.4-2.0",)),
    "power": ("Horsepower", ("150   = the band 150 HP is in, or a range: 110-160",)),
    "drive": ("Drive", ("fwd", "4x4")),
    "warranty": ("Under warranty", ("yes",)),
    "plate": ("Plate", ("türkiye",)),
    "tradeable": ("Open to a trade-in", ("yes",)),
}
BASIC = ("condition", "fuel", "transmission", "body", "color", "seller", "heavy_damage")
MORE = ("engine", "power", "drive", "warranty", "plate", "tradeable")

#: Groups whose options are numeric bands, so a plain number picks its band.
BANDED = {"engine", "power"}


class Invalid(ValueError):
    """An answer that cannot be used, with what to type instead."""


def _english() -> dict[str, str]:
    """sahibinden's label -> the shortest English word that selects it."""
    out: dict[str, str] = {}
    for word, label in ENUM_SYNONYMS.items():
        if label not in out or len(word) < len(out[label]):
            out[label] = word
    return out


ENGLISH = _english()


def _numbers(label: str) -> list[int]:
    return [int(n) for n in re.findall(r"\d+", label.replace("cm3", ""))]


def _band(label: str) -> tuple[float, float] | None:
    """'126 - 150 HP' -> (126, 150); "50 HP'ye kadar" -> (0, 50)."""
    numbers = _numbers(label)
    if len(numbers) == 2:
        return numbers[0], numbers[1]
    if len(numbers) == 1:
        return (numbers[0], math.inf) if "üzeri" in label else (0, numbers[0])
    return None


def _in_order(label: str):
    """Bands by size, and 24 hours before 3 days; everything else by name."""
    numbers = _numbers(label)
    size = numbers[0] if numbers else 0
    return (size / 24 if "saat" in label else size, slugify(label))


def _unknown(kind: str, answer: str, names: list[str]) -> Invalid:
    """What to say about a name sahibinden does not list: the nearest ones."""
    by_slug = {slugify(n): n for n in names}
    near = get_close_matches(slugify(answer), list(by_slug), n=4, cutoff=0.6)
    return Invalid(f"unknown {kind} {answer!r}"
                   + (f". Did you mean {', '.join(by_slug[n] for n in near)}?"
                      if near else f". ? lists all {len(names)}"))


def _wrap(text: str) -> str:
    return textwrap.fill(text, width=78, initial_indent="    ",
                         subsequent_indent="    ")


def parse_amount(text: str) -> int:
    """'1.5m' -> 1500000, '150k' -> 150000, '1.500.000' -> 1500000."""
    t = text.strip().lower().replace(" ", "")
    scaled = re.fullmatch(r"(\d+(?:[.,]\d+)?)(k|m|bin|milyon)", t)
    if scaled:
        number = float(scaled.group(1).replace(",", "."))
        return round(number * (1_000 if scaled.group(2) in ("k", "bin") else 1_000_000))
    # A separator only ever groups thousands, so "1.5" is refused rather than
    # read as 15: guessing wrong there is off by a factor of 100,000.
    if re.fullmatch(r"\d{1,3}(?:[.,_]\d{3})+|\d+", t):
        return int(re.sub(r"[.,_]", "", t))
    raise Invalid(f"{text.strip()!r} is not a number. Write 1500000, 1.500.000, "
                  "1.5m or 150k")


def parse_range(text: str, bare: str) -> tuple[int | None, int | None]:
    """'500k-1.5m' -> (500000, 1500000). A bare number is a `bare` bound."""
    t = text.replace("–", "-").replace(" to ", "-")
    if "-" not in t:
        number = parse_amount(t)
        return (number, None) if bare == "min" else (None, number)
    low_text, _, high_text = t.partition("-")
    low = parse_amount(low_text) if low_text.strip() else None
    high = parse_amount(high_text) if high_text.strip() else None
    if low is None and high is None:
        raise Invalid("give at least one side of the range, e.g. 2018- or -2020")
    if low is not None and high is not None and low > high:
        raise Invalid(f"{low:,} is more than {high:,}: write the smaller one first")
    # 0 is no bound at all: a 0-150000 km range is "up to 150000".
    return low or None, high


class Questions:
    """One sitting of questions, building the `search` arguments as it goes."""

    def __init__(self, tax: Taxonomy, *, learn: Callable[[list[dict]], dict],
                 read: Callable[[str], str] = input,
                 write: Callable[[str], None] = print) -> None:
        self.tax, self.learn, self.read, self.write = tax, learn, read, write
        self.argv: list[str] = []
        self.chosen: list[tuple[str, str]] = []
        self.category = "otomobil"

    def take(self, label: str, shown: str, *argv: str) -> None:
        self.chosen.append((label, shown))
        self.argv.extend(argv)

    def ask(self, title: str, hints: list[str], parse, *, everything=(),
            required: str | None = None):
        """Ask until the answer parses; (value, shown), or None for Enter."""
        self.write("")
        self.write(f"  {title}")
        for hint in hints:
            self.write(_wrap(hint))
        while True:
            answer = self.read("  > ").strip()
            if not answer:
                if required:
                    self.write(f"    {required}")
                    continue
                return None
            if answer == "?":
                self.write(_wrap(", ".join(everything)) if everything else
                           "    there is no fixed list here: see the example above")
                continue
            try:
                value, shown = parse(answer)
            except Invalid as exc:
                for line in str(exc).splitlines():
                    self.write(_wrap(line))
                continue
            self.write(f"    = {shown}")
            return value, shown

    def yes(self, question: str, default: bool) -> bool:
        choices = "[Y/n]" if default else "[y/N]"
        while True:
            answer = slugify(self.read(f"\n  {question} {choices} "))
            if not answer:
                return default
            if answer in ("y", "yes", "e", "evet"):
                return True
            if answer in ("n", "no", "h", "hayir"):
                return False
            self.write("    type y or n")

    # ---------------------------------------------------------- the steps

    def brand(self) -> str:
        names = sorted(self.tax.brands, key=slugify)

        def parse(answer):
            name, ambiguous = self.tax.match_brand(answer, detail=True)
            if name:
                return name, name
            if ambiguous:
                raise Invalid(f"{answer!r} could be {', '.join(ambiguous)}: type more of it")
            raise _unknown("brand", answer, names)

        popular = [b for b in POPULAR_BRANDS if b in self.tax.brands]
        brand, _ = self.ask(
            "Brand (Marka)",
            [f"{len(names)} brands, e.g. {', '.join(popular)}. Short forms work: "
             "vw, merc"], parse, everything=names,
            required="a brand is needed: every search starts from a brand's page")
        self.take("brand", brand, "--brand", brand)
        return brand

    def model(self, brand: str, homes: list[str]) -> bool:
        """Ask for the model; True if one was given, which also fixes the category."""
        entries = {k: self.tax.categories[k]["models"].get(brand) or {} for k in homes}
        known = {k: sorted(e.get("models") or {}, key=slugify) for k, e in entries.items()}
        hints = []
        for key, names in known.items():
            if names:
                sample = ", ".join(names[:8]) + (", ..." if len(names) > 8 else "")
                hints.append(f"{CATEGORY_NAMES.get(key, key)}: {sample}")
        missing = [k for k, e in entries.items() if not e.get("complete")]
        if missing:
            hints.append(f"{brand}'s full model list isn't loaded yet. Naming a model "
                         f"reads it from sahibinden ({len(missing)} request(s), only "
                         "the first time)")
        hints.append("e.g. " + (next((n[0] for n in known.values() if n), "Corolla"))
                     + "   Enter = every model")

        def parse(answer):
            placed = self.tax.resolve_model(brand, answer, homes)
            if placed.get("learn"):
                self.write(f"    reading {brand}'s model list from sahibinden ...")
                learned = self.learn(placed["learn"])
                if learned.get("error"):
                    raise Invalid(f"could not read the model list: {learned['error']} "
                                  f"{learned.get('reason') or ''}\n"
                                  f"Enter searches every {brand} instead")
                self.tax = Taxonomy(self.tax.path)
                placed = self.tax.resolve_model(brand, answer, homes)
            if placed.get("learn"):
                raise Invalid(f"sahibinden's {brand} page listed no models")
            if placed.get("error"):
                text = placed["error"]
                if placed.get("did_you_mean"):
                    text += f". Did you mean {', '.join(placed['did_you_mean'])}?"
                raise Invalid(text + "\n? lists every model")
            where = self.tax.category_label(placed["category"])
            return placed, f"{placed['model']} ({where})"

        everything = [f"{n} ({CATEGORY_NAMES.get(k, k)})" for k, names in known.items()
                      for n in names]
        answer = self.ask(f"Model of {brand}", hints, parse, everything=everything)
        if answer is None:
            return False
        placed, shown = answer
        self.category = placed["category"]
        self.take("model", shown, "--model", placed["model"])
        return True

    def choose_category(self, brand: str, homes: list[str]) -> None:
        """Cars or SUVs, when the brand has both and no model settled it."""
        self.category = homes[0]
        if len(homes) < 2:
            return
        names = [CATEGORY_NAMES.get(k, k) for k in homes]

        def parse(answer):
            key = CATEGORY_ALIASES.get(answer.strip().lower(), slugify(answer))
            if key not in homes:
                raise Invalid(f"choose one of: {', '.join(names)}")
            return key, self.tax.category_label(key)

        answer = self.ask(
            "Category (Kategori)",
            [f"{brand} is listed under "
             + ", ".join(f"{CATEGORY_NAMES.get(k, k)} ({self.tax.category_label(k)})"
                         for k in homes),
             f"Enter = {names[0]}"], parse, everything=names)
        if answer and answer[0] != homes[0]:
            self.category = answer[0]
            self.take("category", answer[1], "--category",
                      CATEGORY_NAMES.get(answer[0], answer[0]))

    def city(self) -> bool:
        names = sorted(self.tax.cities, key=slugify)

        def parse(answer):
            name, ambiguous = self.tax.match_city(answer, detail=True)
            if name:
                return name, name
            if ambiguous:
                raise Invalid(f"{answer!r} could be {', '.join(ambiguous)}: type more of it")
            raise _unknown("province", answer, names)

        answer = self.ask("City (İl)",
                          [f"{len(names)} provinces, e.g. {', '.join(POPULAR_CITIES)}. "
                           "Turkish letters are optional: izmir"],
                          parse, everything=names)
        if answer:
            self.take("city", answer[1], "--city", answer[0])
        return answer is not None

    def district(self) -> None:
        answer = self.ask(
            "District (İlçe)",
            ["e.g. Kadıköy, Çankaya. Not checked until the search runs: a "
             "misspelled one finds nothing"],
            lambda a: (a, a))
        if answer:
            self.take("district", answer[1], "--district", answer[0])

    def ranges(self) -> None:
        year = self.tax.categories[self.category]["filters"].get("Yıl") or {}
        earliest = int(year.get("min") or 1900)
        latest = max(int(year.get("max") or 0), dt.date.today().year + 1)

        def prices(answer):
            bounds = parse_range(answer, "max")
            return bounds, span(*bounds, "TL")

        def years(answer):
            bounds = parse_range(answer, "min")
            for value in bounds:
                if value is not None and not earliest <= value <= latest:
                    raise Invalid(f"{value} is not a model year ({earliest}-{latest})")
            return bounds, span(*bounds, grouped=False)

        def kilometres(answer):
            bounds = parse_range(answer, "max")
            return bounds, span(*bounds, "km")

        steps = (
            ("Price (Fiyat)", "price",
             ["from-to, in TL. k and m work: 500k-1.5m",
              "-1.5m = up to 1.5 million, 750k- = from 750,000. One number "
              "means 'up to'"], prices),
            ("Year (Yıl)", "year",
             ["e.g. 2018-2022, or 2018 for 2018 and newer, -2020 for 2020 and older"],
             years),
            ("Kilometres (KM)", "km",
             ["e.g. 150k for up to 150,000 km, or 50k-120k"], kilometres),
        )
        for title, name, hints, parse in steps:
            answer = self.ask(title, hints, parse)
            if not answer:
                continue
            (low, high), shown = answer
            argv = []
            if low is not None:
                argv += [f"--min-{name}", str(low)]
            if high is not None:
                argv += [f"--max-{name}", str(high)]
            self.take(name, shown, *argv)

    def choice(self, flag: str) -> None:
        """One of the sidebar's checkbox or radio groups."""
        group = self.tax.enum_group(flag, self.category)
        spec = self.tax.categories[self.category]["enums"].get(group)
        if not spec:
            return  # not offered for this category
        # Bands sort by size; a list that only mentions a number here and there
        # ("Hatchback 5 kapı") stays alphabetical.
        numbered = all(_numbers(o) for o in spec["options"])
        options = sorted(spec["options"], key=_in_order if numbered else slugify)
        multi = bool(spec.get("multi"))
        shown = [f"{o} ({ENGLISH[o]})" if o in ENGLISH else o for o in options]
        if len(shown) > 18:
            listed = (", ".join(shown[:4]) + ", ... " + ", ".join(shown[-2:])
                      + f"  ({len(shown)} in all, ? lists them)")
        else:
            listed = ", ".join(shown)
        title, examples = CHOICES[flag]

        def parse(answer):
            tokens = [t.strip() for t in answer.split(",") if t.strip()]
            values = []
            for token in tokens:
                values.extend(self._shortcut(flag, token, options) or [token])
            resolved = self.tax.resolve_enum(flag, values, self.category)
            if resolved.get("error"):
                text = resolved["error"]
                if resolved.get("options"):
                    text += f". Valid: {', '.join(map(str, resolved['options']))}"
                raise Invalid(text)
            return resolved["labels"], ", ".join(resolved["labels"])

        def works(example):
            try:
                parse(example.split()[0])
                return True
            except Invalid:
                return False

        example = next((e for e in examples if works(e)), examples[0])
        answer = self.ask(
            f"{title} ({group}), {'one or more' if multi else 'pick one'}",
            [listed, f"e.g. {example}"], parse, everything=shown)
        if answer:
            labels, text = answer
            argv = []
            for label in labels:
                argv += [f"--{flag.replace('_', '-')}", label]
            self.take(title.lower(), text, *argv)

    @staticmethod
    def _shortcut(flag: str, token: str, options: list[str]) -> list[str] | None:
        """A number standing for the options it names: 150 HP, 1.6 litres, 7 days."""
        if flag == "listed_within" and token.isdigit():
            return [o for o in options if token in map(str, _numbers(o))] or None
        if flag not in BANDED:
            return None
        found = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:-\s*(\d+(?:\.\d+)?))?", token)
        if not found:
            return None

        def size(text):
            value = float(text)
            # Engines are written in litres as often as in cm3.
            return value * 1000 if flag == "engine" and value < 10 else value

        low = size(found.group(1))
        high = size(found.group(2)) if found.group(2) else low
        return [o for o in options
                if (b := _band(o)) and b[0] <= high and b[1] >= low] or None

    def paint(self) -> None:
        words = {"unpainted": "unpainted", "boyasiz": "unpainted",
                 "unchanged": "unchanged", "degisensiz": "unchanged",
                 "both": "both", "ikisi": "both"}

        def parse(answer):
            picked = set()
            for token in answer.split(","):
                slug = slugify(token)
                hits = {v for w, v in words.items() if slug and w.startswith(slug)}
                if len(hits) != 1:
                    raise Invalid(f"{token.strip()!r}: write unpainted, unchanged or both")
                picked |= {"unpainted", "unchanged"} if hits == {"both"} else hits
            if picked == {"unpainted", "unchanged"}:
                return ["--undamaged"], "no painted and no replaced parts"
            if picked == {"unpainted"}:
                return ["--unpainted"], "no painted parts (Boyasız)"
            return ["--unchanged"], "no replaced parts (Değişensiz)"

        answer = self.ask(
            "Paint and replaced parts (Boya, Değişen Parça)",
            ["unpainted (Boyasız), unchanged (Değişensiz), both",
             "e.g. both   = no painted and no replaced parts"], parse)
        if answer:
            self.take("paint", answer[1], *answer[0])

    def keyword(self) -> None:
        answer = self.ask("Words in the title (Kelime ile filtrele)",
                          ["e.g. cam tavan"], lambda a: (a, a))
        if answer:
            self.take("title has", answer[1], "--keyword", answer[0])

    def media(self) -> None:
        kinds = {"video": ("--has-video", "has a video"),
                 "clip": ("--has-clip", "has a clip"),
                 "klip": ("--has-clip", "has a clip")}

        def parse(answer):
            picked = []
            for token in answer.split(","):
                slug = slugify(token)
                hit = next((kinds[w] for w in kinds if slug and w.startswith(slug)), None)
                if hit is None:
                    raise Invalid(f"{token.strip()!r}: write video, clip, or both: video, clip")
                if hit not in picked:
                    picked.append(hit)
            return [flag for flag, _ in picked], ", ".join(text for _, text in picked)

        answer = self.ask("Photos and video (Fotoğraf, Video)",
                          ["video (Videolu ilanlar), clip (Klipli ilanlar)",
                           "e.g. video"], parse)
        if answer:
            self.take("media", answer[1], *answer[0])

    def body_parts(self) -> None:
        """Per-part rules: the filter sahibinden itself does not have."""
        self.write("")
        self.write("  Body parts (Boya, Değişen)")
        for line in (
                "sahibinden can only ask for \"nothing painted\" or \"nothing "
                "replaced\" on the whole car. Here you set rules for the parts you "
                "care about and leave the rest free. The CLI reads each car's "
                "damage diagram from its own page for this (1 request per car).",
                "Parts: hood, roof, trunk, front bumper, rear bumper, and "
                "front/rear left/right fender and door (e.g. front left fender). "
                "Turkish works too: kaput, tavan, sol ön çamurluk.",
                "Groups: structural (every part but the bumpers), doors, fenders, "
                "bumpers, front, rear, left, right, all."):
            self.write(_wrap(line))
        everything = ([f"{panel_name(p)} ({PANEL_LABELS[p]})" for p in PANELS]
                      + list(PART_GROUPS))

        for flag, title, example, meaning in PART_RULES:
            def parse(answer, meaning=meaning):
                keys, shown = [], []
                for token in (t for t in answer.split(",") if t.strip()):
                    panels, error = resolve_panels(token)
                    if error:
                        near = error["options"] if len(error["options"]) <= 4 else []
                        raise Invalid(f"unknown part {token.strip()!r}. "
                                      + (f"Did you mean {', '.join(near)}?" if near
                                         else "? lists every part and group"))
                    key = slugify(token)
                    if key in PANEL_GROUPS:
                        keys.append(key)
                        shown.append(f"{key} ({len(panels)} parts)")
                    else:
                        keys.append(panels[0])
                        shown.append(panel_name(panels[0]))
                return keys, f"{', '.join(shown)}: {meaning}"

            answer = self.ask(title, [f"e.g. {example}"], parse, everything=everything)
            if answer:
                keys, shown = answer
                argv = []
                for key in keys:
                    argv += [f"--{flag}", key]
                self.take("body parts", shown, *argv)

        for flag, title, example, noun in (
                ("max-painted", "Most painted parts allowed on the whole car",
                 "2   (painted or local paint)", "painted"),
                ("max-changed", "Most replaced parts allowed on the whole car",
                 "0", "replaced")):
            def parse(answer, noun=noun):
                if not answer.isdigit():
                    raise Invalid("write a number, e.g. 0")
                return int(answer), f"at most {answer} {noun}"

            answer = self.ask(title, [f"e.g. {example}"], parse)
            if answer:
                self.take("body parts", answer[1], f"--{flag}", str(answer[0]))

    def diagrams(self, budget: int, pages: int, cap: int) -> int:
        """How many result pages to open for their damage diagrams."""
        most = max(0, min(cap, budget - pages))
        rules = any(label == "body parts" for label, _ in self.chosen)

        def parse(answer):
            if not answer.isdigit() or int(answer) > most:
                raise Invalid(f"write a number from 0 to {most}")
            n = int(answer)
            return n, (f"up to {n}" if n else "none: only diagrams read on "
                       "earlier searches")

        answer = self.ask(
            "Cars to open for their damage diagram",
            ["1 request each, taking the results in the search's order. The "
             "body-part columns" + (" and rules" if rules else "")
             + " need them. Diagrams read on earlier searches are reused for free, "
             "so running the same search again opens the next cars.",
             f"at most {cap} per search; Enter = {most}"], parse)
        checks = answer[0] if answer else most
        self.take("damage diagrams", answer[1] if answer else f"up to {most}",
                  "--check", str(checks))
        return checks

    def currency(self) -> None:
        units = self.tax.categories[self.category]["units"].get("price_currency") or {}
        if not units:
            return

        def parse(answer):
            resolved = self.tax.resolve_currency(answer, self.category)
            if resolved.get("error"):
                raise Invalid(f"{resolved['error']}. Valid: {', '.join(sorted(units))}")
            return resolved["label"], resolved["label"]

        answer = self.ask("Price currency",
                          [f"{', '.join(sorted(units))}. The price you gave is read in it",
                           "e.g. usd   Enter = TL"], parse, everything=sorted(units))
        if answer and answer[0] != "TL":
            # The price was echoed in lira when it was typed; say what it means now.
            self.chosen = [(label, shown.replace(" TL", f" {answer[0]}")
                            if label == "price" else shown)
                           for label, shown in self.chosen]
            self.take("currency", answer[1], "--currency", answer[0])

    def order(self) -> None:
        def parse(answer):
            slug = slugify(answer).replace("-", "_")
            if slug in SORTS.values() and slug != "condition":
                return slug, slug
            hits = [w for w in SORTS if w.startswith(slug)]
            if len(hits) != 1:
                raise Invalid(f"write one of: {', '.join(SORTS)}")
            if hits[0] == "condition":
                return "condition", "best condition first (the cheapest are read)"
            return SORTS[hits[0]], f"{hits[0]} first"

        answer = self.ask("Order (Sıralama)",
                          [f"{', '.join(SORTS)}   Enter = cheapest first",
                           "condition = best body condition first, among the "
                           "cheapest cars"],
                          parse, everything=list(SORTS))
        if answer and answer[0] == "condition":
            self.take("order", answer[1], "--best-first")
        elif answer and answer[0] != "price_asc":
            self.take("order", answer[1], "--sort", answer[0])

    def pages(self, budget: int, default: int) -> int:
        most = max(1, min(50, budget))
        default = min(default, most)

        def parse(answer):
            if not answer.isdigit() or not 1 <= int(answer) <= most:
                raise Invalid(f"write a number from 1 to {most}")
            n = int(answer)
            return n, f"{n} page(s), up to {n * 20} listings"

        answer = self.ask(
            "Pages to read (20 listings each)",
            [f"each page is one request, and {budget} are left today",
             f"Enter = {default}"], parse)
        pages = answer[0] if answer else default
        if answer and pages != default:
            self.take("pages", answer[1], "--pages", str(pages))
        return pages


def ask_search(tax: Taxonomy, *, budget: Callable[[], int], default_pages: int,
               learn: Callable[[list[dict]], dict], detail_cap: int = 15,
               seconds_per_request: float = 18.0,
               read: Callable[[str], str] = input,
               write: Callable[[str], None] = print) -> list[str] | None:
    """Ask for the filters, and return them as `search` arguments.

    None means the search was declined at the end, after it was shown.
    """
    q = Questions(tax, learn=learn, read=read, write=write)
    write("\n  Set the filters you care about, as on the website. Enter skips a "
          "question,\n  ? lists every value it takes, Ctrl+C stops.")

    brand = q.brand()
    homes = [k for k in tax.resolve_category(None)[0]
             if brand in tax.categories[k]["brands"]]
    if not q.model(brand, homes):
        q.choose_category(brand, homes)
    if q.city():
        q.district()
    q.ranges()
    for flag in BASIC:
        q.choice(flag)
    q.paint()
    q.choice("listed_within")
    q.keyword()
    q.body_parts()

    if q.yes("More filters (engine, horsepower, drive, warranty, plate, trade-in, "
             "video, currency)?", default=False):
        for flag in MORE:
            q.choice(flag)
        q.media()
        if any(a in q.argv for a in ("--min-price", "--max-price")):
            q.currency()

    q.order()
    left = budget()
    pages = q.pages(left, default_pages)
    checks = q.diagrams(left, pages, detail_cap)

    argv = q.argv + ["--markdown"]
    write("\n  Your search")
    for label, shown in q.chosen:
        write(f"    {label:<20} {shown}")
    requests = pages + checks
    minutes = max(1, math.ceil(requests * seconds_per_request / 60))
    if not q.yes(f"Search now? It uses up to {pages} + {checks} = {requests} of "
                 f"today's {left} requests, about {minutes} minutes.", default=True):
        return None
    return argv
