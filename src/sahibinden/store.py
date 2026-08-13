"""SQLite store: turns repeated sweeps into a time series.

A single scrape answers "what is listed right now", which the website already
does better. The value here is what accumulates across runs:

  * **price history** — sellers anchor high and walk prices down; the trail is
    the negotiating signal
  * **days to delist** — how fast a car at a given price actually disappears,
    which is the closest observable proxy for what it sold at

Design notes:

* Prices are recorded only when they *change*. "Still listed at the same price"
  is already captured by ``listings.last_seen_at``, so storing it again would
  bloat the history and hide the actual moves.
* Delisting is inferred from absence, but only on **complete** sweeps. A partial
  sweep stops after N pages, so everything deeper looks absent — inferring
  removal from that would silently mark half the market as sold.
* Layout-specific fields (car year/km/colour vs property m²/rooms) live in a
  JSON column rather than forcing one table to serve both.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "data" / "sahibinden.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sweep_runs (
    id              INTEGER PRIMARY KEY,
    base_url        TEXT    NOT NULL,
    layout          TEXT    NOT NULL,
    started_at      TEXT    NOT NULL,
    total_results   INTEGER,
    pages_fetched   INTEGER,
    pages_available INTEGER,
    complete        INTEGER NOT NULL DEFAULT 0,
    listings_seen   INTEGER
);

CREATE TABLE IF NOT EXISTS listings (
    listing_id     TEXT PRIMARY KEY,
    url            TEXT,
    title          TEXT,
    trim           TEXT,
    layout         TEXT,
    city           TEXT,
    district       TEXT,
    store_name     TEXT,
    store_url      TEXT,
    listed_date    TEXT,
    attributes     TEXT,
    last_price_try INTEGER,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    delisted_at    TEXT
);

CREATE TABLE IF NOT EXISTS price_observations (
    id           INTEGER PRIMARY KEY,
    listing_id   TEXT    NOT NULL REFERENCES listings(listing_id),
    price_try    INTEGER NOT NULL,
    observed_at  TEXT    NOT NULL,
    sweep_run_id INTEGER REFERENCES sweep_runs(id)
);

CREATE TABLE IF NOT EXISTS sweep_appearances (
    sweep_run_id INTEGER NOT NULL REFERENCES sweep_runs(id),
    listing_id   TEXT    NOT NULL REFERENCES listings(listing_id),
    PRIMARY KEY (sweep_run_id, listing_id)
);

CREATE TABLE IF NOT EXISTS listing_details (
    listing_id          TEXT PRIMARY KEY REFERENCES listings(listing_id),
    fetched_at          TEXT NOT NULL,
    heavy_damage_record TEXT,
    original_count      INTEGER,
    local_painted_count INTEGER,
    painted_count       INTEGER,
    changed_count       INTEGER,
    damage              TEXT,
    description         TEXT,
    attributes          TEXT,
    info                TEXT,
    spec                TEXT,
    equipment           TEXT,
    images              TEXT,
    raw_path            TEXT
);

CREATE INDEX IF NOT EXISTS ix_prices_listing  ON price_observations(listing_id);
CREATE INDEX IF NOT EXISTS ix_listings_active ON listings(delisted_at, last_seen_at);
CREATE INDEX IF NOT EXISTS ix_sweeps_url      ON sweep_runs(base_url, started_at);
"""

#: Added after the first sweeps were already stored. Year and km arrive as
#: display strings ("2016", "280.000") inside the attributes JSON, which cannot
#: be compared or ordered numerically. Promoting them to real columns is what
#: makes market queries possible; MIGRATIONS runs on every open and is a no-op
#: once applied.
MIGRATIONS = [
    "ALTER TABLE listings ADD COLUMN year INTEGER",
    "ALTER TABLE listings ADD COLUMN km INTEGER",
    "ALTER TABLE listings ADD COLUMN brand TEXT",
    "ALTER TABLE listings ADD COLUMN model TEXT",
    "CREATE INDEX IF NOT EXISTS ix_listings_yearkm ON listings(year, km)",
    "CREATE INDEX IF NOT EXISTS ix_listings_brand ON listings(brand, model)",
]


def _as_int(value) -> int | None:
    """'280.000' -> 280000. Turkish thousands separator is '.'."""
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value) -> str | None:
    return json.dumps(value, ensure_ascii=False) if value else None


class Store:
    def __init__(self, path: Path | str = DEFAULT_DB) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        for statement in MIGRATIONS:
            try:
                self.db.execute(statement)
            except sqlite3.OperationalError:
                pass  # already applied
        self.db.commit()
        self._backfill_year_km()
        self._backfill_brand_model()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


    def _backfill_brand_model(self) -> None:
        """Recover brand/model for rows stored before those columns existed.

        The sweep that found each listing recorded its base_url, and a
        sahibinden search path is exactly ``/{brand-slug}-{model-slug}``. So the
        pair can be reconstructed offline instead of re-fetching 139 listings —
        matching the longest known brand slug first, since 'mercedes-benz-c-serisi'
        must not resolve to a brand called 'mercedes'.
        """
        from urllib.parse import urlparse

        from .taxonomy import Taxonomy, slugify

        pending = self.db.execute(
            "SELECT COUNT(*) FROM listings WHERE brand IS NULL"
        ).fetchone()[0]
        if not pending:
            return

        brand_slugs = sorted(
            ((slugify(name), name) for name in Taxonomy().brands),
            key=lambda pair: -len(pair[0]),
        )
        if not brand_slugs:
            return

        rows = self.db.execute(
            "SELECT DISTINCT a.listing_id, s.base_url FROM sweep_appearances a"
            " JOIN sweep_runs s ON s.id = a.sweep_run_id"
            " JOIN listings l ON l.listing_id = a.listing_id"
            " WHERE l.brand IS NULL"
        ).fetchall()

        for row in rows:
            path = urlparse(row["base_url"]).path.strip("/").split("/")[0]
            for slug, name in brand_slugs:
                if path == slug or path.startswith(slug + "-"):
                    model = path[len(slug):].strip("-") or None
                    self.db.execute(
                        "UPDATE listings SET brand=?, model=? WHERE listing_id=?",
                        (name, model, row["listing_id"]),
                    )
                    break
        self.db.commit()

    def _backfill_year_km(self) -> None:
        """Populate the new columns for rows stored before they existed."""
        rows = self.db.execute(
            "SELECT listing_id, attributes FROM listings"
            " WHERE attributes IS NOT NULL AND (year IS NULL OR km IS NULL)"
        ).fetchall()
        for row in rows:
            try:
                attrs = json.loads(row["attributes"]) or {}
            except (TypeError, ValueError):
                continue
            self.db.execute(
                "UPDATE listings SET year=COALESCE(year,?), km=COALESCE(km,?)"
                " WHERE listing_id=?",
                (_as_int(attrs.get("year")), _as_int(attrs.get("km")), row["listing_id"]),
            )
        if rows:
            self.db.commit()

    # ---------------------------------------------------------------- sweeps

    def record_sweep(self, result: dict) -> dict:
        """Persist a sweep. Returns counts of what actually changed."""
        now = _now()
        listings = result.get("listings", [])
        resolved = result.get("resolved") or {}
        brand, model = resolved.get("brand"), resolved.get("model")
        complete = bool(
            result.get("pages_available")
            and result.get("pages_fetched", 0) >= result["pages_available"]
        )

        cur = self.db.execute(
            "INSERT INTO sweep_runs (base_url, layout, started_at, total_results,"
            " pages_fetched, pages_available, complete, listings_seen)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (result.get("base_url"), result.get("layout", "vehicle"), now,
             result.get("total_results"), result.get("pages_fetched"),
             result.get("pages_available"), int(complete), len(listings)),
        )
        run_id = cur.lastrowid

        new, price_changes = 0, []
        for row in listings:
            listing_id = row["listing_id"]
            price = row.get("price_try")
            existing = self.db.execute(
                "SELECT last_price_try FROM listings WHERE listing_id=?", (listing_id,)
            ).fetchone()

            if existing is None:
                new += 1
                self.db.execute(
                    "INSERT INTO listings (listing_id, url, title, trim, layout, city,"
                    " district, store_name, store_url, listed_date, attributes,"
                    " last_price_try, first_seen_at, last_seen_at, year, km,"
                    " brand, model)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (listing_id, row.get("url"), row.get("title"), row.get("trim"),
                     result.get("layout", "vehicle"), row.get("city"), row.get("district"),
                     row.get("store_name"), row.get("store_url"), row.get("listed_date"),
                     _json(row.get("attributes")), price, now, now,
                     _as_int((row.get("attributes") or {}).get("year")),
                     _as_int((row.get("attributes") or {}).get("km")),
                     brand, model),
                )
                if price:
                    self._add_price(listing_id, price, now, run_id)
            else:
                # A listing reappearing after being marked delisted is a
                # relist, not a resurrection: clear the flag so days-to-delist
                # measures the current run rather than an ancient one.
                self.db.execute(
                    "UPDATE listings SET last_seen_at=?, delisted_at=NULL,"
                    " url=COALESCE(?,url), title=COALESCE(?,title),"
                    " attributes=COALESCE(?,attributes),"
                    " year=COALESCE(?,year), km=COALESCE(?,km),"
                    " brand=COALESCE(brand,?), model=COALESCE(model,?)"
                    " WHERE listing_id=?",
                    (now, row.get("url"), row.get("title"),
                     _json(row.get("attributes")),
                     _as_int((row.get("attributes") or {}).get("year")),
                     _as_int((row.get("attributes") or {}).get("km")),
                     brand, model, listing_id),
                )
                if price and price != existing["last_price_try"]:
                    price_changes.append(
                        (listing_id, existing["last_price_try"], price)
                    )
                    self._add_price(listing_id, price, now, run_id)
                    self.db.execute(
                        "UPDATE listings SET last_price_try=? WHERE listing_id=?",
                        (price, listing_id),
                    )

            self.db.execute(
                "INSERT OR IGNORE INTO sweep_appearances (sweep_run_id, listing_id)"
                " VALUES (?,?)", (run_id, listing_id),
            )

        delisted = self._mark_delisted(run_id, result.get("base_url"), complete, now)
        self.db.commit()

        return {
            "sweep_run_id": run_id,
            "complete": complete,
            "seen": len(listings),
            "new": new,
            "price_changes": price_changes,
            "delisted": delisted,
        }

    def _add_price(self, listing_id: str, price: int, at: str, run_id: int) -> None:
        self.db.execute(
            "INSERT INTO price_observations (listing_id, price_try, observed_at,"
            " sweep_run_id) VALUES (?,?,?,?)", (listing_id, price, at, run_id),
        )

    def _mark_delisted(self, run_id: int, base_url: str | None,
                       complete: bool, now: str) -> list[str]:
        """Mark listings that vanished since the last complete sweep of this search.

        Only meaningful on a complete sweep — on a partial one, everything past
        the last fetched page is absent for reasons that have nothing to do with
        the seller.
        """
        if not complete or not base_url:
            return []

        previous = self.db.execute(
            "SELECT id FROM sweep_runs WHERE base_url=? AND complete=1 AND id<?"
            " ORDER BY id DESC LIMIT 1", (base_url, run_id),
        ).fetchone()
        if previous is None:
            return []

        gone = [r["listing_id"] for r in self.db.execute(
            "SELECT a.listing_id FROM sweep_appearances a"
            " JOIN listings l ON l.listing_id = a.listing_id"
            " WHERE a.sweep_run_id=? AND l.delisted_at IS NULL"
            "   AND a.listing_id NOT IN"
            "       (SELECT listing_id FROM sweep_appearances WHERE sweep_run_id=?)",
            (previous["id"], run_id),
        ).fetchall()]

        for listing_id in gone:
            self.db.execute(
                "UPDATE listings SET delisted_at=? WHERE listing_id=?", (now, listing_id)
            )
        return gone

    # --------------------------------------------------------------- details

    def record_detail(self, detail: dict, raw_path: str | None = None) -> None:
        damage = detail.get("damage") or {}
        raw_counts = damage.get("counts") or {}
        # parse_damage omits zero counts, so a missing key means "none of that
        # kind". NULL is reserved for "no diagram on the page at all" — the two
        # must stay distinguishable, since absent evidence and evidence of
        # absence are very different for a damage record.
        counts = (
            {k: raw_counts.get(k, 0) for k in
             ("original", "local_painted", "painted", "changed")}
            if damage else {}
        )
        self.db.execute(
            "INSERT INTO listing_details (listing_id, fetched_at, heavy_damage_record,"
            " original_count, local_painted_count, painted_count, changed_count,"
            " damage, description, attributes, info, spec, equipment, images, raw_path)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(listing_id) DO UPDATE SET"
            "   fetched_at=excluded.fetched_at,"
            "   heavy_damage_record=excluded.heavy_damage_record,"
            "   original_count=excluded.original_count,"
            "   local_painted_count=excluded.local_painted_count,"
            "   painted_count=excluded.painted_count,"
            "   changed_count=excluded.changed_count,"
            "   damage=excluded.damage, description=excluded.description,"
            "   attributes=excluded.attributes, info=excluded.info,"
            "   spec=excluded.spec, equipment=excluded.equipment,"
            "   images=excluded.images, raw_path=excluded.raw_path",
            (detail.get("listing_id"), _now(), detail.get("heavy_damage_record"),
             counts.get("original"), counts.get("local_painted"),
             counts.get("painted"), counts.get("changed"),
             _json(damage.get("parts")), detail.get("description"),
             _json(detail.get("attributes")), _json(detail.get("info")),
             _json(detail.get("spec")), _json(detail.get("equipment")),
             _json(detail.get("images")), raw_path),
        )
        self.db.commit()

    # --------------------------------------------------------------- queries

    def stats(self) -> dict:
        q = lambda sql: self.db.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "listings": q("SELECT COUNT(*) FROM listings"),
            "active": q("SELECT COUNT(*) FROM listings WHERE delisted_at IS NULL"),
            "delisted": q("SELECT COUNT(*) FROM listings WHERE delisted_at IS NOT NULL"),
            "price_points": q("SELECT COUNT(*) FROM price_observations"),
            "with_details": q("SELECT COUNT(*) FROM listing_details"),
            "sweeps": q("SELECT COUNT(*) FROM sweep_runs"),
        }

    def inventory(self) -> dict:
        """What is actually recorded, so `status` can answer it without SQL.

        Coverage matters more than raw counts here: a sweep that stopped after
        five of 308 pages still stores 100 listings, and reading that as "the
        Egea market" would be badly wrong. So each tracked search reports how
        deep it went and whether any sweep of it was ever complete — completeness
        is also what gates delisting detection.
        """
        searches = [dict(r) for r in self.db.execute(
            "SELECT base_url, layout, COUNT(*) AS sweeps,"
            "       MAX(started_at) AS last_swept,"
            "       MAX(complete) AS ever_complete,"
            "       MAX(total_results) AS site_total,"
            "       MAX(pages_fetched) AS deepest_pages,"
            "       MAX(pages_available) AS pages_available"
            " FROM sweep_runs GROUP BY base_url ORDER BY last_swept DESC"
        ).fetchall()]

        groups = [dict(r) for r in self.db.execute(
            "SELECT l.brand, l.model, COUNT(*) AS listings,"
            "       SUM(CASE WHEN l.delisted_at IS NULL THEN 1 ELSE 0 END) AS active,"
            "       COUNT(d.listing_id) AS with_damage,"
            "       MIN(l.last_price_try) AS min_price,"
            "       MAX(l.last_price_try) AS max_price,"
            "       COUNT(DISTINCT l.city) AS cities"
            " FROM listings l LEFT JOIN listing_details d"
            "   ON d.listing_id = l.listing_id"
            " WHERE l.brand IS NOT NULL"
            " GROUP BY l.brand, l.model ORDER BY listings DESC"
        ).fetchall()]

        span = self.db.execute(
            "SELECT MIN(first_seen_at) AS since, MAX(last_seen_at) AS until"
            " FROM listings"
        ).fetchone()

        return {
            "totals": self.stats(),
            "collecting_since": span["since"],
            "last_updated": span["until"],
            "tracked_searches": searches,
            "by_model": groups,
        }

    def price_history(self, listing_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT price_try, observed_at FROM price_observations"
            " WHERE listing_id=? ORDER BY observed_at", (listing_id,),
        ).fetchall()

    def needs_detail(self, limit: int, *, max_price: int | None = None,
                     min_price: int | None = None, brand: str | None = None,
                     model: str | None = None, min_year: int | None = None,
                     max_km: int | None = None) -> list[sqlite3.Row]:
        """Active listings with no detail record yet — the detail-fetch queue.

        Cheapest-first *within the given scope*. Unscoped, cheapest-first is
        actively wrong once the database holds anything from a broad sweep: an
        unfiltered market query drags in the bottom of it, and a whole day's
        detail budget went on 1980s cars with broken gearboxes. The filters
        exist so the budget can be aimed at cars actually under consideration.
        """
        sql = ("SELECT l.listing_id, l.url, l.last_price_try, l.brand, l.model,"
               " l.year, l.km FROM listings l"
               " LEFT JOIN listing_details d ON d.listing_id = l.listing_id"
               " WHERE d.listing_id IS NULL AND l.delisted_at IS NULL AND l.url IS NOT NULL")
        params: list = []
        for clause, value in (
            (" AND l.last_price_try <= ?", max_price),
            (" AND l.last_price_try >= ?", min_price),
            (" AND LOWER(l.brand) = LOWER(?)", brand),
            (" AND LOWER(l.model) = LOWER(?)", model),
            (" AND l.year >= ?", min_year),
            (" AND l.km <= ?", max_km),
        ):
            if value is not None:
                sql += clause
                params.append(value)
        sql += " ORDER BY l.last_price_try ASC LIMIT ?"
        params.append(limit)
        return self.db.execute(sql, params).fetchall()

    def market(self, *, brand=None, model=None, min_year=None, max_year=None,
               max_km=None, city=None, exclude_damaged=False,
               include_delisted=False) -> dict:
        """Aggregate stored listings into an answer, not a row dump.

        Returned to an agent, so it is deliberately small: percentiles rather
        than every price, and the damage split rather than every diagram.
        """
        where, params = ["last_price_try IS NOT NULL"], []
        if brand:
            where.append("LOWER(brand) = LOWER(?)"); params.append(brand)
        if model:
            where.append("LOWER(model) = LOWER(?)"); params.append(model)
        if min_year:
            where.append("year >= ?"); params.append(min_year)
        if max_year:
            where.append("year <= ?"); params.append(max_year)
        if max_km:
            where.append("km <= ?"); params.append(max_km)
        if city:
            where.append("LOWER(city) = LOWER(?)"); params.append(city)
        if not include_delisted:
            where.append("delisted_at IS NULL")
        if exclude_damaged:
            # Absent detail is not evidence of cleanliness, so a listing only
            # survives this filter once it has actually been checked.
            where.append(
                "listing_id IN (SELECT listing_id FROM listing_details"
                " WHERE heavy_damage_record = 'hayir'"
                "   AND COALESCE(changed_count,0) = 0)"
            )
        clause = " AND ".join(where)

        rows = self.db.execute(
            f"SELECT l.*, d.heavy_damage_record, d.changed_count, d.painted_count"
            f" FROM listings l LEFT JOIN listing_details d"
            f" ON d.listing_id = l.listing_id WHERE {clause}"
            f" ORDER BY l.last_price_try", params,
        ).fetchall()

        prices = [r["last_price_try"] for r in rows]
        def pct(p):
            if not prices:
                return None
            idx = min(len(prices) - 1, int(round((len(prices) - 1) * p)))
            return prices[idx]

        checked = [r for r in rows if r["heavy_damage_record"] is not None]
        sold = self.db.execute(
            f"SELECT AVG(julianday(delisted_at) - julianday(first_seen_at)) AS d,"
            f" COUNT(*) AS n FROM listings l"
            f" WHERE delisted_at IS NOT NULL"
            + (" AND LOWER(brand)=LOWER(?)" if brand else ""),
            ([brand] if brand else []),
        ).fetchone()

        return {
            "matched": len(rows),
            "price": {
                "min": pct(0), "p25": pct(0.25), "median": pct(0.5),
                "p75": pct(0.75), "max": pct(1.0),
            },
            "km_median": sorted(r["km"] for r in rows if r["km"])[len(
                [r for r in rows if r["km"]]) // 2] if any(r["km"] for r in rows) else None,
            "damage": {
                "checked": len(checked),
                "unchecked": len(rows) - len(checked),
                "heavy_damage_flagged": sum(
                    1 for r in checked if r["heavy_damage_record"] == "evet"),
                "any_panel_changed": sum(
                    1 for r in checked if (r["changed_count"] or 0) > 0),
            },
            "turnover": {
                "delisted_observed": sold["n"] or 0,
                "avg_days_to_delist": round(sold["d"], 1) if sold["d"] else None,
            },
            "cheapest": [
                {"listing_id": r["listing_id"], "price_try": r["last_price_try"],
                 "year": r["year"], "km": r["km"], "city": r["city"],
                 "heavy_damage_record": r["heavy_damage_record"]}
                for r in rows[:10]
            ],
        }
