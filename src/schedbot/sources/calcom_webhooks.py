"""cal.com webhook receiver.

Accepts the four event types SchedBot cares about:
  - BOOKING_CREATED       → status REQUESTED → reserve → CONFIRMED
  - BOOKING_RESCHEDULED   → previous appt RESCHEDULED, successor REQUESTED
  - BOOKING_CANCELLED     → status CANCELLED, optional rebooking offer
  - MEETING_ENDED         → COMPLETED (or NO_SHOW if attendee never joined,
                            depending on the payload's `noShow` flag)

cal.com's webhook signatures are HMAC-SHA256 of the raw request body using
the secret shown in `Settings → Developer → Webhooks`. The verifier here
expects the secret in `keys.calcom_webhook_secret`; pass it the raw body
bytes (not the parsed JSON) so the digest matches.

Reference: https://cal.com/docs/api-reference/v2/webhooks
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Optional

from schedbot.config.loader import APIKeys, SchedBotConfig
from schedbot.models import RawBookingRequest
from schedbot.sources.base import WebhookReceiver
from schedbot.sources.calcom import _calcom_to_raw

logger = logging.getLogger(__name__)


# Map cal.com triggerEvent values → our internal event_kind strings.
_EVENT_KIND_MAP = {
    "BOOKING_CREATED": "booking.created",
    "BOOKING_RESCHEDULED": "booking.rescheduled",
    "BOOKING_CANCELLED": "booking.cancelled",
    "BOOKING_PAID": "booking.paid",
    "MEETING_ENDED": "meeting.ended",
    "MEETING_STARTED": "meeting.started",
}


class CalComWebhookReceiver(WebhookReceiver):
    """Decodes a cal.com webhook payload into a RawBookingRequest.

    Usage from an HTTP layer (FastAPI, Lambda, etc.):

        receiver = CalComWebhookReceiver(config, keys)
        envelope = await receiver.handle(
            payload=json.loads(body),
            headers=dict(request.headers),
            raw_body=body,           # required if signature verification on
        )

    The HTTP layer is responsible for returning a 4xx when `handle` raises
    `ValueError("Invalid signature")` and persisting the envelope through
    `service.ingest_booking_event(...)` on success.
    """

    async def handle(
        self,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        raw_body: bytes | None = None,
    ) -> RawBookingRequest:
        secret = self.keys.calcom_webhook_secret
        if secret:
            if raw_body is None:
                raise ValueError(
                    "raw_body is required for signature verification "
                    "(pass the unparsed request body bytes)."
                )
            sig_header = (headers or {}).get("X-Cal-Signature-256") or (
                headers or {}
            ).get("x-cal-signature-256")
            if not sig_header:
                raise ValueError("Missing X-Cal-Signature-256 header")
            if not self._verify_signature(secret, raw_body, sig_header):
                raise ValueError("Invalid signature")

        trigger = payload.get("triggerEvent") or payload.get("type") or "BOOKING_CREATED"
        event_kind = _EVENT_KIND_MAP.get(trigger, "booking.created")
        booking = payload.get("payload") or payload  # cal.com nests under `payload`

        envelope = _calcom_to_raw(booking, event_kind=event_kind)
        if envelope is None:
            raise ValueError(f"Could not decode cal.com webhook payload for {trigger}")

        # MEETING_ENDED carries an explicit no-show flag in newer versions.
        if event_kind == "meeting.ended":
            no_show = bool(booking.get("noShow") or payload.get("noShow"))
            envelope.event_kind = "meeting.no_show" if no_show else "meeting.ended"

        logger.info(
            f"cal.com webhook decoded: trigger={trigger} "
            f"event_kind={envelope.event_kind} booking_id={envelope.provider_event_id}"
        )
        return envelope

    @staticmethod
    def _verify_signature(secret: str, body: bytes, header: str) -> bool:
        """Constant-time compare against an HMAC-SHA256 hex digest."""
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        # cal.com sends the bare hex; some forks prefix with "sha256=" — accept either.
        candidate = header.strip()
        if candidate.startswith("sha256="):
            candidate = candidate[len("sha256=") :]
        return hmac.compare_digest(digest, candidate)


def event_kind_from_trigger(trigger: str) -> Optional[str]:
    """Public mapping helper for code that wants to log / route triggers
    without instantiating the receiver. Returns None on unknown triggers."""
    return _EVENT_KIND_MAP.get(trigger)
