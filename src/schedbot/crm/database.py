"""SchedBot CRM — async SQLite store.

Tables: appointments, reminders, waitlist.
JSON blobs hold list-valued and nested fields; denormalized columns enable
fast filtering (status, start_at, client_email, service_slug).

Two interlocks live here, mirroring the pattern from CustComm's
ThreadDatabase:

  1. Atomic slot reservation (`reserve_slot`) — UPDATE guarded by both
     status AND non-overlap with any other CONFIRMED appointment for the
     same service. Buffer time is included via the service's stored
     duration. Defends against double-bookings when CLI + MCP race.

  2. Reminder send-guard (`mark_reminder_sent`) — UPDATE guarded by both
     status='approved' AND approval_token. Prevents double-sends when CLI
     and MCP both try to flush the queue.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from schedbot._time import now_utc, parse_iso, to_iso
from schedbot.models import (
    Appointment,
    AppointmentSource,
    AppointmentStatus,
    ClientInfo,
    Location,
    LocationKind,
    ReminderChannel,
    ReminderRecord,
    ReminderStatus,
    WaitlistEntry,
    WaitlistStatus,
)

logger = logging.getLogger(__name__)


class AppointmentDatabase:
    """Async SQLite-backed store for all SchedBot entities."""

    def __init__(self, db_path: str = "./data/schedbot.db") -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    # ── schema ────────────────────────────────────────────────────────────────

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS appointments (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL DEFAULT 'manual',
                    external_id TEXT,
                    client_name TEXT DEFAULT '',
                    client_email TEXT DEFAULT '',
                    client_phone TEXT DEFAULT '',
                    service_slug TEXT DEFAULT '',
                    service_name TEXT DEFAULT '',
                    requested_at TEXT,
                    start_at TEXT,
                    end_at TEXT,
                    duration_minutes INTEGER DEFAULT 30,
                    timezone TEXT DEFAULT 'UTC',
                    location_json TEXT DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'requested',
                    confirmation_subject TEXT DEFAULT '',
                    confirmation_body TEXT DEFAULT '',
                    confirmation_status TEXT DEFAULT 'drafted',
                    confirmation_token TEXT DEFAULT '',
                    confirmation_approved_at TEXT,
                    confirmation_sent_at TEXT,
                    notes TEXT DEFAULT '',
                    intake_notes TEXT DEFAULT '',
                    tags_json TEXT DEFAULT '[]',
                    no_show_at TEXT,
                    no_show_followup_subject TEXT DEFAULT '',
                    no_show_followup_body TEXT DEFAULT '',
                    confirmed_at TEXT,
                    cancelled_at TEXT,
                    cancellation_reason TEXT DEFAULT '',
                    rescheduled_to_id TEXT,
                    raw_data_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_appts_status ON appointments(status)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_appts_start ON appointments(start_at)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_appts_client ON appointments(client_email)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_appts_service ON appointments(service_slug)"
            )
            # External-id dedup for webhook replays. NULL allowed (manual entries),
            # uniqueness is conditional. Same shape as CustComm's message dedup.
            await db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_appts_external
                ON appointments(source, external_id)
                WHERE external_id IS NOT NULL
                """
            )

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id TEXT PRIMARY KEY,
                    appointment_id TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'email',
                    offset_minutes_before INTEGER NOT NULL DEFAULT 60,
                    scheduled_for TEXT,
                    status TEXT NOT NULL DEFAULT 'drafted',
                    subject TEXT DEFAULT '',
                    body TEXT DEFAULT '',
                    drafted_at TEXT NOT NULL,
                    approved_at TEXT,
                    approved_by TEXT,
                    sent_at TEXT,
                    provider_message_id TEXT,
                    error TEXT DEFAULT '',
                    approval_token TEXT NOT NULL
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_reminders_appt "
                "ON reminders(appointment_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_reminders_status "
                "ON reminders(status)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_reminders_due "
                "ON reminders(scheduled_for)"
            )

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS waitlist (
                    id TEXT PRIMARY KEY,
                    client_name TEXT DEFAULT '',
                    client_email TEXT DEFAULT '',
                    client_phone TEXT DEFAULT '',
                    service_slug TEXT DEFAULT '',
                    desired_window_start TEXT,
                    desired_window_end TEXT,
                    notes TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'waiting',
                    offered_appointment_id TEXT,
                    offered_at TEXT,
                    offer_expires_at TEXT,
                    offer_subject TEXT DEFAULT '',
                    offer_body TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_waitlist_status ON waitlist(status)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_waitlist_service ON waitlist(service_slug)"
            )

            await db.commit()
        logger.info(f"Database initialized: {self.db_path}")

    # ── appointments ──────────────────────────────────────────────────────────

    async def upsert_appointment(self, appt: Appointment) -> Appointment:
        appt.touch()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT id FROM appointments WHERE id = ?", (appt.id,)
            ) as cur:
                existing = await cur.fetchone()

            row = _appt_to_row(appt)
            if existing:
                await db.execute(
                    """
                    UPDATE appointments SET
                      source=?, external_id=?, client_name=?, client_email=?,
                      client_phone=?, service_slug=?, service_name=?,
                      requested_at=?, start_at=?, end_at=?, duration_minutes=?,
                      timezone=?, location_json=?, status=?,
                      confirmation_subject=?, confirmation_body=?,
                      confirmation_status=?, confirmation_token=?,
                      confirmation_approved_at=?, confirmation_sent_at=?,
                      notes=?, intake_notes=?, tags_json=?,
                      no_show_at=?, no_show_followup_subject=?,
                      no_show_followup_body=?, confirmed_at=?, cancelled_at=?,
                      cancellation_reason=?, rescheduled_to_id=?,
                      raw_data_json=?, updated_at=?
                    WHERE id=?
                    """,
                    row[1:] + (appt.id,),
                )
            else:
                await db.execute(
                    """
                    INSERT INTO appointments
                      (id, source, external_id, client_name, client_email,
                       client_phone, service_slug, service_name,
                       requested_at, start_at, end_at, duration_minutes,
                       timezone, location_json, status,
                       confirmation_subject, confirmation_body,
                       confirmation_status, confirmation_token,
                       confirmation_approved_at, confirmation_sent_at,
                       notes, intake_notes, tags_json,
                       no_show_at, no_show_followup_subject,
                       no_show_followup_body, confirmed_at, cancelled_at,
                       cancellation_reason, rescheduled_to_id,
                       raw_data_json, updated_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?)
                    """,
                    row + (to_iso(appt.created_at),),
                )
            await db.commit()
        return appt

    async def get_appointment(self, appointment_id: str) -> Optional[Appointment]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM appointments WHERE id = ?", (appointment_id,)
            ) as cur:
                row = await cur.fetchone()
        return _row_to_appt(row) if row else None

    async def find_by_external_id(
        self, source: AppointmentSource | str, external_id: str
    ) -> Optional[Appointment]:
        """Webhook idempotency lookup. Returns the existing appt if a
        replay arrives for an event we've already ingested."""
        if not external_id:
            return None
        src = source.value if isinstance(source, AppointmentSource) else source
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM appointments WHERE source = ? AND external_id = ?",
                (src, external_id),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_appt(row) if row else None

    async def list_appointments(
        self,
        status: Optional[AppointmentStatus] = None,
        service_slug: Optional[str] = None,
        client_email: Optional[str] = None,
        start_after: Optional[str] = None,
        start_before: Optional[str] = None,
        limit: int = 100,
    ) -> list[Appointment]:
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status.value)
        if service_slug:
            where.append("service_slug = ?")
            params.append(service_slug)
        if client_email:
            where.append("client_email = ?")
            params.append(ClientInfo.normalize_email(client_email))
        if start_after:
            where.append("start_at >= ?")
            params.append(start_after)
        if start_before:
            where.append("start_at < ?")
            params.append(start_before)
        clause = f"WHERE {' AND '.join(where)}" if where else ""

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            q = (
                f"SELECT * FROM appointments {clause} "
                f"ORDER BY start_at ASC NULLS LAST, created_at ASC LIMIT ?"
            )
            # NULLS LAST isn't supported by SQLite; emulate with COALESCE.
            q = q.replace(
                "ORDER BY start_at ASC NULLS LAST, created_at ASC",
                "ORDER BY COALESCE(start_at, '9999') ASC, created_at ASC",
            )
            async with db.execute(q, (*params, limit)) as cur:
                rows = await cur.fetchall()
        return [_row_to_appt(r) for r in rows]

    async def count_appointments_by_status(self) -> dict[str, int]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT status, COUNT(*) FROM appointments GROUP BY status"
            ) as cur:
                return {status: count for status, count in await cur.fetchall()}

    async def find_overlapping(
        self,
        service_slug: str,
        start_at: str,
        end_at: str,
        exclude_id: Optional[str] = None,
    ) -> list[Appointment]:
        """Return any CONFIRMED / REMINDED appointment for `service_slug` whose
        [start_at, end_at) overlaps the given window. Used by `reserve_slot`
        for the atomic double-booking check.

        Half-open intervals: an appointment that ends exactly at `start_at`
        is NOT considered overlapping. Buffer time, if needed, must be baked
        into the input window before this is called.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            q = (
                "SELECT * FROM appointments "
                "WHERE service_slug = ? "
                "  AND status IN ('confirmed', 'reminded') "
                "  AND start_at < ? "
                "  AND end_at > ? "
            )
            params: list[Any] = [service_slug, end_at, start_at]
            if exclude_id:
                q += "AND id != ? "
                params.append(exclude_id)
            async with db.execute(q, params) as cur:
                rows = await cur.fetchall()
        return [_row_to_appt(r) for r in rows]

    async def reserve_slot(
        self,
        appointment_id: str,
        service_slug: str,
        start_at: str,
        end_at: str,
    ) -> bool:
        """Atomic confirm: flips status REQUESTED → CONFIRMED only if no
        other CONFIRMED/REMINDED appointment overlaps the window.

        Returns True iff this caller owned the reservation. False means
        either the appointment had already been moved out of REQUESTED, or
        another concurrent caller grabbed an overlapping slot first.

        Implementation: a single UPDATE whose WHERE includes a NOT EXISTS
        anti-join, so SQLite evaluates them inside the same statement
        (effectively a transaction). Caller must have already populated
        start_at / end_at on the appointment row.
        """
        now = to_iso(now_utc())
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                UPDATE appointments
                   SET status = 'confirmed',
                       confirmed_at = ?,
                       updated_at = ?
                 WHERE id = ?
                   AND status = 'requested'
                   AND NOT EXISTS (
                       SELECT 1 FROM appointments AS other
                        WHERE other.service_slug = ?
                          AND other.id != appointments.id
                          AND other.status IN ('confirmed', 'reminded')
                          AND other.start_at < ?
                          AND other.end_at   > ?
                   )
                """,
                (now, now, appointment_id, service_slug, end_at, start_at),
            )
            await db.commit()
            return cur.rowcount > 0

    # ── reminders ─────────────────────────────────────────────────────────────

    async def insert_reminder(
        self, appointment_id: str, reminder: ReminderRecord
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO reminders
                  (id, appointment_id, channel, offset_minutes_before,
                   scheduled_for, status, subject, body, drafted_at,
                   approved_at, approved_by, sent_at, provider_message_id,
                   error, approval_token)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reminder.id,
                    appointment_id,
                    reminder.channel.value,
                    reminder.offset_minutes_before,
                    to_iso(reminder.scheduled_for),
                    reminder.status.value,
                    reminder.subject,
                    reminder.body,
                    to_iso(reminder.drafted_at),
                    to_iso(reminder.approved_at),
                    reminder.approved_by,
                    to_iso(reminder.sent_at),
                    reminder.provider_message_id,
                    reminder.error,
                    reminder.approval_token,
                ),
            )
            await db.commit()

    async def list_reminders_for_appointment(
        self, appointment_id: str
    ) -> list[ReminderRecord]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM reminders WHERE appointment_id = ? "
                "ORDER BY scheduled_for ASC",
                (appointment_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_reminder(r) for r in rows]

    async def list_due_reminders(
        self, before_iso: str, limit: int = 200
    ) -> list[tuple[str, ReminderRecord]]:
        """Return (appointment_id, reminder) pairs whose scheduled_for is on
        or before `before_iso` and whose status is APPROVED."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM reminders "
                "WHERE status = 'approved' "
                "  AND scheduled_for IS NOT NULL "
                "  AND scheduled_for <= ? "
                "ORDER BY scheduled_for ASC LIMIT ?",
                (before_iso, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [(r["appointment_id"], _row_to_reminder(r)) for r in rows]

    async def list_pending_reminders(
        self, limit: int = 200
    ) -> list[tuple[str, ReminderRecord]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM reminders WHERE status = 'drafted' "
                "ORDER BY drafted_at ASC LIMIT ?",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
        return [(r["appointment_id"], _row_to_reminder(r)) for r in rows]

    async def approve_reminder(
        self, reminder_id: str, approved_by: str = "cli"
    ) -> bool:
        """Atomic approve. Returns True if this call flipped the row."""
        now = to_iso(now_utc())
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                UPDATE reminders
                   SET status = 'approved',
                       approved_at = ?,
                       approved_by = ?
                 WHERE id = ? AND status = 'drafted'
                """,
                (now, approved_by, reminder_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def mark_reminder_sent(
        self,
        reminder_id: str,
        approval_token: str,
        provider_message_id: Optional[str],
    ) -> bool:
        """Atomic send-guard. Mirrors CustComm's `mark_draft_sent` pattern —
        only flips the row if status='approved' AND approval_token matches."""
        now = to_iso(now_utc())
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                UPDATE reminders
                   SET status = 'sent',
                       sent_at = ?,
                       provider_message_id = ?
                 WHERE id = ?
                   AND status = 'approved'
                   AND approval_token = ?
                """,
                (now, provider_message_id, reminder_id, approval_token),
            )
            await db.commit()
            return cur.rowcount > 0

    async def mark_reminder_failed(
        self, reminder_id: str, error: str
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE reminders SET status = 'failed', error = ? WHERE id = ?",
                (error, reminder_id),
            )
            await db.commit()

    async def count_reminders_sent_today(
        self, channel: ReminderChannel
    ) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM reminders "
                "WHERE status = 'sent' AND channel = ? "
                "  AND date(sent_at) = date('now')",
                (channel.value,),
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else 0

    # ── waitlist ──────────────────────────────────────────────────────────────

    async def upsert_waitlist_entry(self, entry: WaitlistEntry) -> WaitlistEntry:
        entry.updated_at = now_utc()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT id FROM waitlist WHERE id = ?", (entry.id,)
            ) as cur:
                existing = await cur.fetchone()

            if existing:
                await db.execute(
                    """
                    UPDATE waitlist SET
                      client_name=?, client_email=?, client_phone=?,
                      service_slug=?, desired_window_start=?,
                      desired_window_end=?, notes=?, status=?,
                      offered_appointment_id=?, offered_at=?,
                      offer_expires_at=?, offer_subject=?, offer_body=?,
                      updated_at=?
                    WHERE id=?
                    """,
                    (
                        entry.client.name,
                        ClientInfo.normalize_email(entry.client.email or ""),
                        entry.client.phone,
                        entry.service_slug,
                        to_iso(entry.desired_window_start),
                        to_iso(entry.desired_window_end),
                        entry.notes,
                        entry.status.value,
                        entry.offered_appointment_id,
                        to_iso(entry.offered_at),
                        to_iso(entry.offer_expires_at),
                        entry.offer_subject,
                        entry.offer_body,
                        to_iso(entry.updated_at),
                        entry.id,
                    ),
                )
            else:
                await db.execute(
                    """
                    INSERT INTO waitlist
                      (id, client_name, client_email, client_phone,
                       service_slug, desired_window_start, desired_window_end,
                       notes, status, offered_appointment_id, offered_at,
                       offer_expires_at, offer_subject, offer_body,
                       created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry.id,
                        entry.client.name,
                        ClientInfo.normalize_email(entry.client.email or ""),
                        entry.client.phone,
                        entry.service_slug,
                        to_iso(entry.desired_window_start),
                        to_iso(entry.desired_window_end),
                        entry.notes,
                        entry.status.value,
                        entry.offered_appointment_id,
                        to_iso(entry.offered_at),
                        to_iso(entry.offer_expires_at),
                        entry.offer_subject,
                        entry.offer_body,
                        to_iso(entry.created_at),
                        to_iso(entry.updated_at),
                    ),
                )
            await db.commit()
        return entry

    async def list_waitlist(
        self,
        status: Optional[WaitlistStatus] = None,
        service_slug: Optional[str] = None,
        limit: int = 100,
    ) -> list[WaitlistEntry]:
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status.value)
        if service_slug:
            where.append("service_slug = ?")
            params.append(service_slug)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            q = (
                f"SELECT * FROM waitlist {clause} "
                f"ORDER BY created_at ASC LIMIT ?"
            )
            async with db.execute(q, (*params, limit)) as cur:
                rows = await cur.fetchall()
        return [_row_to_waitlist(r) for r in rows]

    async def get_waitlist_entry(self, entry_id: str) -> Optional[WaitlistEntry]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM waitlist WHERE id = ?", (entry_id,)
            ) as cur:
                row = await cur.fetchone()
        return _row_to_waitlist(row) if row else None


# ── row <-> model helpers ─────────────────────────────────────────────────────


def _appt_to_row(a: Appointment) -> tuple[Any, ...]:
    return (
        a.id,
        a.source.value,
        a.external_id,
        a.client.name,
        ClientInfo.normalize_email(a.client.email or ""),
        a.client.phone,
        a.service_slug,
        a.service_name,
        to_iso(a.requested_at),
        to_iso(a.start_at),
        to_iso(a.end_at),
        a.duration_minutes,
        a.timezone,
        json.dumps(a.location.model_dump(mode="json")),
        a.status.value,
        a.confirmation_subject,
        a.confirmation_body,
        a.confirmation_status.value,
        a.confirmation_token,
        to_iso(a.confirmation_approved_at),
        to_iso(a.confirmation_sent_at),
        a.notes,
        a.intake_notes,
        json.dumps(a.tags),
        to_iso(a.no_show_at),
        a.no_show_followup_subject,
        a.no_show_followup_body,
        to_iso(a.confirmed_at),
        to_iso(a.cancelled_at),
        a.cancellation_reason,
        a.rescheduled_to_id,
        json.dumps(a.raw_data),
        to_iso(a.updated_at),
    )


def _row_to_appt(row: aiosqlite.Row) -> Appointment:
    loc_data = json.loads(row["location_json"] or "{}")
    if loc_data.get("kind") and not isinstance(loc_data["kind"], str):
        loc_data["kind"] = str(loc_data["kind"])
    location = Location(**loc_data) if loc_data else Location()
    return Appointment(
        id=row["id"],
        source=AppointmentSource(row["source"]),
        external_id=row["external_id"],
        client=ClientInfo(
            name=row["client_name"] or "",
            email=row["client_email"] or "",
            phone=row["client_phone"] or "",
        ),
        service_slug=row["service_slug"] or "",
        service_name=row["service_name"] or "",
        requested_at=parse_iso(row["requested_at"]),
        start_at=parse_iso(row["start_at"]),
        end_at=parse_iso(row["end_at"]),
        duration_minutes=row["duration_minutes"] or 30,
        timezone=row["timezone"] or "UTC",
        location=location,
        status=AppointmentStatus(row["status"]),
        confirmation_subject=row["confirmation_subject"] or "",
        confirmation_body=row["confirmation_body"] or "",
        confirmation_status=ReminderStatus(
            row["confirmation_status"] or "drafted"
        ),
        confirmation_token=row["confirmation_token"] or "",
        confirmation_approved_at=parse_iso(row["confirmation_approved_at"]),
        confirmation_sent_at=parse_iso(row["confirmation_sent_at"]),
        notes=row["notes"] or "",
        intake_notes=row["intake_notes"] or "",
        tags=json.loads(row["tags_json"] or "[]"),
        no_show_at=parse_iso(row["no_show_at"]),
        no_show_followup_subject=row["no_show_followup_subject"] or "",
        no_show_followup_body=row["no_show_followup_body"] or "",
        confirmed_at=parse_iso(row["confirmed_at"]),
        cancelled_at=parse_iso(row["cancelled_at"]),
        cancellation_reason=row["cancellation_reason"] or "",
        rescheduled_to_id=row["rescheduled_to_id"],
        raw_data=json.loads(row["raw_data_json"] or "{}"),
        created_at=parse_iso(row["created_at"]) or now_utc(),
        updated_at=parse_iso(row["updated_at"]) or now_utc(),
    )


def _row_to_reminder(row: aiosqlite.Row) -> ReminderRecord:
    return ReminderRecord(
        id=row["id"],
        channel=ReminderChannel(row["channel"]),
        offset_minutes_before=row["offset_minutes_before"] or 0,
        scheduled_for=parse_iso(row["scheduled_for"]),
        status=ReminderStatus(row["status"]),
        subject=row["subject"] or "",
        body=row["body"] or "",
        drafted_at=parse_iso(row["drafted_at"]) or now_utc(),
        approved_at=parse_iso(row["approved_at"]),
        approved_by=row["approved_by"],
        sent_at=parse_iso(row["sent_at"]),
        provider_message_id=row["provider_message_id"],
        error=row["error"] or "",
        approval_token=row["approval_token"],
    )


def _row_to_waitlist(row: aiosqlite.Row) -> WaitlistEntry:
    return WaitlistEntry(
        id=row["id"],
        client=ClientInfo(
            name=row["client_name"] or "",
            email=row["client_email"] or "",
            phone=row["client_phone"] or "",
        ),
        service_slug=row["service_slug"] or "",
        desired_window_start=parse_iso(row["desired_window_start"]),
        desired_window_end=parse_iso(row["desired_window_end"]),
        notes=row["notes"] or "",
        status=WaitlistStatus(row["status"]),
        offered_appointment_id=row["offered_appointment_id"],
        offered_at=parse_iso(row["offered_at"]),
        offer_expires_at=parse_iso(row["offer_expires_at"]),
        offer_subject=row["offer_subject"] or "",
        offer_body=row["offer_body"] or "",
        created_at=parse_iso(row["created_at"]) or now_utc(),
        updated_at=parse_iso(row["updated_at"]) or now_utc(),
    )
