"""Filter rules, the same as frontend/src/lib/filters.ts.

Both sides are tested against shared/cases/filters.json, so they agree on what
"last 30 days" or "active between" means. Days are ISO strings "YYYY-MM-DD";
comparing them as strings compares them as dates.

"Active between": an item counts when it was seen at some point inside the range,
i.e. firstSeen <= to and lastSeen >= from. An empty side means no limit there.
A reversed range (from after to) is invalid and filters nothing out.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

PRESETS = {"7": 7, "30": 30, "90": 90}


@dataclass(frozen=True)
class DateRange:
    from_: str = ""
    to: str = ""
    invalid: bool = False

    def as_dict(self) -> dict:
        return {"from": self.from_, "to": self.to, "invalid": self.invalid}


def shift_days(day: str, days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def resolve_range(range_: str, from_: str, to: str, today: str) -> DateRange:
    """A preset ("7", "30", "90") ends today; "custom" uses from/to as given."""
    if range_ in PRESETS:
        return DateRange(shift_days(today, -PRESETS[range_]), today, False)
    if range_ != "custom":
        raise ValueError(f"Unknown range {range_!r}")
    return DateRange(from_, to, bool(from_ and to and from_ > to))


def overlaps(first_seen: str, last_seen: str, r: DateRange) -> bool:
    if r.invalid:
        return True
    return (not r.from_ or last_seen >= r.from_) and (not r.to or first_seen <= r.to)


def matches_platform(platforms: Iterable[str], wanted: str) -> bool:
    return wanted == "all" or wanted in platforms


def matches_status(verdict: str, wanted: str) -> bool:
    return wanted == "all" or verdict == wanted


_QUERY_TRIM = re.compile("^[ \t\n\r\f\v\u00a0\ufeff]+|[ \t\n\r\f\v\u00a0\ufeff]+$")


def matches_query(text: str, query: str) -> bool:
    """Claim search: case-insensitive substring match; accents are significant
    ("a" doesn't match "ą"); an empty search matches everything. Do this in Python
    (or with a binary collation): MySQL's default collation ignores accents."""
    q = _QUERY_TRIM.sub("", query).lower()
    return not q or q in text.lower()


def range_from_params(from_: str | None, to: str | None) -> DateRange:
    """What the API receives: resolved from/to dates (either may be missing)."""
    from_, to = from_ or "", to or ""
    return DateRange(from_, to, bool(from_ and to and from_ > to))
