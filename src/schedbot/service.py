"""Engine service layer.

Thin coordination functions used by both the CLI and the MCP server.
Mirrors CustComm's `service.py`: every function takes an already-
constructed config + keys + db triple so the caller controls lifecycle.
The service layer never owns state.

The functions here are the only place that knows how to compose the
sub-modules (sources / scheduler / AI / database / outreach) into the
high-level operations the CLI and MCP surface — `book`, `confirm`,
`reschedule`, `cancel`, `send_reminder`, `daily_digest`, etc.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from schedbot._time import now_utc, to_iso
from schedbot.ai.classifier import RequestClassifier
from schedbot.ai.drafter import MessageDrafter
from schedbot.ai.scheduler import AvailabilityScheduler
from schedbot.config.loader import APIKeys, SchedBotConfig, ServiceConfig
from schedbot.crm.database import AppointmentDatabase
from schedbot.models import (
    Appointment,
    AppointmentSource,
    AppointmentStatus,
    ClassificationResult,
    ClientInfo,
    Location,
    LocationKind,
    RawBookingRequest,
    ReminderRecord,
    ReminderStatus,
    TimeSlot,
    WaitlistEntry,
    WaitlistStatus,
)
from schedbot.scheduler.availability import (
    AvailabilityEngine,
    busy_intervals_from_appointments,
)
from schedbot.scheduler.reminders import ReminderScheduler
from schedbot.scheduler.waitlist import WaitlistManager

logger = logging.getLogger(__name__)


# ── Booking ingest ────────────────────────────────────────────────────────────


async def ingest_booking(
    config: SchedBotConfig,
    db: AppointmentDatabase,
    raw: RawBookingRequest,
) -> tuple[Appointment, bool]:
    """Translate a `RawBookingRequest` into an Appointment row.

    Returns `(appointment, is_new)`. Idempotent on `(provider, external_id)` —
    a replay returns the existing appointment with `is_new=False`.

    Status decisions:
      - `booking.created`     → REQUESTED  (caller decides when to confirm)
      - `booking.cancelled`   → CANCELLED
      - `booking.rescheduled` → RESCHEDULED on the prior; new REQUESTED row
                                created with `rescheduled_to_id` chained
      - `meeting.ended`       → COMPLETED
      - `meeting.no_show`     → NO_SHOW
    """
    source = _provider_to_source(raw.provider)

    if raw.provider_event_id:
        existing = await db.find_by_external_id(source, raw.provider_event_id)
        if existing is not None and raw.event_kind == "booking.created":
            return existing, False
        if existing is not None and raw.event_kind == "booking.cancelled":
            existing.status = AppointmentStatus.CANCELLED
            existing.cancelled_at = now_utc()
            return await db.upsert_appointment(existing), False
        if existing is not None and raw.event_kind in {"meeting.ended", "meeting.no_show"}:
            existing.status = (
                AppointmentStatus.NO_SHOW
                if raw.event_kind == "meeting.no_show"
                else AppointmentStatus.COMPLETED
            )
            if raw.event_kind == "meeting.no_show":
                existing.no_show_at = now_utc()
            return await db.upsert_appointment(existing), False

    appt = Appointment(
        source=source,
        external_id=raw.provider_event_id or None,
        client=ClientInfo(
            name=raw.client_name,
            email=raw.client_email,
            phone=raw.client_phone,
        ),
        service_slug=raw.service_slug,
        service_name=raw.service_name,
        requested_at=raw.requested_start_at,
        start_at=raw.requested_start_at,
        end_at=raw.requested_end_at,
        duration_minutes=raw.duration_minutes,
        timezone=raw.timezone,
        location=raw.location or Location(kind=LocationKind.IN_PERSON),
        intake_notes=raw.notes,
        raw_data={"event_kind": raw.event_kind, "payload": raw.raw_payload},
    )
    if raw.event_kind == "booking.cancelled":
        appt.status = AppointmentStatus.CANCELLED
        appt.cancelled_at = now_utc()
    saved = await db.upsert_appointment(appt)
    return saved, True


# ── Availability ──────────────────────────────────────────────────────────────


async def find_available_slots(
    config: SchedBotConfig,
    db: AppointmentDatabase,
    service_slug: str,
    from_dt: Optional[datetime] = None,
    days: Optional[int] = None,
    max_slots: int = 30,
) -> list[TimeSlot]:
    """Generate up to `max_slots` candidate slots for `service_slug`,
    excluding any CONFIRMED / REMINDED appointment in the window.
    """
    service = _get_service(config, service_slug)
    if service is None:
        raise ValueError(
            f"Unknown service slug {service_slug!r}. "
            f"Add it to business.services in config.yaml."
        )

    engine = AvailabilityEngine(config)
    window_days = days if days is not None else config.scheduler.booking_window_days
    horizon = (from_dt or now_utc()) + timedelta(days=window_days)
    busy_appts = await db.list_appointments(
        service_slug=service_slug,
        start_after=to_iso(from_dt or now_utc()),
        start_before=to_iso(horizon),
        limit=500,
    )
    busy_appts = [a for a in busy_appts if a.is_open]
    busy_intervals = busy_intervals_from_appointments(busy_appts, service)

    return engine.find_candidate_slots(
        service=service,
        from_dt=from_dt,
        days=window_days,
        busy=busy_intervals,
        max_slots=max_slots,
    )


# ── Confirm / Reschedule / Cancel ─────────────────────────────────────────────


async def confirm_appointment(
    config: SchedBotConfig,
    keys: APIKeys,
    db: AppointmentDatabase,
    appointment_id: str,
    drafter_cls: type[MessageDrafter] = MessageDrafter,
    reminder_scheduler_cls=ReminderScheduler,
    channel: str = "email",
    guidance: str = "",
) -> Optional[Appointment]:
    """Atomically reserve the slot, draft the confirmation message, and
    enqueue the reminder records (all DRAFTED — operator approves before
    anything sends).

    Returns the updated appointment, or None if the slot couldn't be
    reserved (already cancelled, already confirmed, or conflict).
    """
    appt = await db.get_appointment(appointment_id)
    if appt is None or appt.start_at is None or appt.end_at is None:
        return None
    if appt.status != AppointmentStatus.REQUESTED:
        # Idempotent: already-confirmed appointments just re-draft if the
        # operator asks. We skip the slot reservation step in that case.
        if appt.status == AppointmentStatus.CONFIRMED:
            await _draft_and_attach_confirmation(
                config, keys, appt, drafter_cls, channel, guidance
            )
            return await db.upsert_appointment(appt)
        logger.info(
            f"Cannot confirm appointment {appointment_id}: status={appt.status.value}"
        )
        return None

    reserved = await db.reserve_slot(
        appointment_id=appt.id,
        service_slug=appt.service_slug,
        start_at=to_iso(appt.start_at),
        end_at=to_iso(appt.end_at),
    )
    if not reserved:
        logger.warning(
            f"reserve_slot returned False for appointment {appointment_id} — "
            "another concurrent caller booked an overlapping slot."
        )
        # Refresh and return so caller can see the current state.
        return await db.get_appointment(appointment_id)

    appt = await db.get_appointment(appointment_id)
    if appt is None:
        return None

    await _draft_and_attach_confirmation(
        config, keys, appt, drafter_cls, channel, guidance
    )
    saved = await db.upsert_appointment(appt)

    scheduler = reminder_scheduler_cls(config, db)
    await scheduler.enqueue_for_appointment(saved)
    return saved


async def _draft_and_attach_confirmation(
    config: SchedBotConfig,
    keys: APIKeys,
    appt: Appointment,
    drafter_cls: type[MessageDrafter],
    channel: str,
    guidance: str,
) -> None:
    drafter = drafter_cls(config, keys)
    subject, body = await drafter.draft_confirmation(appt, channel=channel, guidance=guidance)
    appt.confirmation_subject = subject
    appt.confirmation_body = body
    appt.confirmation_status = ReminderStatus.DRAFTED


async def reschedule_appointment(
    config: SchedBotConfig,
    keys: APIKeys,
    db: AppointmentDatabase,
    appointment_id: str,
    drafter_cls: type[MessageDrafter] = MessageDrafter,
    scheduler_cls: type[AvailabilityScheduler] = AvailabilityScheduler,
    customer_request_text: str = "",
    channel: str = "email",
    guidance: str = "",
) -> tuple[Optional[Appointment], list[TimeSlot]]:
    """Generate alternative slots and draft a "here are some options"
    response. Does NOT actually move the appointment — that's a follow-on
    `confirm_appointment` once the customer picks a slot.
    """
    appt = await db.get_appointment(appointment_id)
    if appt is None:
        return None, []

    slots = await find_available_slots(
        config, db, service_slug=appt.service_slug, max_slots=20
    )
    picker = scheduler_cls(config, keys)
    chosen, reasoning = await picker.pick_slots(
        candidate_slots=slots,
        customer_request_text=customer_request_text,
        service_name=appt.service_name,
        requested_at=appt.start_at,
    )
    logger.info(
        f"reschedule_appointment {appointment_id}: picked {len(chosen)} slot(s) — {reasoning}"
    )

    drafter = drafter_cls(config, keys)
    subject, body = await drafter.draft_reschedule_options(
        appt, chosen, channel=channel, guidance=guidance
    )
    appt.confirmation_subject = subject
    appt.confirmation_body = body
    appt.confirmation_status = ReminderStatus.DRAFTED
    saved = await db.upsert_appointment(appt)
    return saved, chosen


async def cancel_appointment(
    config: SchedBotConfig,
    keys: APIKeys,
    db: AppointmentDatabase,
    appointment_id: str,
    reason: str = "",
    drafter_cls: type[MessageDrafter] = MessageDrafter,
    waitlist_cls=WaitlistManager,
    channel: str = "email",
    guidance: str = "",
) -> Optional[Appointment]:
    """Cancel an appointment and draft the cancellation acknowledgement.
    Promotes a waitlisted client (if any) to OFFERED on the freed slot."""
    appt = await db.get_appointment(appointment_id)
    if appt is None:
        return None
    appt.status = AppointmentStatus.CANCELLED
    appt.cancelled_at = now_utc()
    appt.cancellation_reason = reason

    drafter = drafter_cls(config, keys)
    subject, body = await drafter.draft_cancellation(
        appt, reason=reason, channel=channel, guidance=guidance
    )
    appt.confirmation_subject = subject
    appt.confirmation_body = body
    appt.confirmation_status = ReminderStatus.DRAFTED
    saved = await db.upsert_appointment(appt)

    # Best-effort waitlist promotion. Failure here mustn't block the cancel.
    try:
        manager = waitlist_cls(config, db)
        candidate = await manager.find_promotion_candidate(appt.service_slug)
        if candidate and config.waitlist.auto_draft_offer:
            offer_subject, offer_body = await drafter.draft_waitlist_offer(
                appt, offer_expires_at=None, channel=channel
            )
            await manager.offer(candidate, appt, offer_subject, offer_body)
            logger.info(
                f"Promoted waitlist entry {candidate.id} into OFFERED for {appt.id}"
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Waitlist promotion after cancel failed: {e}")

    return saved


async def mark_no_show(
    config: SchedBotConfig,
    keys: APIKeys,
    db: AppointmentDatabase,
    appointment_id: str,
    drafter_cls: type[MessageDrafter] = MessageDrafter,
    channel: str = "email",
    guidance: str = "",
) -> Optional[Appointment]:
    appt = await db.get_appointment(appointment_id)
    if appt is None:
        return None
    appt.status = AppointmentStatus.NO_SHOW
    appt.no_show_at = now_utc()

    if config.reminders.no_show_followup_enabled:
        drafter = drafter_cls(config, keys)
        subject, body = await drafter.draft_no_show_followup(
            appt, channel=channel, guidance=guidance
        )
        appt.no_show_followup_subject = subject
        appt.no_show_followup_body = body
    return await db.upsert_appointment(appt)


# ── Reminders ─────────────────────────────────────────────────────────────────


async def draft_pending_reminders(
    config: SchedBotConfig,
    keys: APIKeys,
    db: AppointmentDatabase,
    drafter_cls: type[MessageDrafter] = MessageDrafter,
    limit: int = 50,
    guidance: str = "",
) -> list[ReminderRecord]:
    """Walk DRAFTED reminders that don't yet have a body, fill them in,
    and persist. Drafts that already have a body (e.g. operator-edited)
    are left alone."""
    drafter = drafter_cls(config, keys)
    out: list[ReminderRecord] = []
    pairs = await db.list_pending_reminders(limit=limit)
    for appt_id, reminder in pairs:
        if reminder.body:
            continue
        appt = await db.get_appointment(appt_id)
        if appt is None:
            continue
        subject, body = await drafter.draft_reminder(
            appt,
            offset_minutes_before=reminder.offset_minutes_before,
            channel=reminder.channel.value,
            guidance=guidance,
        )
        reminder.subject = subject
        reminder.body = body
        # Re-insert with updated content. (insert_reminder expects a fresh
        # row; the reminders table allows overwrite via the unique id since
        # we INSERT OR REPLACE-equivalent via PK collision.)
        await db.mark_reminder_failed(reminder.id, "")  # clears any prior error
        await _replace_reminder(db, appt_id, reminder)
        out.append(reminder)
    return out


async def _replace_reminder(
    db: AppointmentDatabase, appointment_id: str, reminder: ReminderRecord
) -> None:
    """Update a reminder's drafted content. We don't have an UPDATE method
    on the DB facade, so we delete + re-insert under the same id."""
    import aiosqlite  # local import to keep the public surface minimal

    async with aiosqlite.connect(db.db_path) as conn:
        await conn.execute("DELETE FROM reminders WHERE id = ?", (reminder.id,))
        await conn.commit()
    await db.insert_reminder(appointment_id, reminder)


# ── Daily digest ──────────────────────────────────────────────────────────────


async def daily_digest(
    config: SchedBotConfig,
    db: AppointmentDatabase,
    on_date: Optional[datetime] = None,
) -> dict[str, list[Appointment]]:
    """Return today + tomorrow's appointments, grouped, for the morning
    digest. Pure data — formatting is the caller's concern."""
    base = on_date or now_utc()
    today_start = base.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)
    day_after = today_start + timedelta(days=2)

    today = await db.list_appointments(
        start_after=to_iso(today_start),
        start_before=to_iso(tomorrow_start),
        limit=200,
    )
    tomorrow = await db.list_appointments(
        start_after=to_iso(tomorrow_start),
        start_before=to_iso(day_after),
        limit=200,
    )
    return {"today": today, "tomorrow": tomorrow}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _get_service(config: SchedBotConfig, slug: str) -> Optional[ServiceConfig]:
    for s in config.business.services:
        if s.slug == slug:
            return s
    return None


def _provider_to_source(provider: str) -> AppointmentSource:
    """Map a provider name from a RawBookingRequest into our Source enum.
    Unknown providers fall through to MANUAL — better to record an
    unrecognized booking than to drop it."""
    try:
        return AppointmentSource(provider.lower())
    except (KeyError, ValueError):
        return AppointmentSource.MANUAL


__all__ = [
    "ingest_booking",
    "find_available_slots",
    "confirm_appointment",
    "reschedule_appointment",
    "cancel_appointment",
    "mark_no_show",
    "draft_pending_reminders",
    "daily_digest",
]
