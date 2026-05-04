"""Google Calendar API connector — secondary / optional.

For deployments where the operator doesn't use cal.com and wants SchedBot
to read (and optionally write) directly against a Google Calendar.

Auth: OAuth 2.0 via `google-auth-oauthlib`. On first use a local-server
flow opens a browser for consent; the resulting token is cached at
`keys.google_token_path` (default `./.google_calendar_token.json`,
gitignored). See `docs/API_KEYS.md` for the Google Cloud project setup
walkthrough — it's substantially more involved than the cal.com API key
path, which is why cal.com is the recommended default.

Scopes:
  - `calendar.readonly`         — pull events for availability checking
  - `calendar.events`           — write back when `write_enabled=True`
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Optional

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


GOOGLE_CALENDAR_SCOPES_RO = ["https://www.googleapis.com/auth/calendar.readonly"]
GOOGLE_CALENDAR_SCOPES_RW = ["https://www.googleapis.com/auth/calendar.events"]


class GoogleCalendarSource(BookingSource):
    """Polls a Google Calendar for events and surfaces them as bookings."""

    async def fetch_new(self) -> AsyncIterator[RawBookingRequest]:
        if not self.config.google_calendar.enabled:
            logger.info("Google Calendar source disabled in config; nothing to fetch.")
            return
            yield  # pragma: no cover - required for AsyncIterator typing

        service = _build_calendar_service(
            credentials_path=self.keys.google_credentials_path,
            token_path=self.keys.google_token_path,
            write_enabled=self.config.google_calendar.write_enabled,
        )
        cal_id = self.config.google_calendar.calendar_id or "primary"
        # Pull from "now" forward across the configured booking window so
        # this can also serve as a busy-time source for the availability
        # engine. The deduper at ingest time handles replays.
        time_min = now_utc()
        time_max = time_min + timedelta(
            days=self.config.scheduler.booking_window_days
        )
        resp = service.events().list(
            calendarId=cal_id,
            timeMin=to_iso(time_min),
            timeMax=to_iso(time_max),
            singleEvents=True,
            orderBy="startTime",
            maxResults=250,
        ).execute()

        events = resp.get("items") or []
        logger.info(f"Google Calendar returned {len(events)} event(s)")
        for event in events:
            envelope = _gcal_to_raw(event)
            if envelope is not None:
                yield envelope

    async def write_event(
        self, raw: RawBookingRequest
    ) -> Optional[str]:
        """Push a confirmed booking back to Google Calendar. Returns the
        provider-side event id, or None when write_enabled is False."""
        if not self.config.google_calendar.write_enabled:
            logger.info("Google Calendar write disabled; skipping write_event.")
            return None
        if raw.requested_start_at is None or raw.requested_end_at is None:
            raise ValueError("write_event requires both start and end times")

        service = _build_calendar_service(
            credentials_path=self.keys.google_credentials_path,
            token_path=self.keys.google_token_path,
            write_enabled=True,
        )
        body = {
            "summary": raw.service_name or raw.service_slug or "Appointment",
            "description": raw.notes or "",
            "start": {
                "dateTime": to_iso(raw.requested_start_at),
                "timeZone": raw.timezone or "UTC",
            },
            "end": {
                "dateTime": to_iso(raw.requested_end_at),
                "timeZone": raw.timezone or "UTC",
            },
            "attendees": (
                [{"email": raw.client_email, "displayName": raw.client_name}]
                if raw.client_email
                else []
            ),
        }
        if raw.location and raw.location.address:
            body["location"] = raw.location.address
        elif raw.location and raw.location.link:
            body["location"] = raw.location.link

        cal_id = self.config.google_calendar.calendar_id or "primary"
        created = service.events().insert(calendarId=cal_id, body=body).execute()
        return created.get("id")


# ── helpers ──────────────────────────────────────────────────────────────────


def _build_calendar_service(
    credentials_path: str, token_path: str, write_enabled: bool
) -> Any:
    """Build a Google Calendar API client. Performs the OAuth dance on
    first run and caches the refresh token to `token_path`."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    scopes = GOOGLE_CALENDAR_SCOPES_RW if write_enabled else GOOGLE_CALENDAR_SCOPES_RO

    token_file = Path(token_path)
    creds: Any = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_path:
                raise RuntimeError(
                    "GOOGLE_CALENDAR_CREDENTIALS_PATH is not set. Download OAuth "
                    "client credentials from Google Cloud Console (APIs & Services "
                    "→ Credentials → Desktop app) and point the env var at the "
                    "downloaded JSON file. See docs/API_KEYS.md for the full walkthrough."
                )
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, scopes)
            creds = flow.run_local_server(port=0)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json(), encoding="utf-8")
        logger.info(f"Cached Google Calendar OAuth token at {token_file}")

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _gcal_to_raw(event: dict[str, Any]) -> Optional[RawBookingRequest]:
    """Translate a Google Calendar event into a RawBookingRequest."""
    eid = event.get("id")
    if not eid:
        return None

    # All-day events have `date` instead of `dateTime`; we don't model
    # those as appointments — skip with a debug log.
    start_dt = (event.get("start") or {}).get("dateTime")
    end_dt = (event.get("end") or {}).get("dateTime")
    if not start_dt or not end_dt:
        logger.debug(f"Skipping all-day Google Calendar event {eid}")
        return None

    attendees = event.get("attendees") or []
    primary = next(
        (a for a in attendees if not a.get("organizer")),
        attendees[0] if attendees else {},
    )

    location_str = (event.get("location") or "").strip()
    location: Optional[Location] = None
    if location_str:
        if location_str.startswith("http"):
            location = Location(kind=LocationKind.VIDEO, link=location_str)
        else:
            location = Location(kind=LocationKind.IN_PERSON, address=location_str)

    start_at = parse_iso(start_dt)
    end_at = parse_iso(end_dt)
    duration = 30
    if start_at and end_at:
        duration = max(1, int((end_at - start_at).total_seconds() // 60))

    return RawBookingRequest(
        provider=AppointmentSource.GOOGLE_CALENDAR.value,
        provider_event_id=str(eid),
        event_kind="booking.created",
        client_name=str(primary.get("displayName") or "").strip(),
        client_email=str(primary.get("email") or "").strip().lower(),
        service_name=str(event.get("summary") or "").strip(),
        requested_start_at=start_at,
        requested_end_at=end_at,
        duration_minutes=duration,
        timezone=(event.get("start") or {}).get("timeZone") or "UTC",
        location=location,
        notes=str(event.get("description") or ""),
        raw_payload=event,
    )
