"""Calendly API connector — alternative to cal.com.

For deployments where the operator already runs Calendly. Auth is a
personal access token from `https://calendly.com/integrations/api_webhooks`.
This connector pulls scheduled events for the configured user/org and
maps them onto SchedBot's `RawBookingRequest`.

Reference: https://developer.calendly.com/api-docs

Status: implemented in v1, lightly exercised. cal.com is the recommended
primary path — Calendly is here so a deployment can switch without
restructuring the package.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, AsyncIterator, Optional

import httpx

from schedbot._time import now_utc, parse_iso, to_iso
from schedbot.config.loader import APIKeys, SchedBotConfig
from schedbot.models import (
    AppointmentSource,
    Location,
    LocationKind,
    RawBookingRequest,
)
from schedbot.sources.base import BookingSource

logger = logging.getLogger(__name__)


CALENDLY_BASE_URL = "https://api.calendly.com"


class CalendlySource(BookingSource):
    async def fetch_new(self) -> AsyncIterator[RawBookingRequest]:
        if not self.config.calendly.enabled:
            logger.info("Calendly source disabled in config; nothing to fetch.")
            return
            yield  # pragma: no cover - required for AsyncIterator typing

        if not self.keys.calendly_api_key:
            raise RuntimeError(
                "CALENDLY_API_KEY is not set. Get a personal access token "
                "from https://calendly.com/integrations/api_webhooks."
            )

        scope_uri = (
            self.config.calendly.user_uri or self.config.calendly.organization_uri
        )
        if not scope_uri:
            raise RuntimeError(
                "Either calendly.user_uri or calendly.organization_uri must be set "
                "in config.yaml so we know whose events to fetch."
            )

        params: dict[str, Any] = {
            "min_start_time": to_iso(now_utc()),
            "max_start_time": to_iso(
                now_utc()
                + timedelta(days=self.config.scheduler.booking_window_days)
            ),
            "count": 100,
            "sort": "start_time:asc",
        }
        if self.config.calendly.user_uri:
            params["user"] = self.config.calendly.user_uri
        else:
            params["organization"] = self.config.calendly.organization_uri

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{CALENDLY_BASE_URL}/scheduled_events",
                params=params,
                headers={"Authorization": f"Bearer {self.keys.calendly_api_key}"},
            )
            resp.raise_for_status()
            payload = resp.json()
            events = payload.get("collection") or []
            logger.info(f"Calendly returned {len(events)} event(s)")

            for event in events:
                # Calendly puts the invitee on a separate endpoint. Pull the
                # first invitee for each event so we can populate client info.
                invitee = await _fetch_first_invitee(client, event, self.keys)
                envelope = _calendly_to_raw(event, invitee)
                if envelope is not None:
                    yield envelope


async def _fetch_first_invitee(
    client: httpx.AsyncClient, event: dict[str, Any], keys: APIKeys
) -> dict[str, Any]:
    invitees_url = event.get("invitees_url") or (
        f"{event.get('uri', '')}/invitees" if event.get("uri") else ""
    )
    if not invitees_url:
        return {}
    try:
        resp = await client.get(
            invitees_url,
            headers={"Authorization": f"Bearer {keys.calendly_api_key}"},
        )
        resp.raise_for_status()
        invitees = (resp.json() or {}).get("collection") or []
        return invitees[0] if invitees else {}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Failed to fetch Calendly invitees for {invitees_url}: {e}")
        return {}


def _calendly_to_raw(
    event: dict[str, Any], invitee: dict[str, Any]
) -> Optional[RawBookingRequest]:
    uri = event.get("uri") or ""
    if not uri:
        return None

    # The event URI doubles as a stable id ("https://api.calendly.com/scheduled_events/<uuid>").
    eid = uri.rsplit("/", 1)[-1]

    start_at = parse_iso(event.get("start_time"))
    end_at = parse_iso(event.get("end_time"))
    duration = 30
    if start_at and end_at:
        duration = max(1, int((end_at - start_at).total_seconds() // 60))

    location = _decode_calendly_location(event.get("location") or {})

    return RawBookingRequest(
        provider=AppointmentSource.CALENDLY.value,
        provider_event_id=eid,
        event_kind="booking.created",
        client_name=str(invitee.get("name") or "").strip(),
        client_email=str(invitee.get("email") or "").strip().lower(),
        client_phone=str(invitee.get("text_reminder_number") or "").strip(),
        service_slug="",
        service_name=str(event.get("name") or "").strip(),
        requested_start_at=start_at,
        requested_end_at=end_at,
        duration_minutes=duration,
        timezone=str(invitee.get("timezone") or "UTC"),
        location=location,
        notes="",
        raw_payload={"event": event, "invitee": invitee},
    )


def _decode_calendly_location(loc: dict[str, Any]) -> Optional[Location]:
    if not loc:
        return None
    kind = (loc.get("type") or "").lower()
    if kind in {"physical", "outbound_call", "inbound_call"}:
        if "call" in kind:
            return Location(
                kind=LocationKind.PHONE,
                phone_number=str(loc.get("location") or ""),
            )
        return Location(kind=LocationKind.IN_PERSON, address=str(loc.get("location") or ""))
    if kind in {"google_conference", "zoom_conference", "microsoft_teams_conference",
                "gotomeeting", "webex_conference", "custom"}:
        return Location(
            kind=LocationKind.VIDEO,
            link=str(loc.get("join_url") or loc.get("location") or ""),
        )
    return Location(kind=LocationKind.IN_PERSON, address=str(loc.get("location") or ""))
