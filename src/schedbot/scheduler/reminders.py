"""Reminder scheduling helpers.

Two responsibilities:
  - Given an Appointment, build the set of `ReminderRecord`s implied by
    `config.reminders.offsets_minutes_before` (each becomes a draft).
  - Pick the right `ReminderChannel` for each draft based on what contact
    info the customer provided — email if we have an address, SMS if we
    only have a phone number.

Drafting itself happens in `schedbot.ai.drafter.MessageDrafter`. This module
just plans + persists the records; the AI layer fills in subject/body when
the operator (or scheduler loop) asks for it.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from schedbot.config.loader import RemindersConfig, SchedBotConfig
from schedbot.crm.database import AppointmentDatabase
from schedbot.models import (
    Appointment,
    ReminderChannel,
    ReminderRecord,
    ReminderStatus,
)

logger = logging.getLogger(__name__)


class ReminderScheduler:
    def __init__(self, config: SchedBotConfig, db: AppointmentDatabase) -> None:
        self.config = config
        self.db = db

    async def enqueue_for_appointment(
        self, appointment: Appointment
    ) -> list[ReminderRecord]:
        """Create one DRAFTED ReminderRecord per configured offset for this
        appointment, persist them, and return them.

        Skips:
          - Appointments without a confirmed start_at.
          - Offsets that resolve to a `scheduled_for` already in the past
            (drafted right before / after the appointment).
        """
        if appointment.start_at is None:
            return []

        cfg = self.config.reminders
        channel = self._pick_channel(cfg, appointment)
        if channel is None:
            logger.info(
                f"Appointment {appointment.id}: no reachable channel "
                f"(client has no email or phone); skipping reminders."
            )
            return []

        out: list[ReminderRecord] = []
        for offset in cfg.offsets_minutes_before:
            if offset <= 0:
                continue
            scheduled = appointment.start_at - timedelta(minutes=offset)
            if scheduled <= appointment.created_at:
                # Reminder offset puts it in the past relative to creation.
                # That's a config / data inconsistency, not an error — log
                # and skip so the rest of the offsets still land.
                logger.debug(
                    f"Skipping reminder offset {offset}m for appt {appointment.id}: "
                    "scheduled_for would be in the past."
                )
                continue

            reminder = ReminderRecord(
                channel=channel,
                offset_minutes_before=offset,
                scheduled_for=scheduled,
                status=ReminderStatus.DRAFTED,
            )
            await self.db.insert_reminder(appointment.id, reminder)
            out.append(reminder)
        return out

    def _pick_channel(
        self, cfg: RemindersConfig, appointment: Appointment
    ) -> Optional[ReminderChannel]:
        for chan_str in cfg.channels:
            chan_str = chan_str.strip().lower()
            if chan_str == "email" and appointment.client.email:
                return ReminderChannel.EMAIL
            if chan_str == "sms" and appointment.client.phone:
                return ReminderChannel.SMS
        return None
