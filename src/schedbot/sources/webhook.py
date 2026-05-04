"""Generic booking-request webhook receiver.

Accepts inbound booking requests from a website contact form, an embedded
booking widget, or any custom HTTP integration. The payload shape is
deliberately small and provider-neutral so a deployment can wire it up
behind any HTTP layer (FastAPI, Lambda, Cloudflare Worker, etc.) without
SchedBot taking a dependency on one.

Expected payload (fields are all optional; minimum is `client_email`
+ `service_slug`):

    {
      "client_name":  "Jane Doe",
      "client_email": "jane@example.com",
      "client_phone": "+15551234567",
      "service_slug": "discovery-call",
      "requested_start_at": "2026-05-04T18:30:00Z",
      "duration_minutes": 30,
      "timezone": "America/Chicago",
      "notes":   "Looking for a quote on lawn care",
      "location": {"kind": "video", "link": "..."},
      "external_id": "form-submission-1234"
    }

The `external_id` field is what the caller's system uses to dedup replays.
If not provided, every retry will create a fresh appointment — so wire one
in if your form host can give you a stable id.

Signature verification: when `WEBHOOK_SIGNING_SECRET` is set, the receiver
expects an HMAC-SHA256 hex digest of the raw body in the configured
`hmac_header` (default `X-SchedBot-Signature`). Empty secret skips the check
— only safe behind an authenticated edge.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Optional

from schedbot._time import parse_iso
from schedbot.config.loader import APIKeys, SchedBotConfig
from schedbot.models import (
    AppointmentSource,
    Location,
    LocationKind,
    RawBookingRequest,
)
from schedbot.sources.base import WebhookReceiver

logger = logging.getLogger(__name__)


class GenericBookingWebhookReceiver(WebhookReceiver):
    async def handle(
        self,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        raw_body: bytes | None = None,
    ) -> RawBookingRequest:
        secret = self.keys.webhook_signing_secret
        if secret:
            if raw_body is None:
                raise ValueError(
                    "raw_body is required for signature verification "
                    "(pass the unparsed request body bytes)."
                )
            sig_hdr = self.config.webhook.hmac_header
            sig = (headers or {}).get(sig_hdr) or (headers or {}).get(sig_hdr.lower())
            if not sig:
                raise ValueError(f"Missing {sig_hdr} header")
            if not _verify_signature(secret, raw_body, sig):
                raise ValueError("Invalid signature")

        client_email = str(payload.get("client_email") or "").strip().lower()
        service_slug = str(payload.get("service_slug") or "").strip()
        if not client_email or not service_slug:
            raise ValueError(
                "Webhook payload must include both `client_email` and `service_slug`."
            )

        location = _decode_location(payload.get("location"))
        start_at = parse_iso(payload.get("requested_start_at"))
        end_at = parse_iso(payload.get("requested_end_at"))

        return RawBookingRequest(
            provider=AppointmentSource.WEB.value,
            provider_event_id=str(payload.get("external_id") or ""),
            event_kind="booking.created",
            client_name=str(payload.get("client_name") or "").strip(),
            client_email=client_email,
            client_phone=str(payload.get("client_phone") or "").strip(),
            service_slug=service_slug,
            service_name=str(payload.get("service_name") or "").strip(),
            requested_start_at=start_at,
            requested_end_at=end_at,
            duration_minutes=int(payload.get("duration_minutes") or 30),
            timezone=str(payload.get("timezone") or "UTC"),
            location=location,
            notes=str(payload.get("notes") or ""),
            raw_payload=payload,
        )


def _decode_location(raw: Any) -> Optional[Location]:
    if not raw:
        return None
    if isinstance(raw, str):
        if raw.startswith("http"):
            return Location(kind=LocationKind.VIDEO, link=raw)
        return Location(kind=LocationKind.IN_PERSON, address=raw)
    if isinstance(raw, dict):
        kind = str(raw.get("kind") or raw.get("type") or "in_person").lower()
        if kind == "video":
            return Location(kind=LocationKind.VIDEO, link=str(raw.get("link") or ""))
        if kind == "phone":
            return Location(
                kind=LocationKind.PHONE,
                phone_number=str(raw.get("phone_number") or raw.get("phone") or ""),
            )
        return Location(kind=LocationKind.IN_PERSON, address=str(raw.get("address") or ""))
    return None


def _verify_signature(secret: str, body: bytes, header: str) -> bool:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    candidate = header.strip()
    if candidate.startswith("sha256="):
        candidate = candidate[len("sha256=") :]
    return hmac.compare_digest(digest, candidate)
