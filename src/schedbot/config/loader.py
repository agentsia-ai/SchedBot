"""SchedBot Configuration Loader.

Loads and validates deployment config from YAML + environment variables.
Mirrors LeadGen / CustComm so operators and downstream personas can learn
one shape and apply it to all three engines.

Two-file split:
    config.yaml — business logic (services, hours, templates, AI prompts)
    .env        — secrets (API keys, OAuth credentials)

Secrets NEVER live in config.yaml. Every field on `APIKeys` reads from a
named environment variable.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()


# ── Pydantic models ───────────────────────────────────────────────────────────


class AIConfig(BaseModel):
    """Optional AI customization. Lets a deployment swap the default Claude
    model and/or override the engine's built-in system prompts by pointing at
    external text files — without subclassing.

    Subclassing the `RequestClassifier` / `MessageDrafter` /
    `AvailabilityScheduler` classes is the other supported customization
    path; see CLAUDE.md → Customization Patterns.
    """

    model: str = "claude-sonnet-4-20250514"
    classifier_prompt_path: Optional[str] = None
    drafter_prompt_path: Optional[str] = None
    scheduler_prompt_path: Optional[str] = None

    # Classifier confidence floor. Anything below this is collapsed to
    # RequestKind.UNCERTAIN and refuses to auto-act.
    min_request_confidence: float = 0.55

    # Hard caps on generated content. Overflow is truncated.
    max_message_chars: int = 1500


class ServiceConfig(BaseModel):
    """Bookable service. Mirrors a cal.com Event Type / Calendly Event Type
    so the same slug can be used in both worlds."""

    slug: str
    name: str
    duration_minutes: int = 30
    buffer_before_minutes: int = 0
    buffer_after_minutes: int = 0
    location_kind: str = "in_person"      # "in_person" | "video" | "phone"
    default_address: str = ""
    default_video_link: str = ""
    description: str = ""


class WorkingWindow(BaseModel):
    """Single open window for a single weekday. Multiple windows per day are
    allowed (e.g. closed for lunch).

    `weekday` uses the same convention as CustComm and agentsia-core configs:
    0 = Sunday, 1 = Monday, …, 6 = Saturday. AvailabilityEngine matches
    windows via schedbot._time.weekday_sunday0().
    """

    weekday: int                          # 0=Sunday .. 6=Saturday
    open_at: str = "09:00"                # HH:MM (24h), interpreted in business tz
    close_at: str = "17:00"


class BusinessConfig(BaseModel):
    """Identity + opening hours for the business."""

    name: str = "Your Business"
    business_type: str = ""               # free-form (e.g. "landscaping")
    timezone: str = "America/New_York"    # IANA name; appointments display in this tz
    working_hours: list[WorkingWindow] = []
    services: list[ServiceConfig] = []

    # Customer-facing tone descriptor injected into AI prompts at runtime.
    # Concrete > abstract: "warm, concise, never over-promises" beats "professional".
    communication_tone: str = "warm and professional"


class RemindersConfig(BaseModel):
    """How and when to remind clients about upcoming appointments."""

    # Each entry is "minutes before the appointment" — e.g. [1440, 60] = 24h + 1h.
    offsets_minutes_before: list[int] = [1440, 60]

    # Channels in priority order. First channel for which client has contact
    # info wins. ("email" requires client.email; "sms" requires client.phone.)
    channels: list[str] = ["email", "sms"]

    # Whether the engine drafts a follow-up after a no-show.
    no_show_followup_enabled: bool = True


class WaitlistConfig(BaseModel):
    """Waitlist behavior when a service is fully booked."""

    enabled: bool = True
    # When a slot opens, how long does the offered customer have to accept?
    offer_ttl_minutes: int = 120
    # Auto-draft an offer message when promoting a waitlist entry.
    auto_draft_offer: bool = True


class CalcomConfig(BaseModel):
    """cal.com REST + webhook integration. cal.com is the recommended
    primary source — simple API key auth, no OAuth dance."""

    enabled: bool = True
    base_url: str = "https://api.cal.com/v2"
    # The cal.com username (the part after `cal.com/` in your booking URL —
    # e.g. for `https://cal.com/agentsia/discovery-call` it's `agentsia`).
    username: str = ""
    # Default event-type slug to create bookings against if none specified.
    default_event_type_slug: str = ""


class GoogleCalendarConfig(BaseModel):
    """Google Calendar API integration (secondary). OAuth 2.0 — see
    docs/API_KEYS.md for the setup walkthrough."""

    enabled: bool = False
    calendar_id: str = "primary"          # "primary" or a specific calendar id
    # Whether to write back to the calendar when SchedBot creates a booking
    # (vs. read-only conflict checking).
    write_enabled: bool = False


class CalendlyConfig(BaseModel):
    """Calendly API integration (alternative for clients already on Calendly)."""

    enabled: bool = False
    organization_uri: str = ""            # https://api.calendly.com/organizations/...
    user_uri: str = ""                    # https://api.calendly.com/users/...


class WebhookConfig(BaseModel):
    """Inbound webhook receiver — for website contact forms / embedded
    booking widgets. The receiver is provider-neutral; a deployment binds
    it behind whatever HTTP layer it likes (FastAPI, Lambda, Cloudflare
    Worker, etc.)."""

    enabled: bool = False
    # Shared-secret HMAC verification (recommended). Empty disables verification
    # — only safe behind an authenticated edge.
    hmac_header: str = "X-SchedBot-Signature"
    expected_path: str = "/webhooks/booking"


class EmailParserConfig(BaseModel):
    """Parse booking requests from inbound emails. The parser itself is
    provider-neutral; what email source it polls is a deployment concern."""

    enabled: bool = False
    # Human-friendly hints to help the AI extract slot/service from prose.
    keywords_book: list[str] = ["book", "schedule", "appointment", "reserve"]
    keywords_reschedule: list[str] = ["reschedule", "move", "push", "change time"]
    keywords_cancel: list[str] = ["cancel", "can't make it", "wont be able"]


class SchedulerConfig(BaseModel):
    """Availability + atomic booking guardrails."""

    # Earliest a customer can book — defends against same-minute booking races.
    min_lead_minutes: int = 60

    # How many days into the future are bookable.
    booking_window_days: int = 60

    # Default slot stride when generating availability windows. Each
    # ServiceConfig can override its own duration / buffer; this is the floor.
    default_slot_stride_minutes: int = 15

    # Hard ceiling on how many candidate slots the AI scheduler can propose
    # per request — keeps prompts and customer replies short.
    max_proposed_slots: int = 3


class OutreachConfig(BaseModel):
    """Outbound messaging (email + SMS).

    SAFETY: every outbound channel obeys `require_approval`. `auto_send` is
    belt-and-braces — leaving it false guarantees a future misconfiguration
    can't silently autosend a confirmation, reminder, or follow-up.
    """

    require_approval: bool = True
    auto_send: bool = False

    daily_email_limit: int = 200
    daily_sms_limit: int = 100

    email_signature: str = (
        "Best,\n{agent_name}\n{business_name}"
    )
    sms_signature: str = "- {business_name}"

    # Cancellation policy text inserted into cancellation confirmations and
    # reschedule responses. Plain text, no markdown.
    cancellation_policy_text: str = (
        "Cancellations are appreciated at least 24 hours in advance so we "
        "can offer the slot to another client."
    )

    # Whether to attach a "want to rebook?" link/CTA to cancellation messages.
    rebooking_offer_enabled: bool = True
    rebooking_offer_text: str = (
        "If you'd like to rebook for another time, just reply and we'll find "
        "you a new slot."
    )


class DatabaseConfig(BaseModel):
    backend: str = "sqlite"
    sqlite_path: str = "./data/schedbot.db"


class SchedBotConfig(BaseModel):
    """Top-level config object. Everything is grouped under typed sub-models
    so a YAML can be structured + validated and a downstream persona can
    build a TypedDict-like view of just the bits it cares about."""

    # Identity (operator + agent). Mirrors CustComm's identity block.
    client_name: str = ""
    operator_name: str = "Operator"
    operator_title: str = ""
    operator_email: str = "ops@example.com"
    agent_name: str = ""
    agent_email: str = ""

    business: BusinessConfig = BusinessConfig()
    ai: AIConfig = AIConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    reminders: RemindersConfig = RemindersConfig()
    waitlist: WaitlistConfig = WaitlistConfig()
    outreach: OutreachConfig = OutreachConfig()
    database: DatabaseConfig = DatabaseConfig()

    calcom: CalcomConfig = CalcomConfig()
    google_calendar: GoogleCalendarConfig = GoogleCalendarConfig()
    calendly: CalendlyConfig = CalendlyConfig()
    webhook: WebhookConfig = WebhookConfig()
    email_parser: EmailParserConfig = EmailParserConfig()


# ── API Keys (from environment only — never in config files) ─────────────────


class APIKeys(BaseModel):
    """All secrets the engine needs. Loaded exclusively from env / .env.

    Adding a secret means adding (a) a field here with `alias=ENV_NAME` and
    (b) a row to `.env.example`. Never put a secret default into config.yaml.
    """

    anthropic: str = Field(default="", alias="ANTHROPIC_API_KEY")

    # cal.com — primary scheduling integration
    calcom_api_key: str = Field(default="", alias="CALCOM_API_KEY")
    calcom_webhook_secret: str = Field(default="", alias="CALCOM_WEBHOOK_SECRET")

    # Google Calendar (OAuth) — secondary
    google_credentials_path: str = Field(
        default="", alias="GOOGLE_CALENDAR_CREDENTIALS_PATH"
    )
    google_token_path: str = Field(
        default="./.google_calendar_token.json", alias="GOOGLE_CALENDAR_TOKEN_PATH"
    )
    google_oauth_client_id: str = Field(default="", alias="GOOGLE_OAUTH_CLIENT_ID")
    google_oauth_client_secret: str = Field(
        default="", alias="GOOGLE_OAUTH_CLIENT_SECRET"
    )

    # Calendly (alternative)
    calendly_api_key: str = Field(default="", alias="CALENDLY_API_KEY")

    # Acuity (alternative)
    acuity_user_id: str = Field(default="", alias="ACUITY_USER_ID")
    acuity_api_key: str = Field(default="", alias="ACUITY_API_KEY")

    # SMTP / Gmail (confirmations + reminders)
    smtp_host: str = Field(default="smtp.gmail.com", alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_username: str = Field(default="", alias="SMTP_USERNAME")
    smtp_password: str = Field(default="", alias="SMTP_PASSWORD")
    smtp_from_email: str = Field(default="", alias="SMTP_FROM_EMAIL")
    # FUTURE: when outbound SMTP sends human-escalation mail, prefer
    # config.operator_name / operator_email over this env override.
    smtp_from_name: str = Field(default="", alias="SMTP_FROM_NAME")

    # Twilio (SMS)
    twilio_account_sid: str = Field(default="", alias="TWILIO_ACCOUNT_SID")
    twilio_auth_token: str = Field(default="", alias="TWILIO_AUTH_TOKEN")
    twilio_from_number: str = Field(default="", alias="TWILIO_FROM_NUMBER")

    # Inbound webhook signing secret (when the deployment exposes the
    # webhook receiver behind a public HTTP layer).
    webhook_signing_secret: str = Field(default="", alias="WEBHOOK_SIGNING_SECRET")

    @classmethod
    def from_env(cls) -> "APIKeys":
        values: dict[str, Any] = {}
        for field in cls.model_fields.values():
            alias = field.alias
            if not alias:
                continue
            raw = os.getenv(alias)
            if raw is None or raw == "":
                continue
            values[alias] = raw
        return cls(**values)


# ── Loader ────────────────────────────────────────────────────────────────────


def display_agent_name(config: SchedBotConfig) -> str:
    """Agent-facing label for logs and MCP metadata.

    Productized deployments (e.g. agentsia-core) set config.agent_name.
    Standalone SchedBot installs fall back to the engine name.
    """
    name = (config.agent_name or "").strip()
    return name or "schedbot"


def load_config(config_path: str | Path | None = None) -> SchedBotConfig:
    """Load and validate deployment config from a YAML file.

    Resolution order:
      1. explicit `config_path` argument
      2. CONFIG_PATH env var
      3. `./config.yaml`
    """
    path = Path(config_path or os.getenv("CONFIG_PATH", "config.yaml"))

    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            f"Copy config.example.yaml to {path} and fill in your details."
        )

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    return SchedBotConfig(**raw)


def load_api_keys() -> APIKeys:
    """Load API keys from environment variables (and any .env file)."""
    return APIKeys.from_env()
