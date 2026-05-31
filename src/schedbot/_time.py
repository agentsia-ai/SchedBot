"""Time helpers — every engine timestamp goes through here.

Aware-UTC by construction. Reads tolerate legacy naive ISO strings (treated
as UTC) so existing local SQLite rows continue to work without a one-shot
migration script.

SchedBot is timezone-sensitive in a way LeadGen / CustComm are not — every
appointment has a "local" presentation timezone that's distinct from the
canonical UTC instant we store. The rule is:
    - STORE everything in UTC, always (`now_utc`, `to_iso`, `parse_iso`).
    - DISPLAY in the configured business timezone (`to_local`, `format_local`).

Private module: import via `from schedbot._time import now_utc, ...`. Not
re-exported at the package level — engine internals + tests only.
"""

from __future__ import annotations

from datetime import date, datetime, timezone, tzinfo
from typing import Optional

from dateutil import tz as _dateutil_tz


def now_utc() -> datetime:
    """Current time as a tz-aware UTC datetime.

    Returning an aware datetime means downstream `.isoformat()` produces a
    string with a `+00:00` offset, which round-trips losslessly through
    `parse_iso`.
    """
    return datetime.now(timezone.utc)


def to_iso(dt: datetime | None) -> str | None:
    """ISO 8601 string for storage. None passes through.

    Aware datetimes serialize with a `+00:00` suffix; naive datetimes (which
    should not occur post-migration) serialize without one. Use only on
    values produced by `now_utc()` or `parse_iso()` to guarantee aware output.
    """
    return dt.isoformat() if dt else None


def parse_iso(s: str | None) -> datetime | None:
    """Parse an ISO 8601 string into a tz-aware UTC datetime.

    Tolerates both shapes for forward/back compat:
      - aware  ('2026-01-15T10:30:00+00:00') — converted to UTC
      - naive  ('2026-01-15T10:30:00')       — assumed UTC, tz attached

    Returns None for None / empty input. Raises ValueError on malformed input
    (same behavior as `datetime.fromisoformat`).
    """
    if not s:
        return None
    # `Z` suffix (common in cal.com payloads) isn't accepted by stdlib until
    # 3.11+ in some forms — normalize defensively.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_tz(name: str) -> tzinfo:
    """Resolve an IANA timezone name (e.g. `America/New_York`) to a tzinfo.

    Falls back to UTC on unknown names so we never raise on display — invalid
    timezone configs degrade to ISO UTC strings instead of crashing the CLI.
    """
    resolved = _dateutil_tz.gettz(name)
    return resolved or timezone.utc


def to_local(dt: datetime | None, tz_name: str) -> Optional[datetime]:
    """Convert a stored (UTC) datetime into the business timezone for display.

    Never persist the result — `to_local` is a presentation helper. Always
    store via `to_iso(now_utc())` shape.
    """
    if dt is None:
        return None
    aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return aware.astimezone(get_tz(tz_name))


def format_local(dt: datetime | None, tz_name: str, fmt: str = "%a %b %d %I:%M %p %Z") -> str:
    """Pretty-print a UTC datetime in the configured business timezone."""
    if dt is None:
        return ""
    return to_local(dt, tz_name).strftime(fmt)


def weekday_sunday0(d: date) -> int:
    """Map a calendar date to 0=Sunday .. 6=Saturday.

    Config ``working_hours[].weekday`` and AvailabilityEngine both use this
    convention (distinct from Python's date.weekday() where Monday=0).
    """
    return (d.weekday() + 1) % 7
