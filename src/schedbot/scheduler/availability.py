"""Deterministic availability engine.

Generates candidate `TimeSlot`s for a given service over a date range,
respecting:
  - Configured business hours per weekday (in the business timezone).
  - Per-service duration + buffer (before and after).
  - Existing CONFIRMED / REMINDED appointments (busy intervals) — pulled
    in by the caller and passed in. Keeping the busy lookup external means
    this module is pure and trivially testable.
  - Configured `min_lead_minutes` so customers can't book inside the next
    hour by accident.

This is the *generator* of candidate slots. Picking which 2–3 to OFFER a
customer happens in `schedbot.ai.scheduler.AvailabilityScheduler`.

All intermediate work happens in the business timezone (because "9am
Tuesday" is a local concept), then results are converted to aware-UTC
before being returned. Atomic double-booking prevention happens later, in
`AppointmentDatabase.reserve_slot`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional

from schedbot._time import get_tz, now_utc
from schedbot.config.loader import SchedBotConfig, ServiceConfig
from schedbot.models import Appointment, TimeSlot

logger = logging.getLogger(__name__)


@dataclass
class BusyInterval:
    """Half-open [start, end) UTC interval to subtract from availability.

    Buffer time has already been applied if applicable — the caller decides
    whether buffer applies (e.g. for a same-service overlap) or doesn't
    (e.g. for an out-of-band block).
    """

    start: datetime
    end: datetime


class AvailabilityEngine:
    """Generates candidate slots for a service on a date range."""

    def __init__(self, config: SchedBotConfig) -> None:
        self.config = config
        self.tz = get_tz(config.business.timezone)

    # ── public API ────────────────────────────────────────────────────────

    def find_candidate_slots(
        self,
        service: ServiceConfig,
        from_dt: Optional[datetime] = None,
        days: Optional[int] = None,
        busy: Iterable[BusyInterval] = (),
        max_slots: int = 50,
    ) -> list[TimeSlot]:
        """Return UTC slots that fit `service` and don't overlap `busy`.

        - `from_dt` defaults to (now + min_lead_minutes), in UTC.
        - `days` defaults to `scheduler.booking_window_days`.
        - `busy` should include every CONFIRMED / REMINDED appointment for
          the same service across the window — the engine never trusts
          itself to know what's busy without being told.
        """
        lead = timedelta(minutes=self.config.scheduler.min_lead_minutes)
        from_dt = (from_dt or now_utc()) + lead
        days = days if days is not None else self.config.scheduler.booking_window_days

        # Bake buffer into the comparison interval — a 30-minute service
        # with 15min buffer-after needs a 45-minute open window.
        slot_minutes = service.duration_minutes
        buffer_before = service.buffer_before_minutes
        buffer_after = service.buffer_after_minutes
        full_minutes = buffer_before + slot_minutes + buffer_after

        # Stride determines slot start cadence. Each ServiceConfig duration
        # implicitly defines the "minimum stride"; the global default is a
        # floor for shorter services.
        stride_minutes = max(
            self.config.scheduler.default_slot_stride_minutes,
            slot_minutes,
        )

        busy_sorted = sorted(busy, key=lambda b: b.start)

        out: list[TimeSlot] = []
        current_day = from_dt.astimezone(self.tz).date()
        end_day = current_day + timedelta(days=days)

        while current_day < end_day and len(out) < max_slots:
            for window in self.config.business.working_hours:
                if window.weekday != current_day.weekday():
                    continue

                open_local = self._combine_local(current_day, window.open_at)
                close_local = self._combine_local(current_day, window.close_at)

                cursor_local = open_local
                while cursor_local + timedelta(minutes=full_minutes) <= close_local:
                    # The "appointment slot" the customer sees runs from
                    # cursor + buffer_before to cursor + buffer_before + slot_minutes;
                    # the busy window we test against includes the buffers.
                    appt_start_local = cursor_local + timedelta(minutes=buffer_before)
                    appt_end_local = appt_start_local + timedelta(minutes=slot_minutes)
                    busy_start = cursor_local
                    busy_end = appt_end_local + timedelta(minutes=buffer_after)

                    appt_start_utc = appt_start_local.astimezone(get_tz("UTC"))
                    appt_end_utc = appt_end_local.astimezone(get_tz("UTC"))
                    busy_start_utc = busy_start.astimezone(get_tz("UTC"))
                    busy_end_utc = busy_end.astimezone(get_tz("UTC"))

                    if appt_start_utc < from_dt:
                        cursor_local += timedelta(minutes=stride_minutes)
                        continue

                    if not _overlaps_any(busy_sorted, busy_start_utc, busy_end_utc):
                        out.append(
                            TimeSlot(
                                start_at=appt_start_utc,
                                end_at=appt_end_utc,
                                timezone=self.config.business.timezone,
                            )
                        )
                        if len(out) >= max_slots:
                            break

                    cursor_local += timedelta(minutes=stride_minutes)

            current_day += timedelta(days=1)

        return out

    # ── helpers ──────────────────────────────────────────────────────────

    def _combine_local(self, day: date, t_str_or_time: str | time) -> datetime:
        if isinstance(t_str_or_time, str):
            parsed = time.fromisoformat(t_str_or_time)
        else:
            parsed = t_str_or_time
        return datetime.combine(day, parsed).replace(tzinfo=self.tz)


def _overlaps_any(
    busy_sorted: list[BusyInterval], start: datetime, end: datetime
) -> bool:
    """True if [start, end) overlaps any interval in `busy_sorted`. Half-open
    semantics: an interval that ends exactly at `start` does NOT overlap."""
    for b in busy_sorted:
        if b.end <= start:
            continue
        if b.start >= end:
            return False
        return True
    return False


def busy_intervals_from_appointments(
    appointments: Iterable[Appointment],
    service: Optional[ServiceConfig] = None,
) -> list[BusyInterval]:
    """Convenience: turn a list of stored Appointments into busy intervals,
    inflating with the service's buffer-before / buffer-after if provided."""
    out: list[BusyInterval] = []
    bb = service.buffer_before_minutes if service else 0
    ba = service.buffer_after_minutes if service else 0
    for a in appointments:
        if a.start_at is None or a.end_at is None:
            continue
        out.append(
            BusyInterval(
                start=a.start_at - timedelta(minutes=bb),
                end=a.end_at + timedelta(minutes=ba),
            )
        )
    return out
