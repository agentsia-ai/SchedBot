"""SchedBot Core Data Models.

Central entities used across sources, AI, scheduler, and CRM layers. Every
layer speaks in these objects — never raw dicts.

Mirrors the LeadGen `Lead` and CustComm `Thread` / `Message` patterns: one
canonical model per concept, Pydantic v2 throughout, sensible defaults so
the engine never has to reach for `None` checks in the hot path.
"""

from __future__ import annotations

from datetime import datetime, time
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, EmailStr, Field

from schedbot._time import now_utc


# ── Enums ─────────────────────────────────────────────────────────────────────


class AppointmentSource(str, Enum):
    """Where a booking request came from."""

    WEB = "web"                  # website contact form / embedded widget
    EMAIL = "email"              # parsed inbound email
    SMS = "sms"                  # Twilio SMS
    PHONE = "phone"              # operator-entered after a phone call
    CALCOM = "calcom"            # cal.com webhook (primary integration)
    GOOGLE_CALENDAR = "google_calendar"
    CALENDLY = "calendly"
    ACUITY = "acuity"
    MANUAL = "manual"            # operator-entered with no specific origin


class AppointmentStatus(str, Enum):
    """Lifecycle of an appointment.

    Allowed transitions (the database does not enforce them; service-layer
    code does):
        REQUESTED  → CONFIRMED  → REMINDED  → COMPLETED
                                            → NO_SHOW
                   → RESCHEDULED → CONFIRMED → ...
                   → CANCELLED
    """

    REQUESTED = "requested"      # someone asked, slot not yet locked
    CONFIRMED = "confirmed"      # slot is locked + held on the calendar
    REMINDED = "reminded"        # at least one reminder has been sent
    COMPLETED = "completed"      # the appointment happened
    CANCELLED = "cancelled"      # the customer or operator cancelled
    NO_SHOW = "no_show"          # the customer didn't show up
    RESCHEDULED = "rescheduled"  # superseded by a follow-on appointment


class LocationKind(str, Enum):
    IN_PERSON = "in_person"
    VIDEO = "video"
    PHONE = "phone"


class ReminderChannel(str, Enum):
    EMAIL = "email"
    SMS = "sms"


class ReminderStatus(str, Enum):
    DRAFTED = "drafted"          # message drafted, awaiting approval
    APPROVED = "approved"        # operator approved; queued for send
    SENT = "sent"                # delivered to recipient
    FAILED = "failed"            # provider returned an error
    CANCELLED = "cancelled"      # appt cancelled before reminder fired


class WaitlistStatus(str, Enum):
    WAITING = "waiting"          # on the waitlist for a slot
    OFFERED = "offered"          # we found a slot; offer pending response
    BOOKED = "booked"            # accepted the offer; appointment created
    DECLINED = "declined"        # declined the offer
    EXPIRED = "expired"          # offer timed out


class RequestKind(str, Enum):
    """Output of the request classifier — what is the customer asking for?"""

    NEW_BOOKING = "new_booking"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"
    QUESTION = "question"        # not a booking action — escalate to ops
    UNCERTAIN = "uncertain"

    @classmethod
    def values(cls) -> list[str]:
        return [r.value for r in cls]


# ── Nested value objects ──────────────────────────────────────────────────────


class ClientInfo(BaseModel):
    """The person on the receiving end of an appointment."""

    name: str = ""
    email: str = ""              # normalized lowercase (DB UNIQUE-by-appointment)
    phone: str = ""              # E.164 preferred, but not enforced

    @staticmethod
    def normalize_email(raw: str) -> str:
        return raw.strip().lower()


class Location(BaseModel):
    """Where the appointment happens. Exactly one of `address`, `link`, or
    `phone_number` is meaningful per `kind`."""

    kind: LocationKind = LocationKind.IN_PERSON
    address: str = ""            # physical address for IN_PERSON
    link: str = ""               # join URL for VIDEO
    phone_number: str = ""       # number to call for PHONE
    notes: str = ""              # parking instructions, dial-in PIN, etc.


class TimeSlot(BaseModel):
    """A bounded interval. Used for availability querying and proposals.

    Always stored UTC; `timezone` is presentation metadata only. The
    availability layer guarantees `start_at < end_at` and that buffer time
    has already been accounted for upstream.
    """

    start_at: datetime
    end_at: datetime
    timezone: str = "UTC"        # display zone (IANA name)
    label: str = ""              # human-readable hint (e.g. "Tue 2pm")

    @property
    def duration_minutes(self) -> int:
        delta = self.end_at - self.start_at
        return int(delta.total_seconds() // 60)


class ServiceType(BaseModel):
    """A bookable service offered by the business.

    Mirrors a cal.com Event Type / Calendly Event Type. One business can
    offer many; each carries its own duration and buffer requirement.
    """

    slug: str                                 # short id used in config + URLs
    name: str                                 # human-friendly label
    duration_minutes: int = 30
    buffer_before_minutes: int = 0
    buffer_after_minutes: int = 0
    location: Optional[Location] = None       # default location for this service
    description: str = ""


class WorkingHours(BaseModel):
    """Per-weekday open hours. `weekday` follows Python's Monday=0 .. Sunday=6
    convention. Multiple windows per day are allowed (e.g. closed for lunch)."""

    weekday: int                              # 0=Mon, 6=Sun
    open_at: time
    close_at: time

    @classmethod
    def from_strings(
        cls, weekday: int, open_at: str, close_at: str
    ) -> "WorkingHours":
        return cls(
            weekday=weekday,
            open_at=time.fromisoformat(open_at),
            close_at=time.fromisoformat(close_at),
        )


class BusinessHours(BaseModel):
    """The full weekly availability template + the timezone they're in."""

    timezone: str = "America/New_York"        # IANA name
    windows: list[WorkingHours] = []

    def is_open(self, weekday: int, at: time) -> bool:
        """True if `at` falls inside any window for `weekday`."""
        for w in self.windows:
            if w.weekday == weekday and w.open_at <= at <= w.close_at:
                return True
        return False


class ReminderRecord(BaseModel):
    """One entry in an appointment's reminder history.

    Drafted reminders are NOT auto-sent — they go through the same
    require_approval interlock as the rest of the engine.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    channel: ReminderChannel = ReminderChannel.EMAIL
    offset_minutes_before: int = 60           # 60 = "1 hour before"
    scheduled_for: Optional[datetime] = None  # when this reminder should fire
    status: ReminderStatus = ReminderStatus.DRAFTED
    subject: str = ""
    body: str = ""
    drafted_at: datetime = Field(default_factory=now_utc)
    approved_at: Optional[datetime] = None
    approved_by: Optional[str] = None         # "cli" / "mcp" / operator email
    sent_at: Optional[datetime] = None
    provider_message_id: Optional[str] = None
    error: str = ""

    # Approval interlock — same shape as CustComm's Draft.approval_token.
    # Only an UPDATE that matches both `status='approved'` AND this token
    # may flip the row to `sent`. Prevents CLI/MCP races from double-sending.
    approval_token: str = Field(default_factory=lambda: str(uuid4()))


# ── Entities ──────────────────────────────────────────────────────────────────


class Appointment(BaseModel):
    """The core SchedBot entity — analogous to LeadGen's `Lead` and
    CustComm's `Thread`. Every layer (sources, AI, scheduler, CRM) speaks in
    these.

    Key invariants (enforced at the service / DB layer, not by Pydantic):
      - `requested_at` is set by every source.
      - `confirmed_at` is set only when status transitions to CONFIRMED.
      - `start_at` / `end_at` are aware-UTC; `timezone` is presentation only.
      - `external_id` is the upstream provider's id (cal.com booking id,
        Calendly UUID, etc.) — used for webhook idempotency.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    source: AppointmentSource = AppointmentSource.MANUAL
    external_id: Optional[str] = None         # provider-side id (for dedup)

    client: ClientInfo = ClientInfo()
    service_slug: str = ""                    # FK-by-convention into config.services
    service_name: str = ""                    # denormalized for display

    # Time window. start_at/end_at are the canonical UTC instants the
    # appointment occupies; duration_minutes is denormalized for fast queries
    # and for cases where the slot was requested without an end_at.
    requested_at: Optional[datetime] = None   # what the customer asked for
    start_at: Optional[datetime] = None       # confirmed start (UTC)
    end_at: Optional[datetime] = None         # confirmed end (UTC)
    duration_minutes: int = 30
    timezone: str = "UTC"                     # display tz (IANA name)

    location: Location = Location()
    status: AppointmentStatus = AppointmentStatus.REQUESTED

    # The customer-facing message draft generated when the appointment was
    # confirmed. Subject is empty for SMS-only confirmations.
    confirmation_subject: str = ""
    confirmation_body: str = ""
    confirmation_status: ReminderStatus = ReminderStatus.DRAFTED
    confirmation_token: str = Field(default_factory=lambda: str(uuid4()))
    confirmation_approved_at: Optional[datetime] = None
    confirmation_sent_at: Optional[datetime] = None

    reminders: list[ReminderRecord] = []
    notes: str = ""                           # operator notes / prep
    intake_notes: str = ""                    # what the customer told us
    tags: list[str] = []

    # No-show / follow-up bookkeeping
    no_show_at: Optional[datetime] = None
    no_show_followup_subject: str = ""
    no_show_followup_body: str = ""

    confirmed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    cancellation_reason: str = ""
    rescheduled_to_id: Optional[str] = None   # the successor appointment id

    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)

    raw_data: dict[str, Any] = {}             # full provider payload (webhook body, etc.)

    def touch(self) -> None:
        self.updated_at = now_utc()

    @property
    def is_open(self) -> bool:
        """True if the appointment is in a state that still expects action."""
        return self.status in {
            AppointmentStatus.REQUESTED,
            AppointmentStatus.CONFIRMED,
            AppointmentStatus.REMINDED,
        }


class WaitlistEntry(BaseModel):
    """A customer waiting for a slot to open up.

    The `desired_*` fields describe what the customer wants; the offer fields
    record the most recent slot we offered them, if any.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    client: ClientInfo = ClientInfo()
    service_slug: str = ""
    desired_window_start: Optional[datetime] = None
    desired_window_end: Optional[datetime] = None
    notes: str = ""

    status: WaitlistStatus = WaitlistStatus.WAITING

    offered_appointment_id: Optional[str] = None
    offered_at: Optional[datetime] = None
    offer_expires_at: Optional[datetime] = None
    offer_subject: str = ""
    offer_body: str = ""

    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


# ── Provider-neutral envelopes ────────────────────────────────────────────────


class ClassificationResult(BaseModel):
    """Output of `RequestClassifier.classify()` — what's the inbound asking for?"""

    kind: RequestKind = RequestKind.UNCERTAIN
    confidence: float = 0.0
    reasoning: str = ""
    referenced_appointment_id: Optional[str] = None  # set on RESCHEDULE / CANCEL
    classified_at: datetime = Field(default_factory=now_utc)


class RawBookingRequest(BaseModel):
    """Provider-neutral inbound envelope produced by every BookingSource.

    Kept deliberately flat so new connectors (a future Acuity integration,
    Twilio voice intake, etc.) can produce it without inventing their own
    intermediate shape — same convention as CustComm's RawInboundMessage.
    """

    provider: str                                    # "calcom" | "calendly" | "webhook" | ...
    provider_event_id: Optional[str] = None          # stable per-provider id
    event_kind: str = "booking.created"              # "booking.created" | "booking.cancelled" | ...

    client_name: str = ""
    client_email: str = ""
    client_phone: str = ""
    service_slug: str = ""
    service_name: str = ""

    requested_start_at: Optional[datetime] = None
    requested_end_at: Optional[datetime] = None
    duration_minutes: int = 30
    timezone: str = "UTC"

    location: Optional[Location] = None
    notes: str = ""
    raw_payload: dict[str, Any] = {}


# Re-export EmailStr so downstream consumers can import it without pulling
# pydantic directly (matches CustComm's pattern).
__all__ = [
    "AppointmentSource",
    "AppointmentStatus",
    "LocationKind",
    "ReminderChannel",
    "ReminderStatus",
    "WaitlistStatus",
    "RequestKind",
    "ClientInfo",
    "Location",
    "TimeSlot",
    "ServiceType",
    "WorkingHours",
    "BusinessHours",
    "ReminderRecord",
    "Appointment",
    "WaitlistEntry",
    "ClassificationResult",
    "RawBookingRequest",
    "EmailStr",
]
