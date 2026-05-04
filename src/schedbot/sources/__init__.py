"""Booking-source connectors.

Each backend exposes a single small surface so the engine can stay
provider-neutral:

  - Pull-style sources (cal.com REST, Google Calendar, Calendly) implement
    `BookingSource.fetch_new()` returning an async iterator of
    `RawBookingRequest` envelopes.
  - Push-style receivers (cal.com webhooks, generic webhook, email parser)
    expose a `handle()` method that takes a raw payload and returns a
    `RawBookingRequest`.

Service code in `schedbot.service` is responsible for mapping
`RawBookingRequest` → `Appointment` and persisting through the database.
"""

from __future__ import annotations

from schedbot.sources.base import BookingSource, WebhookReceiver

__all__ = ["BookingSource", "WebhookReceiver"]
