"""Shared helpers for the sahibinden parsers."""

from __future__ import annotations

import datetime as dt
import re

TURKISH_MONTHS = {
    "ocak": 1, "şubat": 2, "mart": 3, "nisan": 4, "mayıs": 5, "haziran": 6,
    "temmuz": 7, "ağustos": 8, "eylül": 9, "ekim": 10, "kasım": 11, "aralık": 12,
}


def text(node) -> str:
    """Collapse a node to single-spaced text, dropping &nbsp; and stray tabs."""
    if node is None:
        return ""
    raw = node.get_text(" ", strip=True).replace("\xa0", " ")
    return re.sub(r"\s+", " ", raw).strip()


def parse_turkish_date(value: str | None) -> str | None:
    """'13 Ağustos 2026' -> '2026-08-13'. Returns None if unrecognised.

    Kept lossy-safe: callers keep the original string too, so a month-name
    change upstream degrades to a missing date rather than a wrong one.
    """
    if not value:
        return None
    m = re.search(r"(\d{1,2})\s+([^\s\d]+)\s+(\d{4})", value.strip())
    if not m:
        return None
    day, month_name, year = m.groups()
    month = TURKISH_MONTHS.get(month_name.lower())
    if month is None:
        return None
    try:
        return dt.date(int(year), month, int(day)).isoformat()
    except ValueError:
        return None


def parse_turkish_number(value: str | None) -> int | None:
    """'159.000' or '850.000 TL' -> int. Thousands separator is '.' in tr-TR."""
    if not value:
        return None
    digits = re.sub(r"[^\d]", "", value)
    return int(digits) if digits else None
