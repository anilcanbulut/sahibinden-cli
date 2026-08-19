"""Per-panel damage filtering: the 13-part diagram as a query.

sahibinden's sidebar offers exactly two paint filters — Boyasız (nothing
painted anywhere) and Değişensiz (nothing replaced anywhere) — and both are
all-or-nothing. The distinction that actually decides what a car is worth is
not there: a painted rear bumper is a parking scrape, a changed front fender
or roof is a structural repair, and "Boyasız" throws both away together.

The diagram carries that per panel, but only on the detail page. So this
filter cannot go in the search URL; it runs over stored detail records, and
what it can see is limited to the listings whose details have been fetched.

Which is why a listing with no diagram never passes a panel filter and is
never counted as a failure either. It is reported as *unchecked*, separately,
with the command that would fill it in — treating "not looked at" as "clean"
would be the worst failure mode this dataset has, and treating it as "damaged"
would hide the cars most worth checking.
"""

from __future__ import annotations

from difflib import get_close_matches

from .parse.detail import PART_SLUGS
from .taxonomy import slugify

#: The 13 panels, in the order sahibinden draws them. Single-sourced from the
#: parser so a diagram change cannot leave the two disagreeing.
PANELS: tuple[str, ...] = tuple(PART_SLUGS.values())

#: Display labels. Unlike the filter ids these are ours, not harvested: the
#: diagram labels its panels by position in the drawing, not in text.
PANEL_LABELS = {
    "front_bumper": "ön tampon",
    "hood": "kaput",
    "roof": "tavan",
    "front_right_fender": "sağ ön çamurluk",
    "front_right_door": "sağ ön kapı",
    "rear_right_door": "sağ arka kapı",
    "rear_right_fender": "sağ arka çamurluk",
    "front_left_fender": "sol ön çamurluk",
    "front_left_door": "sol ön kapı",
    "rear_left_door": "sol arka kapı",
    "rear_left_fender": "sol arka çamurluk",
    "trunk": "bagaj kapağı",
    "rear_bumper": "arka tampon",
}

#: One panel under another name. Turkish included, because the diagram is.
PANEL_ALIASES = {
    "bonnet": "hood", "kaput": "hood", "motor-kaputu": "hood",
    "boot": "trunk", "bagaj": "trunk", "bagaj-kapagi": "trunk",
    "rear-hood": "trunk", "arka-kaput": "trunk",
    "tavan": "roof",
    "on-tampon": "front_bumper", "arka-tampon": "rear_bumper",
    "sag-on-camurluk": "front_right_fender",
    "sag-arka-camurluk": "rear_right_fender",
    "sol-on-camurluk": "front_left_fender",
    "sol-arka-camurluk": "rear_left_fender",
    "sag-on-kapi": "front_right_door",
    "sag-arka-kapi": "rear_right_door",
    "sol-on-kapi": "front_left_door",
    "sol-arka-kapi": "rear_left_door",
}

#: Names for sets of panels, so a structural check is one argument rather than
#: eleven. "structural" is every panel except the two bumpers: bumpers are
#: plastic, bolted on, and repainted after any car-park knock, so requiring
#: them original mostly filters out honest cars.
PANEL_GROUPS = {
    "all": list(PANELS),
    "structural": [p for p in PANELS if not p.endswith("bumper")],
    "bumpers": ["front_bumper", "rear_bumper"],
    "tamponlar": ["front_bumper", "rear_bumper"],
    "doors": [p for p in PANELS if p.endswith("door")],
    "kapilar": [p for p in PANELS if p.endswith("door")],
    "fenders": [p for p in PANELS if p.endswith("fender")],
    "camurluklar": [p for p in PANELS if p.endswith("fender")],
    "left": [p for p in PANELS if "_left_" in p],
    "sol": [p for p in PANELS if "_left_" in p],
    "right": [p for p in PANELS if "_right_" in p],
    "sag": [p for p in PANELS if "_right_" in p],
    "front": ["front_bumper", "hood", "front_right_fender", "front_left_fender",
              "front_right_door", "front_left_door"],
    "on": ["front_bumper", "hood", "front_right_fender", "front_left_fender",
           "front_right_door", "front_left_door"],
    "rear": ["rear_bumper", "trunk", "rear_right_fender", "rear_left_fender",
             "rear_right_door", "rear_left_door"],
    "arka": ["rear_bumper", "trunk", "rear_right_fender", "rear_left_fender",
             "rear_right_door", "rear_left_door"],
}

#: Worst-first, because that is the order the question is usually asked in.
#: "unknown" is a real state: the panel is in the diagram but its marking did
#: not parse, which is not the same as the panel being fine.
STATUSES = ("original", "local_painted", "painted", "changed", "unknown")

STATUS_ALIASES = {
    "orijinal": "original", "orjinal": "original", "clean": "original",
    "untouched": "original", "none": "original", "ok": "original",
    "local": "local_painted", "lokal": "local_painted",
    "lokal-boyali": "local_painted", "local-paint": "local_painted",
    "localpainted": "local_painted",
    "boyali": "painted", "boya": "painted", "paint": "painted",
    "degisen": "changed", "degismis": "changed", "replaced": "changed",
    "swapped": "changed",
    "bilinmiyor": "unknown",
}

#: What each convenience flag allows. --not-changed is the loose one people
#: actually want for bumpers: paint is cosmetic, a replaced panel is not.
PRESETS = {
    "original": {"original"},
    "not_changed": {"original", "local_painted", "painted"},
    "not_painted": {"original", "local_painted"},
}


def resolve_panels(value: str) -> tuple[list[str], dict | None]:
    """'hood', 'kaput', 'doors', 'structural' -> panel slugs, or an error."""
    key = slugify(value)
    if key in PANEL_GROUPS:
        return list(PANEL_GROUPS[key]), None
    if key in PANEL_ALIASES:
        return [PANEL_ALIASES[key]], None

    direct = key.replace("-", "_")
    if direct in PANELS:
        return [direct], None

    known = list(PANELS) + sorted(PANEL_GROUPS) + sorted(PANEL_ALIASES)
    near = get_close_matches(direct, [k.replace("-", "_") for k in known],
                             n=4, cutoff=0.6)
    return [], {"error": f"unknown panel {value!r}",
                "options": near or sorted(PANELS) + sorted(PANEL_GROUPS)}


def resolve_status(value: str) -> tuple[str | None, dict | None]:
    """'boyali', 'painted', 'changed' -> a diagram status, or an error."""
    key = slugify(value)
    if key in STATUS_ALIASES:
        return STATUS_ALIASES[key], None
    direct = key.replace("-", "_")
    if direct in STATUSES:
        return direct, None
    return None, {"error": f"unknown panel status {value!r}",
                  "options": list(STATUSES)}


def parse_requirement(spec: str) -> tuple[dict[str, set[str]], dict | None]:
    """'hood=original' or 'doors=original,local_painted' -> {panel: statuses}."""
    if "=" not in spec:
        return {}, {"error": f"--panel wants PANEL=STATUS, got {spec!r}",
                    "options": [f"{p}=original" for p in ("hood", "roof", "trunk")]}
    name, _, statuses = spec.partition("=")
    panels, error = resolve_panels(name)
    if error:
        return {}, error

    allowed: set[str] = set()
    for piece in statuses.split(","):
        if not piece.strip():
            continue
        status, error = resolve_status(piece)
        if error:
            return {}, error
        allowed.add(status)
    if not allowed:
        return {}, {"error": f"no status given in {spec!r}",
                    "options": list(STATUSES)}
    return {panel: allowed for panel in panels}, None


def build_requirements(*, panel: list[str] | None = None,
                       original: list[str] | None = None,
                       not_changed: list[str] | None = None,
                       not_painted: list[str] | None = None,
                       ) -> tuple[dict[str, set[str]], dict | None]:
    """Collect every panel argument into one {panel: allowed statuses} map.

    Two arguments naming the same panel intersect rather than overwrite, so
    ``--original hood --not-changed structural`` keeps the stricter of the two
    for the hood. An impossible intersection is an error, not an empty result
    that would quietly match nothing.
    """
    requirements: dict[str, set[str]] = {}

    def tighten(panel_name: str, allowed: set[str], source: str) -> dict | None:
        if panel_name in requirements:
            merged = requirements[panel_name] & allowed
            if not merged:
                return {"error": f"conflicting requirements for {panel_name}: "
                                 f"{sorted(requirements[panel_name])} and "
                                 f"{sorted(allowed)} ({source}) cannot both hold"}
            requirements[panel_name] = merged
        else:
            requirements[panel_name] = set(allowed)
        return None

    for spec in panel or []:
        parsed, error = parse_requirement(spec)
        if error:
            return {}, error
        for panel_name, allowed in parsed.items():
            error = tighten(panel_name, allowed, spec)
            if error:
                return {}, error

    for flag, values in (("original", original), ("not_changed", not_changed),
                         ("not_painted", not_painted)):
        for value in values or []:
            names, error = resolve_panels(value)
            if error:
                return {}, error
            for panel_name in names:
                error = tighten(panel_name, PRESETS[flag],
                                f"--{flag.replace('_', '-')} {value}")
                if error:
                    return {}, error

    return requirements, None


def evaluate(parts: dict[str, str] | None, requirements: dict[str, set[str]],
             *, max_painted: int | None = None,
             max_changed: int | None = None) -> str:
    """'pass', 'fail' or 'unchecked' for one listing's diagram.

    'unchecked' means the question cannot be answered for this car — no detail
    fetched, or the diagram did not carry the panel that was asked about. It is
    deliberately not folded into 'fail': those are the cars to spend a detail
    request on, and a caller that cannot tell them apart would either discard
    good cars or count unseen ones as clean.
    """
    if not parts:
        return "unchecked"

    for panel, allowed in requirements.items():
        status = parts.get(panel)
        if status is None:
            return "unchecked"
        if status == "unknown" and "unknown" not in allowed:
            return "unchecked"
        if status not in allowed:
            return "fail"

    if max_painted is not None or max_changed is not None:
        if len(parts) < len(PANELS):
            return "unchecked"
        if max_painted is not None:
            painted = sum(1 for s in parts.values() if s in ("painted", "local_painted"))
            if painted > max_painted:
                return "fail"
        if max_changed is not None:
            changed = sum(1 for s in parts.values() if s == "changed")
            if changed > max_changed:
                return "fail"
    return "pass"


def describe(requirements: dict[str, set[str]]) -> list[str]:
    """Human phrasing: one line per distinct rule, not per panel.

    A structural check covers eleven panels; printing eleven identical lines
    would bury the one line that differs, which is usually the whole point of
    the query ("everything original except the bumpers").
    """
    by_allowed: dict[tuple[str, ...], list[str]] = {}
    for panel in PANELS:
        if panel in requirements:
            ordered = tuple(s for s in STATUSES if s in requirements[panel])
            by_allowed.setdefault(ordered, []).append(panel)

    group_names: dict[tuple[str, ...], str] = {}
    for name, members in PANEL_GROUPS.items():
        group_names.setdefault(tuple(members), name)

    def name(panel: str) -> str:
        return f"{PANEL_LABELS.get(panel, panel)} [{panel}]"

    lines = []
    for allowed, panels in by_allowed.items():
        group = group_names.get(tuple(panels))
        missing = [p for p in PANELS if p not in panels]
        if group:
            subject = f"{group} ({len(panels)} panels)"
        elif len(panels) <= 4:
            subject = ", ".join(name(p) for p in panels)
        elif len(missing) <= 3:
            # A rule that covers nearly everything reads better as what it
            # leaves out — which is usually the panel the query is really about.
            subject = (f"{len(panels)} panels, all except "
                       + ", ".join(name(p) for p in missing))
        else:
            subject = (f"{len(panels)} panels: "
                       + ", ".join(name(p) for p in panels[:3])
                       + f", +{len(panels) - 3} more")
        lines.append(f"{subject}: {' or '.join(allowed)}")
    return lines


def summarise(parts: dict[str, str] | None) -> str:
    """'11 orijinal, 2 boyalı' — one line for a results table."""
    if not parts:
        return "?"
    counts = {status: sum(1 for s in parts.values() if s == status)
              for status in STATUSES}
    shown = {"original": "orij", "local_painted": "lokal",
             "painted": "boya", "changed": "değişen", "unknown": "?"}
    return " ".join(f"{counts[s]}{shown[s]}" for s in STATUSES if counts[s])
