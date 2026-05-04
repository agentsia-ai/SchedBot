"""Booking-source ABCs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from schedbot.config.loader import APIKeys, SchedBotConfig
from schedbot.models import RawBookingRequest


class BookingSource(ABC):
    """Pull-style source — polls an external system for new bookings."""

    def __init__(self, config: SchedBotConfig, keys: APIKeys) -> None:
        self.config = config
        self.keys = keys

    @abstractmethod
    def fetch_new(self) -> AsyncIterator[RawBookingRequest]:
        """Yield new booking events since the last poll.

        Implementations decide what "new" means (cal.com `since=` cursor,
        Google Calendar sync token, Calendly webhook backlog). Engine-side
        ingest dedups by `(provider, provider_event_id)`, so returning
        replays is safe but wasteful.
        """
        ...


class WebhookReceiver(ABC):
    """Push-style receiver — handles a single inbound webhook payload."""

    def __init__(self, config: SchedBotConfig, keys: APIKeys) -> None:
        self.config = config
        self.keys = keys

    @abstractmethod
    async def handle(
        self, payload: dict[str, Any], headers: dict[str, str] | None = None
    ) -> RawBookingRequest:
        """Translate the provider's webhook body into a RawBookingRequest.

        Should raise on signature-verification failure when the deployment
        configures one — the surrounding HTTP layer should map that to a
        4xx response.
        """
        ...
