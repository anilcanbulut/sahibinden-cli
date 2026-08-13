"""Runtime configuration: file-backed, env-overridable, CLI-overridable.

Precedence, lowest to highest:

    dataclass defaults  <  data/config.json  <  SAHIBINDEN_* env vars  <  CLI flags

Defaults are calibrated from the operator's own history with the site rather
than guessed: bans there were rate-triggered, near-instant under fast
gathering, IP-wide, and self-releasing within roughly an hour. Because the ban
is IP-wide, tripping it costs the operator their own browsing too — so the
budget starts far below any suspected ceiling and should only be raised on
evidence of clean runs.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "data" / "config.json"
ENV_PREFIX = "SAHIBINDEN_"


@dataclass
class Config:
    #: Total page loads per day across every tool. One search sweep of a
    #: narrow model is ~19 pages.
    daily_budget: int = 40

    #: Cap, within daily_budget, on detail pages. Search pages return ~20
    #: listings each; detail pages return one. Uncapped, damage-record
    #: fetching would eat the whole budget in a single run.
    detail_budget: int = 15

    #: Seconds between navigations, before the breaker's backoff multiplier.
    #: Jittered — a fixed interval is a signature in itself.
    min_delay: float = 12.0
    max_delay: float = 25.0

    #: Preferred crawl window, local time. A trip inside it costs nothing,
    #: since the IP block lifts before the operator is awake.
    quiet_start: int = 1
    quiet_end: int = 7

    #: Default pages per sweep when the caller does not say. Bounded because
    #: a broad search ("volkswagen-golf" = 583 pages) would otherwise consume
    #: everything.
    default_pages: int = 5

    #: sahibinden stops offering pages past this row offset.
    max_offset: int = 1000

    #: Accept-Language, measured from the operator's real Chrome so the
    #: automated browser is not distinguishable from it.
    accept_languages: str = "en-US,en,tr"

    db_path: str = str(PROJECT_ROOT / "data" / "sahibinden.db")
    profile_dir: str = str(PROJECT_ROOT / "browser-profile")

    _source: str = field(default="defaults", repr=False)

    # ------------------------------------------------------------ loading

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = Path(path or CONFIG_PATH)
        values: dict = {}
        source = "defaults"

        if path.exists():
            values = json.loads(path.read_text(encoding="utf-8"))
            values.pop("_source", None)
            source = str(path)

        # Types come from each field's default, not its annotation: with
        # `from __future__ import annotations` every annotation is a *string*
        # ("int"), so comparing it to int always failed and every env value
        # stayed a string — which only surfaced later as a TypeError doing
        # arithmetic on the budget.
        known = {f.name: type(f.default) for f in fields(cls)
                 if not f.name.startswith("_")}
        for name in known:
            env = os.environ.get(f"{ENV_PREFIX}{name.upper()}")
            if env is not None:
                values[name] = env
                source += " + env"

        # JSON and env both hand us strings; coerce to the declared types.
        coerced = {}
        for name, raw in values.items():
            if name not in known:
                continue
            target = known[name]
            try:
                if target is int:
                    coerced[name] = int(raw)
                elif target is float:
                    coerced[name] = float(raw)
                else:
                    coerced[name] = str(raw)
            except (TypeError, ValueError):
                continue  # keep the default rather than crash on a bad value

        return cls(**coerced, _source=source)

    def save(self, path: Path | None = None) -> Path:
        path = Path(path or CONFIG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def override(self, **kwargs) -> "Config":
        """Apply CLI flags. None values mean 'not supplied', so they are ignored."""
        supplied = {k: v for k, v in kwargs.items() if v is not None}
        if not supplied:
            return self
        data = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        data.update(supplied)
        return Config(**data, _source=f"{self._source} + cli")

    # ------------------------------------------------------------ helpers

    @property
    def quiet_hours(self) -> range:
        return range(self.quiet_start, self.quiet_end)

    def in_quiet_hours(self, now: dt.datetime | None = None) -> bool:
        return (now or dt.datetime.now()).hour in self.quiet_hours

    def describe(self) -> str:
        return (
            f"budget {self.daily_budget}/day (detail cap {self.detail_budget}), "
            f"delay {self.min_delay:.0f}-{self.max_delay:.0f}s, "
            f"quiet {self.quiet_start:02d}:00-{self.quiet_end:02d}:00 "
            f"[{self._source}]"
        )


#: Process-wide instance. Commands may replace it via ``set_config`` after
#: parsing flags, so modules must read ``config.<field>`` at call time rather
#: than importing the values.
config = Config.load()


def set_config(new: Config) -> Config:
    """Apply settings by mutating the shared instance in place.

    Rebinding the module global would not work: other modules do
    ``from .config import config``, which binds the *object* at import time, so
    a rebind here leaves them pointing at the original and CLI overrides get
    silently ignored — the request budget would read 40 no matter what was
    passed. Mutating the instance everyone already holds avoids that entirely.
    """
    for spec in fields(Config):
        setattr(config, spec.name, getattr(new, spec.name))
    return config
