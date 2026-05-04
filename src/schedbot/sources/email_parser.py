"""Booking-request email parser.

Translates a free-text inbound email into a `RawBookingRequest`. Two-step:

  1. Heuristic pre-pass — keyword search to bias the AI classifier and
     pre-fill obvious fields (sender email, subject as service hint).
  2. Claude-backed extraction — when the heuristic is ambiguous, the
     parser asks Claude to pick the service and infer the requested time.

This module is the email-source-side counterpart to CustComm's inbox
connectors. It does NOT poll an email backend — that's a deployment
concern (CustComm itself can be the upstream when both engines run side
by side, or a deployment can wire the email parser behind a Lambda
triggered by SES, an Outlook Graph subscription, etc.). All the parser
needs is a (sender, subject, body) triple.

Heuristic-only mode is the default — the AI extraction path is opt-in via
the `use_ai` argument so the engine can run without an Anthropic key
when only webhooks are wired up.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Optional

import anthropic
from dateutil import parser as dateparser

from schedbot._time import now_utc, parse_iso
from schedbot.config.loader import APIKeys, SchedBotConfig
from schedbot.models import (
    AppointmentSource,
    Location,
    LocationKind,
    RawBookingRequest,
)

logger = logging.getLogger(__name__)


_PHONE_RE = re.compile(r"(\+?\d[\d\s().-]{7,}\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


class BookingEmailParser:
    """Best-effort parse of an inbound booking-related email.

    Produces a `RawBookingRequest` with everything it could pull. Fields
    that are uncertain are left empty so downstream code (the classifier,
    the operator review) can fill in via the standard human-approval flow.
    """

    def __init__(self, config: SchedBotConfig, keys: APIKeys) -> None:
        self.config = config
        self.keys = keys
        self._anthropic: Optional[anthropic.AsyncAnthropic] = None

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._anthropic is None:
            if not self.keys.anthropic:
                raise RuntimeError(
                    "ANTHROPIC_API_KEY is not set; set use_ai=False on parse() "
                    "to run without it."
                )
            self._anthropic = anthropic.AsyncAnthropic(api_key=self.keys.anthropic)
        return self._anthropic

    async def parse(
        self,
        sender: str,
        subject: str,
        body: str,
        provider_event_id: Optional[str] = None,
        use_ai: bool = True,
    ) -> RawBookingRequest:
        """Parse a single email into a RawBookingRequest."""
        body_text = body or ""
        sender_norm = (sender or "").strip()
        subject_norm = (subject or "").strip()

        # Heuristic extraction — these are cheap and don't need the model.
        client_email = _extract_email(sender_norm) or _extract_email(body_text)
        client_phone = _extract_phone(body_text)
        client_name = _extract_name(sender_norm)
        requested_at = _extract_datetime(body_text + "\n" + subject_norm)
        service_slug = self._guess_service_slug(body_text + "\n" + subject_norm)

        envelope = RawBookingRequest(
            provider=AppointmentSource.EMAIL.value,
            provider_event_id=provider_event_id or "",
            event_kind="booking.created",
            client_name=client_name,
            client_email=client_email,
            client_phone=client_phone,
            service_slug=service_slug,
            service_name=self._service_name_for(service_slug),
            requested_start_at=requested_at,
            requested_end_at=None,
            duration_minutes=self._duration_for(service_slug),
            timezone=self.config.business.timezone,
            location=self._default_location_for(service_slug),
            notes=body_text.strip()[:1000],
            raw_payload={"sender": sender_norm, "subject": subject_norm, "body": body_text},
        )

        if use_ai and (not envelope.service_slug or envelope.requested_start_at is None):
            envelope = await self._ai_fill(envelope, body_text, subject_norm)
        return envelope

    # ── AI fill ──────────────────────────────────────────────────────────

    async def _ai_fill(
        self, envelope: RawBookingRequest, body: str, subject: str
    ) -> RawBookingRequest:
        services = [
            {"slug": s.slug, "name": s.name, "duration_minutes": s.duration_minutes}
            for s in self.config.business.services
        ]
        if not services:
            return envelope
        prompt = f"""Extract structured booking-request fields from this customer email.

Available services (pick exactly one slug, or "" if uncertain):
{json.dumps(services, indent=2)}

Email subject: {subject}
Email body:
---
{body.strip()}
---

Return ONLY JSON in this exact shape:
{{
  "service_slug": "<one of the slugs above, or empty string>",
  "requested_start_at": "<ISO 8601 datetime or empty string>",
  "duration_minutes": <integer>,
  "reasoning": "<1 short sentence>"
}}"""
        try:
            response = await self.client.messages.create(
                model=self.config.ai.model,
                max_tokens=400,
                system=(
                    "You are an extractor. Return only JSON matching the shape "
                    "the user message specifies; never invent fields the email "
                    "doesn't mention."
                ),
                messages=[{"role": "user", "content": prompt}],
            )
            data = _parse_json_loosely(response.content[0].text)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Email parser AI fill failed: {e}")
            return envelope

        slug = (data.get("service_slug") or "").strip()
        if slug and not envelope.service_slug:
            envelope.service_slug = slug
            envelope.service_name = self._service_name_for(slug)
            envelope.duration_minutes = (
                int(data.get("duration_minutes") or 0)
                or self._duration_for(slug)
            )
            envelope.location = self._default_location_for(slug) or envelope.location

        when = data.get("requested_start_at")
        if when and envelope.requested_start_at is None:
            envelope.requested_start_at = parse_iso(when)
        return envelope

    # ── service catalog helpers ──────────────────────────────────────────

    def _service_name_for(self, slug: str) -> str:
        for s in self.config.business.services:
            if s.slug == slug:
                return s.name
        return ""

    def _duration_for(self, slug: str) -> int:
        for s in self.config.business.services:
            if s.slug == slug:
                return s.duration_minutes
        return 30

    def _default_location_for(self, slug: str) -> Optional[Location]:
        for s in self.config.business.services:
            if s.slug != slug:
                continue
            kind = s.location_kind.lower()
            if kind == "video":
                return Location(kind=LocationKind.VIDEO, link=s.default_video_link)
            if kind == "phone":
                return Location(kind=LocationKind.PHONE)
            return Location(kind=LocationKind.IN_PERSON, address=s.default_address)
        return None

    def _guess_service_slug(self, text: str) -> str:
        text_lower = text.lower()
        for s in self.config.business.services:
            if s.slug and s.slug.lower() in text_lower:
                return s.slug
            if s.name and s.name.lower() in text_lower:
                return s.slug
        return ""


# ── extraction helpers ──────────────────────────────────────────────────────


def _extract_email(text: str) -> str:
    m = _EMAIL_RE.search(text or "")
    return m.group(0).lower() if m else ""


def _extract_phone(text: str) -> str:
    m = _PHONE_RE.search(text or "")
    if not m:
        return ""
    cleaned = re.sub(r"[^\d+]", "", m.group(0))
    return cleaned if len(cleaned) >= 7 else ""


def _extract_name(sender: str) -> str:
    """`Jane Doe <jane@example.com>` → `Jane Doe`. Falls back to email local part."""
    if not sender:
        return ""
    if "<" in sender:
        return sender.split("<", 1)[0].strip(" \"'")
    if "@" in sender:
        return sender.split("@", 1)[0].replace(".", " ").title()
    return sender


def _extract_datetime(text: str) -> Optional[datetime]:
    """Try dateutil's fuzzy parser; refuse anything in the past or
    >180 days out (probable misparse)."""
    if not text:
        return None
    try:
        # `default` anchors relative phrases ("Friday at 2") to today's date.
        parsed = dateparser.parse(text, fuzzy=True, default=datetime.utcnow())
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        from datetime import timezone

        parsed = parsed.replace(tzinfo=timezone.utc)
    now = now_utc()
    if parsed < now - timedelta(hours=1):
        return None
    if parsed > now + timedelta(days=180):
        return None
    return parsed


def _parse_json_loosely(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())
