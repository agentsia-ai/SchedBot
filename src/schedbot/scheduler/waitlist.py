"""Waitlist management.

When a service is fully booked, customers can be added to the waitlist.
When a confirmed appointment cancels, this module promotes the
longest-waiting compatible entry — drafting an offer message for operator
approval (the engine never auto-sends).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from schedbot._time import now_utc
from schedbot.config.loader import SchedBotConfig
from schedbot.crm.database import AppointmentDatabase
from schedbot.models import (
    Appointment,
    ClientInfo,
    WaitlistEntry,
    WaitlistStatus,
)

logger = logging.getLogger(__name__)


class WaitlistManager:
    def __init__(self, config: SchedBotConfig, db: AppointmentDatabase) -> None:
        self.config = config
        self.db = db

    async def add(
        self,
        client: ClientInfo,
        service_slug: str,
        notes: str = "",
    ) -> Optional[WaitlistEntry]:
        if not self.config.waitlist.enabled:
            logger.info("Waitlist disabled in config; ignoring add().")
            return None
        entry = WaitlistEntry(
            client=client, service_slug=service_slug, notes=notes,
            status=WaitlistStatus.WAITING,
        )
        return await self.db.upsert_waitlist_entry(entry)

    async def find_promotion_candidate(
        self, service_slug: str
    ) -> Optional[WaitlistEntry]:
        """Return the longest-waiting entry for `service_slug`, or None."""
        if not self.config.waitlist.enabled:
            return None
        entries = await self.db.list_waitlist(
            status=WaitlistStatus.WAITING, service_slug=service_slug
        )
        return entries[0] if entries else None

    async def offer(
        self,
        entry: WaitlistEntry,
        appointment: Appointment,
        offer_subject: str = "",
        offer_body: str = "",
    ) -> WaitlistEntry:
        """Mark a waitlist entry as OFFERED against a freshly-opened
        appointment. The offer message body / subject are filled in by
        the AI drafter at the call site (this module just records them)."""
        ttl = timedelta(minutes=self.config.waitlist.offer_ttl_minutes)
        entry.status = WaitlistStatus.OFFERED
        entry.offered_appointment_id = appointment.id
        entry.offered_at = now_utc()
        entry.offer_expires_at = now_utc() + ttl
        entry.offer_subject = offer_subject
        entry.offer_body = offer_body
        return await self.db.upsert_waitlist_entry(entry)

    async def expire_overdue_offers(self) -> int:
        """Walk OFFERED entries whose offer_expires_at has passed and
        revert them to WAITING so the next promotion can run.

        Returns the number of entries reverted.
        """
        now = now_utc()
        count = 0
        for e in await self.db.list_waitlist(status=WaitlistStatus.OFFERED, limit=500):
            if e.offer_expires_at and e.offer_expires_at < now:
                e.status = WaitlistStatus.EXPIRED
                await self.db.upsert_waitlist_entry(e)
                count += 1
        return count
