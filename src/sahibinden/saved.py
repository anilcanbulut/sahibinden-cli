"""Searches kept as JSON files, so a set of filters is typed once.

A file holds a `search`'s flags by their own names, without the dashes:

    {"brand": "Skoda", "model": "Scala", "max-price": 1300000,
     "fuel": ["Benzin"], "original": ["hood", "roof"], "markdown": true}

It is read through the `search` parser itself rather than a schema of its own,
so every option `search` takes can be saved, and a value is checked exactly as
it would be on the command line. `ask` writes one for every search it runs.
"""

from __future__ import annotations

import argparse
import json
from difflib import get_close_matches
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Next to data/ but not in it: data/ stays on the PC it was made on, while a
#: saved search is worth carrying to the next one with the rest of the repo.
SEARCHES_DIR = PROJECT_ROOT / "searches"

#: Options a file cannot hold: a file naming another file goes in circles.
NOT_SAVED = {"help", "filters"}


def _options(search: argparse.ArgumentParser) -> dict[str, argparse.Action]:
    """'max-price' -> its action, for every option `search` takes."""
    return {a.option_strings[-1].lstrip("-"): a for a in search._actions  # noqa: SLF001
            if a.option_strings and a.option_strings[-1].lstrip("-") not in NOT_SAVED}


def _is_switch(action: argparse.Action) -> bool:
    return action.nargs == 0


def _is_list(action: argparse.Action) -> bool:
    return isinstance(action, argparse._AppendAction)  # noqa: SLF001


def to_file(argv: list[str], search: argparse.ArgumentParser) -> dict:
    """`search` arguments as the dict a file holds, lists for repeatable flags."""
    options = _options(search)
    data: dict = {}
    tokens = iter(argv)
    for token in tokens:
        name = token.lstrip("-")
        action = options[name]
        if _is_switch(action):
            data[name] = True
            continue
        value = next(tokens)
        if action.type in (int, float):
            value = action.type(value)
        if _is_list(action):
            data.setdefault(name, []).append(value)
        else:
            data[name] = value
    return data


def to_argv(data: dict, search: argparse.ArgumentParser, *, source: str) -> list[str]:
    """A file's dict as `search` arguments, refusing anything `search` would not take."""
    if not isinstance(data, dict):
        raise ValueError(f"{source}: expected {{\"option\": value, ...}}")
    options = _options(search)
    argv: list[str] = []
    for key, value in data.items():
        name = str(key).replace("_", "-")
        action = options.get(name)
        if action is None:
            near = get_close_matches(name, list(options), n=3, cutoff=0.6)
            raise ValueError(f"{source}: {key!r} is not a search option"
                             + (f" (did you mean {', '.join(near)}?)" if near else
                                "; see: sahibinden-cli search --help"))
        flag = f"--{name}"
        if _is_switch(action):
            if not isinstance(value, bool):
                raise ValueError(f"{source}: {key!r} is a switch: write true or false")
            if value:
                argv.append(flag)
            continue
        values = value if isinstance(value, list) else [value]
        if len(values) > 1 and not _is_list(action):
            raise ValueError(f"{source}: {key!r} takes one value, not a list")
        for item in values:
            if action.type in (int, float):
                try:
                    action.type(item)
                except (TypeError, ValueError):
                    raise ValueError(f"{source}: {key!r} must be a number, "
                                     f"got {item!r}") from None
            argv += [flag, str(item)]
    return argv


def locate(name: str) -> Path:
    """A path as given, or a bare name looked up in searches/."""
    path = Path(name)
    if path.exists() or path.suffix or len(path.parts) > 1:
        return path
    return SEARCHES_DIR / f"{name}.json"


def load(name: str, search: argparse.ArgumentParser) -> list[str]:
    path = locate(name)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        saved = sorted(p.stem for p in SEARCHES_DIR.glob("*.json"))
        raise ValueError(f"no saved search at {path}"
                         + (f". Saved: {', '.join(saved)}" if saved else "")) from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON: {exc.msg} "
                         f"(line {exc.lineno}, column {exc.colno})") from None
    return to_argv(data, search, source=path.name)


def save(argv: list[str], search: argparse.ArgumentParser, name: str) -> Path:
    """Write a search under searches/, never over a different one.

    The same filters saved again land on the file already holding them. A
    different search under a taken name gets the next number rather than
    replacing it, since that file may have been edited by hand.
    """
    SEARCHES_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(to_file(argv, search), ensure_ascii=False, indent=2) + "\n"
    for n in range(1, 1000):
        path = SEARCHES_DIR / (f"{name}.json" if n == 1 else f"{name}-{n}.json")
        if not path.exists():
            path.write_text(text, encoding="utf-8")
            return path
        if path.read_text(encoding="utf-8") == text:
            return path
    raise ValueError(f"too many saved searches named {name}")
