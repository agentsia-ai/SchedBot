"""SchedBot Message Drafter.

Drafts the customer-facing prose for the four message types SchedBot owns:

  - confirmation         : "your appointment is booked for X"
  - reminder             : "just a heads-up — see you at X tomorrow"
  - cancellation         : "we received your cancellation"
  - rebooking offer      : "if you'd like to rebook, here's how"
  - no-show follow-up    : "we missed you today — would you like to rebook?"
  - reschedule options   : "here are some times that could work instead"
  - waitlist offer       : "a slot just opened — first dibs"

This is the generic engine implementation. To customize for a productized
agent (e.g. a named persona with a distinctive voice), either:
  1. Subclass `MessageDrafter` and override `SYSTEM_PROMPT` (and optionally
     any `draft_*` method), or
  2. Point `config.ai.drafter_prompt_path` at an external prompt file.

See CLAUDE.md → Customization Patterns for details.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import anthropic

from schedbot._time import format_local
from schedbot.config.loader import APIKeys, SchedBotConfig
from schedbot.models import Appointment, TimeSlot

logger = logging.getLogger(__name__)


DEFAULT_DRAFTER_PROMPT = """You draft short, human-sounding scheduling messages on behalf of a small
business. Every output is reviewed and approved by an operator before
anything is sent — your job is to produce the best draft, not the final
word.

Hard rules — you MUST follow all of these without exception:
  - NEVER invent times, prices, locations, or policies that aren't in the
    context the user message gives you. If the customer needs to know
    something you weren't told, say the operator will confirm.
  - Match the customer's tone. Formal customers get a formal draft;
    casual customers get a friendly one. Don't over-warm.
  - Keep it concise. Under 120 words for confirmations and reminders;
    under 180 for cancellation / rebooking / no-show follow-ups.
  - Don't open with filler ("I hope this email finds you well", "Thank
    you for reaching out", etc.). Open with the substance.
  - Use the local presentation timezone the user message specifies. Do
    not include raw ISO strings or +00:00 offsets in customer-facing
    prose — say "Tuesday at 2:30 PM" not "2026-05-04T18:30:00Z".
  - Do not include a signature block. The engine appends the agent
    signature separately (agent_name + business_name).
  - For SMS messages (the user message will say "sms"), keep the body
    under 320 characters and skip the subject. For email, include both
    a subject line and a body.

Return ONLY valid JSON — no preamble, no explanation outside the JSON —
in this exact shape:
{
  "subject": "<subject line, or empty string for SMS>",
  "body": "<message body, plain text, no signature>"
}"""


class MessageDrafter:
    """Drafts confirmations, reminders, follow-ups, and rebooking offers
    using Claude.

    Subclass this and override `SYSTEM_PROMPT` to define a tuned drafter
    with a custom voice. Per-deployment overrides can also be supplied via
    `config.ai.drafter_prompt_path`.
    """

    SYSTEM_PROMPT: str = DEFAULT_DRAFTER_PROMPT

    def __init__(self, config: SchedBotConfig, keys: APIKeys) -> None:
        self.config = config
        self.client = anthropic.AsyncAnthropic(api_key=keys.anthropic)
        self.model = config.ai.model
        self.max_chars = config.ai.max_message_chars
        self._system_prompt = self._load_system_prompt()

    def _load_system_prompt(self) -> str:
        """Resolution order:
          1. `config.ai.drafter_prompt_path` (if set and file exists)
          2. Class attribute `SYSTEM_PROMPT` (subclass-overridable)
        """
        override = self.config.ai.drafter_prompt_path
        if override:
            path = Path(override)
            if path.exists():
                logger.info(f"{type(self).__name__} using prompt override: {path}")
                return path.read_text(encoding="utf-8")
            logger.warning(
                f"drafter_prompt_path points at missing file: {path} — "
                f"falling back to {type(self).__name__}.SYSTEM_PROMPT"
            )
        return self.SYSTEM_PROMPT

    # ── public draft methods ──────────────────────────────────────────────

    async def draft_confirmation(
        self,
        appointment: Appointment,
        channel: str = "email",
        guidance: str = "",
    ) -> tuple[str, str]:
        """Draft a confirmation. Returns (subject, body)."""
        local_when = format_local(
            appointment.start_at,
            appointment.timezone or self.config.business.timezone,
        )
        ctx = self._appointment_context_block(appointment, local_when)
        return await self._draft(
            f"""Draft a {channel.upper()} CONFIRMATION for the booking below.

{ctx}

Tone: {self.config.business.communication_tone}
Channel: {channel}

Confirm that the appointment is booked, restate the local time and
location/link if provided, and (if email) close with a short "let us know
if anything changes". Do not include a signature.{_format_guidance(guidance)}

Return JSON.""",
            channel=channel,
        )

    async def draft_reminder(
        self,
        appointment: Appointment,
        offset_minutes_before: int,
        channel: str = "email",
        guidance: str = "",
    ) -> tuple[str, str]:
        """Draft a reminder for `offset_minutes_before` before start_at."""
        local_when = format_local(
            appointment.start_at,
            appointment.timezone or self.config.business.timezone,
        )
        offset_label = _humanize_offset(offset_minutes_before)
        ctx = self._appointment_context_block(appointment, local_when)
        return await self._draft(
            f"""Draft a {channel.upper()} REMINDER for the booking below. The reminder
will be delivered approximately {offset_label} before the appointment.

{ctx}

Tone: {self.config.business.communication_tone}
Channel: {channel}

Briefly remind the customer of the time and location/link, and tell them
how to reach us if they need to reschedule. Don't repeat the date if
sending less than 4 hours out — "in about an hour" reads better than the
full timestamp.{_format_guidance(guidance)}

Return JSON.""",
            channel=channel,
        )

    async def draft_cancellation(
        self,
        appointment: Appointment,
        reason: str = "",
        channel: str = "email",
        guidance: str = "",
    ) -> tuple[str, str]:
        local_when = format_local(
            appointment.start_at,
            appointment.timezone or self.config.business.timezone,
        )
        ctx = self._appointment_context_block(appointment, local_when)
        policy = self.config.outreach.cancellation_policy_text or ""
        rebook = ""
        if self.config.outreach.rebooking_offer_enabled:
            rebook = f"\n\nInvitation to rebook (use the operator's wording, do not change the offer): {self.config.outreach.rebooking_offer_text}"
        return await self._draft(
            f"""Draft a {channel.upper()} CANCELLATION acknowledgement for the booking below.

{ctx}
Cancellation reason from customer (may be empty): {reason or "(not given)"}

Tone: {self.config.business.communication_tone}
Channel: {channel}

Acknowledge the cancellation warmly, restate the appointment so the
customer knows we got the right one, and (if relevant) note the
cancellation policy. Do not assess fees or apply penalties — that's a
human decision.

Cancellation policy (verbatim if you reference it): {policy or "(none provided)"}{rebook}{_format_guidance(guidance)}

Return JSON.""",
            channel=channel,
        )

    async def draft_no_show_followup(
        self,
        appointment: Appointment,
        channel: str = "email",
        guidance: str = "",
    ) -> tuple[str, str]:
        local_when = format_local(
            appointment.start_at,
            appointment.timezone or self.config.business.timezone,
        )
        ctx = self._appointment_context_block(appointment, local_when)
        return await self._draft(
            f"""Draft a {channel.upper()} NO-SHOW FOLLOW-UP for the booking below.

{ctx}

Tone: {self.config.business.communication_tone}
Channel: {channel}

The customer did not show up. Be gracious — assume the best (something
came up). Don't accuse, don't shame. Offer to rebook with one short
sentence inviting a reply. Do not assess fees. Do not invent a new time
to offer.{_format_guidance(guidance)}

Return JSON.""",
            channel=channel,
        )

    async def draft_reschedule_options(
        self,
        appointment: Appointment,
        slots: list[TimeSlot],
        channel: str = "email",
        guidance: str = "",
    ) -> tuple[str, str]:
        local_when = format_local(
            appointment.start_at,
            appointment.timezone or self.config.business.timezone,
        )
        ctx = self._appointment_context_block(appointment, local_when)
        slot_lines = []
        for i, s in enumerate(slots):
            slot_lines.append(
                f"  [{i}] {format_local(s.start_at, s.timezone)} – "
                f"{format_local(s.end_at, s.timezone)}"
            )
        slots_block = "\n".join(slot_lines) or "  (no slots provided — ask for preference)"
        return await self._draft(
            f"""Draft a {channel.upper()} RESCHEDULE response for the booking below.

{ctx}

Tone: {self.config.business.communication_tone}
Channel: {channel}

Available alternative slots (use ONLY these — never invent a time):
{slots_block}

Acknowledge the reschedule, present the available alternatives in
human-friendly day/time phrasing (Tuesday at 2:30 PM, not raw ISO), and
ask the customer to confirm which one works (or to suggest another window
if none do).{_format_guidance(guidance)}

Return JSON.""",
            channel=channel,
        )

    async def draft_waitlist_offer(
        self,
        appointment: Appointment,
        offer_expires_at: Optional[datetime],
        channel: str = "email",
        guidance: str = "",
    ) -> tuple[str, str]:
        local_when = format_local(
            appointment.start_at,
            appointment.timezone or self.config.business.timezone,
        )
        expiry = format_local(
            offer_expires_at,
            appointment.timezone or self.config.business.timezone,
        ) if offer_expires_at else ""
        ctx = self._appointment_context_block(appointment, local_when)
        return await self._draft(
            f"""Draft a {channel.upper()} WAITLIST OFFER for the slot below.

{ctx}
Offer expires at: {expiry or "(no expiry)"}

Tone: {self.config.business.communication_tone}
Channel: {channel}

Tell the waitlisted customer a slot just opened, when it is, and invite
them to reply yes/no. If an expiry was provided, mention it conversationally
(e.g. "we'll hold this until Wednesday morning"). Don't pressure.{_format_guidance(guidance)}

Return JSON.""",
            channel=channel,
        )

    # ── shared call ──────────────────────────────────────────────────────

    async def _draft(self, user_prompt: str, channel: str = "email") -> tuple[str, str]:
        max_tokens = 600 if channel == "email" else 300
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=self._system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = response.content[0].text
        data = _parse_json_loosely(raw)

        subject = str(data.get("subject") or "").strip()
        body = str(data.get("body") or "").strip()

        if len(body) > self.max_chars:
            body = body[: self.max_chars].rstrip() + "…"
            logger.warning(
                f"{type(self).__name__} body exceeded max_chars={self.max_chars}; truncated."
            )

        body = self._append_signature(body, channel)
        return subject, body

    # ── helpers ──────────────────────────────────────────────────────────

    def _appointment_context_block(
        self, appt: Appointment, local_when: str
    ) -> str:
        loc = appt.location
        loc_line: str
        if loc.kind.value == "video" and loc.link:
            loc_line = f"Location: video call ({loc.link})"
        elif loc.kind.value == "phone" and loc.phone_number:
            loc_line = f"Location: phone call ({loc.phone_number})"
        elif loc.kind.value == "in_person" and loc.address:
            loc_line = f"Location: in person at {loc.address}"
        else:
            loc_line = "Location: (not specified)"

        client_line = appt.client.name or appt.client.email or "(unknown client)"
        return (
            f"Business: {self.config.business.name}\n"
            f"Service: {appt.service_name or appt.service_slug or '(unspecified)'}\n"
            f"Client: {client_line}\n"
            f"When (local time): {local_when or '(not yet scheduled)'}\n"
            f"Duration: {appt.duration_minutes} minutes\n"
            f"{loc_line}\n"
            f"Notes: {appt.intake_notes or '(none)'}"
        )

    def _append_signature(self, body: str, channel: str) -> str:
        template = (
            self.config.outreach.sms_signature
            if channel == "sms"
            else self.config.outreach.email_signature
        )
        if not template:
            return body
        sig = template.format(
            agent_name=self.config.agent_name,
            business_name=self.config.business.name,
        )
        if channel == "sms":
            return f"{body.strip()}\n{sig.strip()}"
        return f"{body.strip()}\n\n{sig.strip()}"


# ── helpers ──────────────────────────────────────────────────────────────────


def _parse_json_loosely(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


def _format_guidance(guidance: str) -> str:
    if not guidance:
        return ""
    return (
        "\n\nOperator guidance for THIS draft (overrides defaults when relevant):\n"
        f"{guidance.strip()}\n"
    )


def _humanize_offset(minutes: int) -> str:
    """Convert a minutes-before number into a human phrase the prompt can
    pivot off ('1 hour', '24 hours', '2 days')."""
    if minutes >= 1440 and minutes % 1440 == 0:
        d = minutes // 1440
        return f"{d} day{'s' if d != 1 else ''}"
    if minutes >= 60 and minutes % 60 == 0:
        h = minutes // 60
        return f"{h} hour{'s' if h != 1 else ''}"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"
