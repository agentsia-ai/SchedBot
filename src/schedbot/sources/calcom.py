"""cal.com REST API connector — the primary booking source.

The operator's public booking page is served by cal.com (e.g.
https://cal.com/agentsia/discovery-call), so cal.com is the recommended
default for any new SchedBot deployment. Auth is a single API key from
cal.com → Settings → Developer → API Keys (no OAuth dance, unlike Google
Calendar).

API reference: https://cal.com/docs/api-reference (v2 endpoints under
https://api.cal.com/v2). This connector uses the v2 paths and assumes
Bearer-token auth.

What it pulls:
  - `/bookings` — every booking on the operator's account, filtered by
    `after` cursor on subsequent polls.
  - `/event-types` — the operator's bookable services. Used at config
    bootstrap to suggest service slugs.

What it does NOT do:
  - Create bookings. cal.com is the booking-page-of-record; SchedBot
    listens to bookings the customer makes on cal.com, then layers
    confirmation + reminder + follow-up flows on top. Operator-initiated
    bookings (a phone call) go in via the `manual` source instead.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, AsyncIterator, Optional

import httpx

from schedbot._time import now_utc, parse_iso, to_iso
from schedbot.config.loader import APIKeys, SchedBotConfig
from schedbot.models import Location, LocationKind, RawBookingRequest
from schedbot.sources.base import BookingSource

logger = logging.getLogger(__name__)


class CalComSource(BookingSource):
    """Polls cal.com's `/bookings` endpoint for new + updated bookings."""

    def __init__(
        self, config: SchedBotConfig, keys: APIKeys, since: Optional[datetime] = None
    ) -> None:
        super().__init__(config, keys)
        self._since = since
        self.base_url = config.calcom.base_url.rstrip("/")
        if not keys.calcom_api_key:
            raise RuntimeError(
                "CALCOM_API_KEY is not set. Get a key from "
                "https://cal.com/settings/developer/api-keys and add it to .env."
            )

    async def fetch_new(self) -> AsyncIterator[RawBookingRequest]:
        cursor = self._since or now_utc()
        params: dict[str, Any] = {"limit": 100}
        if cursor:
            params["afterStart"] = to_iso(cursor)

        async with httpx.AsyncClient(timeout=30.0) as client:
            url = f"{self.base_url}/bookings"
            resp = await client.get(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {self.keys.calcom_api_key}",
                    "cal-api-version": "2024-08-13",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        bookings = data.get("data") or data.get("bookings") or []
        logger.info(f"cal.com /bookings returned {len(bookings)} entries")

        for b in bookings:
            envelope = _calcom_to_raw(b, event_kind="booking.created")
            if envelope is not None:
                yield envelope

    async def list_event_types(self) -> list[dict[str, Any]]:
        """Convenience: fetch the operator's event types so a downstream
        config-bootstrap helper can suggest matching `service.slug`s."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            url = f"{self.base_url}/event-types"
            resp = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {self.keys.calcom_api_key}",
                    "cal-api-version": "2024-06-14",
                },
            )
            resp.raise_for_status()
            payload = resp.json()
            return payload.get("data") or payload.get("event_types") or []


# ── decoder ──────────────────────────────────────────────────────────────────


def _calcom_to_raw(
    booking: dict[str, Any], event_kind: str = "booking.created"
) -> Optional[RawBookingRequest]:
    """Translate a cal.com booking object into a RawBookingRequest.

    cal.com's payload shape evolves; we look for both v1 and v2-ish
    field names where they differ, and fall through to None when a
    required field is missing rather than half-populating an envelope.
    """
    bid = booking.get("id") or booking.get("uid")
    if not bid:
        logger.warning(f"cal.com booking has no id: {booking}")
        return None

    # Attendee = the customer who booked. cal.com always returns a list
    # of attendees; index 0 is the primary booker.
    attendees = booking.get("attendees") or []
    primary = attendees[0] if attendees else {}

    client_email = (primary.get("email") or "").strip().lower()
    client_name = (primary.get("name") or "").strip()
    client_phone = (
        primary.get("phoneNumber")
        or primary.get("phone")
        or _extract_phone_from_responses(booking)
        or ""
    ).strip()

    # Event type — slug + name, both useful for our config matching.
    et = booking.get("eventType") or {}
    service_slug = (et.get("slug") or "").strip()
    service_name = (et.get("title") or et.get("name") or "").strip()
    duration = int(et.get("length") or booking.get("length") or 30)

    # Time window — v2 uses `start` / `end` (ISO 8601); some legacy webhook
    # payloads use `startTime` / `endTime`.
    start_iso = booking.get("start") or booking.get("startTime")
    end_iso = booking.get("end") or booking.get("endTime")
    start_at = parse_iso(start_iso) if start_iso else None
    end_at = parse_iso(end_iso) if end_iso else None

    # Location — cal.com bundles a lot of info into the `location` string;
    # we normalize the common cases and pass the raw value through in the
    # envelope's raw_payload for downstream code.
    location = _decode_location(booking)

    return RawBookingRequest(
        provider="calcom",
        provider_event_id=str(bid),
        event_kind=event_kind,
        client_name=client_name,
        client_email=client_email,
        client_phone=client_phone,
        service_slug=service_slug,
        service_name=service_name,
        requested_start_at=start_at,
        requested_end_at=end_at,
        duration_minutes=duration,
        timezone=primary.get("timeZone") or booking.get("timeZone") or "UTC",
        location=location,
        notes=(booking.get("description") or booking.get("notes") or ""),
        raw_payload=booking,
    )


def _decode_location(booking: dict[str, Any]) -> Optional[Location]:
    raw = booking.get("location") or ""
    if not raw:
        return None
    if isinstance(raw, dict):
        # Newer cal.com payloads sometimes return a typed object.
        kind_raw = (raw.get("type") or raw.get("kind") or "").lower()
        if "video" in kind_raw or "zoom" in kind_raw or "meet" in kind_raw:
            return Location(kind=LocationKind.VIDEO, link=raw.get("link") or "")
        if "phone" in kind_raw:
            return Location(
                kind=LocationKind.PHONE, phone_number=raw.get("phone") or ""
            )
        return Location(kind=LocationKind.IN_PERSON, address=raw.get("address") or "")

    # String form: cal.com encodes meetings as e.g. "integrations:daily" /
    # "integrations:zoom" / a raw URL / a postal address.
    s = str(raw).strip()
    lower = s.lower()
    if lower.startswith("http://") or lower.startswith("https://"):
        return Location(kind=LocationKind.VIDEO, link=s)
    if "phone" in lower or s.startswith("+") or s.replace(" ", "").isdigit():
        return Location(kind=LocationKind.PHONE, phone_number=s)
    return Location(kind=LocationKind.IN_PERSON, address=s)


def _extract_phone_from_responses(booking: dict[str, Any]) -> str:
    """cal.com surfaces custom-question answers under `responses` /
    `bookingFieldsResponses`. Look for anything that smells like a phone."""
    for key in ("responses", "bookingFieldsResponses", "customInputs"):
        block = booking.get(key)
        if not block:
            continue
        if isinstance(block, dict):
            for k, v in block.items():
                if "phone" in str(k).lower() and v:
                    return str(v)
        elif isinstance(block, list):
            for item in block:
                label = str(item.get("label") or item.get("name") or "").lower()
                value = item.get("value") or item.get("answer") or ""
                if "phone" in label and value:
                    return str(value)
    return ""
