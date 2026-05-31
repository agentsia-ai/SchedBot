"""SchedBot MCP Server.

Exposes SchedBot as an MCP tool server so Claude Desktop (or any MCP
client) can run scheduling operations conversationally.

Usage:
    schedbot mcp
    # or: python -m schedbot.mcp

See docs/MCP_SETUP.md for the Claude Desktop configuration block.

This server mirrors CustComm's MCP layout exactly:
  - All tool handlers read module-level globals (`config`, `keys`, `db`).
  - Globals are populated inside `main()` so an outer agent runtime can
    chdir to a client-specific directory before the engine reads
    `config.yaml`.
  - Pluggable AI classes (`REQUEST_CLASSIFIER_CLASS`, `MESSAGE_DRAFTER_CLASS`,
    `AVAILABILITY_SCHEDULER_CLASS`) default to the engine base classes; a
    persona runtime overrides them via `*_cls=` kwargs to `main()`.
  - Stdout is reserved for JSON-RPC frames — every log goes to stderr.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from schedbot.ai.classifier import RequestClassifier
from schedbot.ai.drafter import MessageDrafter
from schedbot.ai.scheduler import AvailabilityScheduler
from schedbot.config.loader import display_agent_name, load_api_keys, load_config
from schedbot.crm.database import AppointmentDatabase
from schedbot.models import (
    Appointment,
    AppointmentSource,
    AppointmentStatus,
    ClientInfo,
    Location,
    LocationKind,
    RawBookingRequest,
    WaitlistStatus,
)
from schedbot.scheduler.waitlist import WaitlistManager
from schedbot.service import (
    cancel_appointment,
    confirm_appointment,
    daily_digest,
    draft_pending_reminders,
    find_available_slots,
    ingest_booking,
    mark_no_show,
    reschedule_appointment,
)

logger = logging.getLogger(__name__)


# ── Server init ───────────────────────────────────────────────────────────────

app = Server("schedbot")

# Config / keys / db are initialized in main() rather than at import time so
# the cwd has had a chance to be set by the caller. When a productized agent
# runtime (agentsia-core's AgentContext.activate) chdir's into agents/<agent>/
# before invoking main(), loading config here ensures the engine sees the
# client-specific config.yaml.
config = None  # type: ignore[assignment]
keys = None    # type: ignore[assignment]
db = None      # type: ignore[assignment]


# ── Pluggable class seam ──────────────────────────────────────────────────────
# Defaults = generic engine classes. A productized persona (e.g. Nova) injects
# subclasses by passing *_cls= kwargs to main(); main() overwrites these
# globals BEFORE the server starts handling tool calls. Tool handlers read the
# globals at request time, so injection takes effect for every subsequent call.
REQUEST_CLASSIFIER_CLASS: type[RequestClassifier] = RequestClassifier
MESSAGE_DRAFTER_CLASS: type[MessageDrafter] = MessageDrafter
AVAILABILITY_SCHEDULER_CLASS: type[AvailabilityScheduler] = AvailabilityScheduler


# ── Tool definitions ──────────────────────────────────────────────────────────


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_schedule_summary",
            description=(
                "Today and tomorrow's appointments, grouped by day, with "
                "status. The morning-digest view."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="check_availability",
            description=(
                "Find open slots for a service over the next N days. Returns "
                "candidate slots that respect business hours, buffer time, "
                "and existing confirmed appointments."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "service_slug": {"type": "string"},
                    "days": {"type": "integer", "description": "Lookahead window (default: config booking_window_days)"},
                    "max_slots": {"type": "integer", "description": "Cap on returned slots (default 30)"},
                },
                "required": ["service_slug"],
            },
        ),
        Tool(
            name="book_appointment",
            description=(
                "Create a new appointment from explicit fields (manual / "
                "phone-call source). Status starts as REQUESTED — call "
                "`confirm_appointment` separately to lock the slot."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "client_name": {"type": "string"},
                    "client_email": {"type": "string"},
                    "client_phone": {"type": "string"},
                    "service_slug": {"type": "string"},
                    "start_at": {"type": "string", "description": "ISO 8601 datetime"},
                    "duration_minutes": {"type": "integer"},
                    "timezone": {"type": "string"},
                    "notes": {"type": "string"},
                    "location_kind": {"type": "string", "description": "in_person|video|phone"},
                    "location_value": {"type": "string", "description": "Address, link, or phone"},
                },
                "required": ["client_email", "service_slug", "start_at"],
            },
        ),
        Tool(
            name="confirm_appointment",
            description=(
                "Atomically reserve a REQUESTED appointment's slot and draft "
                "its confirmation message. Will refuse if a conflict exists. "
                "The drafted confirmation is PENDING — operator must approve "
                "before anything is sent."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "string"},
                    "channel": {"type": "string", "description": "email|sms (default email)"},
                    "guidance": {"type": "string"},
                },
                "required": ["appointment_id"],
            },
        ),
        Tool(
            name="reschedule_appointment",
            description=(
                "Find alternative slots for an appointment and draft a "
                "reschedule-options reply. Does NOT move the appointment — "
                "use `confirm_appointment` once the customer picks a slot."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "string"},
                    "customer_request_text": {
                        "type": "string",
                        "description": "What the customer said in their reschedule message.",
                    },
                    "channel": {"type": "string"},
                    "guidance": {"type": "string"},
                },
                "required": ["appointment_id"],
            },
        ),
        Tool(
            name="cancel_appointment",
            description=(
                "Mark an appointment CANCELLED, draft the acknowledgement, "
                "and (if waitlist enabled) draft an offer to the longest-"
                "waiting compatible waitlist entry."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "channel": {"type": "string"},
                    "guidance": {"type": "string"},
                },
                "required": ["appointment_id"],
            },
        ),
        Tool(
            name="send_reminder",
            description=(
                "Draft (or re-draft) a reminder for a specific appointment. "
                "The reminder sits in DRAFTED status until approved — this "
                "tool never sends."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "string"},
                    "offset_minutes_before": {"type": "integer"},
                    "channel": {"type": "string"},
                    "guidance": {"type": "string"},
                },
                "required": ["appointment_id", "offset_minutes_before"],
            },
        ),
        Tool(
            name="get_appointment_detail",
            description="Full detail on a specific appointment (incl. reminders).",
            inputSchema={
                "type": "object",
                "properties": {"appointment_id": {"type": "string"}},
                "required": ["appointment_id"],
            },
        ),
        Tool(
            name="add_to_waitlist",
            description=(
                "Add a client to the waitlist for a service. They'll be "
                "promoted (with a drafted offer) when a matching slot opens."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "client_name": {"type": "string"},
                    "client_email": {"type": "string"},
                    "client_phone": {"type": "string"},
                    "service_slug": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["client_email", "service_slug"],
            },
        ),
        Tool(
            name="get_waitlist",
            description="Current waitlist entries, optionally filtered by service.",
            inputSchema={
                "type": "object",
                "properties": {
                    "service_slug": {"type": "string"},
                    "status": {"type": "string", "description": "waiting|offered|booked|declined|expired"},
                    "limit": {"type": "integer"},
                },
            },
        ),
        Tool(
            name="mark_no_show",
            description=(
                "Flag an appointment as NO_SHOW and (if enabled) draft a "
                "follow-up message."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "string"},
                    "channel": {"type": "string"},
                    "guidance": {"type": "string"},
                },
                "required": ["appointment_id"],
            },
        ),
    ]


# ── Tool handlers ─────────────────────────────────────────────────────────────


def _json(obj: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(obj, indent=2, default=str))]


def _appt_view(a: Appointment) -> dict[str, Any]:
    return {
        "id": a.id,
        "status": a.status.value,
        "service": a.service_name or a.service_slug,
        "client": {
            "name": a.client.name,
            "email": a.client.email,
            "phone": a.client.phone,
        },
        "start_at": a.start_at,
        "end_at": a.end_at,
        "duration_minutes": a.duration_minutes,
        "timezone": a.timezone,
        "location": a.location.model_dump(mode="json"),
        "notes": a.notes,
        "intake_notes": a.intake_notes,
    }


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    await db.init()

    if name == "get_schedule_summary":
        digest = await daily_digest(config, db)
        return _json(
            {
                "agent": display_agent_name(config),
                "today": [_appt_view(a) for a in digest["today"]],
                "tomorrow": [_appt_view(a) for a in digest["tomorrow"]],
                "today_count": len(digest["today"]),
                "tomorrow_count": len(digest["tomorrow"]),
            }
        )

    elif name == "check_availability":
        slots = await find_available_slots(
            config, db,
            service_slug=arguments["service_slug"],
            days=arguments.get("days"),
            max_slots=arguments.get("max_slots", 30),
        )
        return _json(
            [
                {
                    "start_at": s.start_at,
                    "end_at": s.end_at,
                    "timezone": s.timezone,
                    "duration_minutes": s.duration_minutes,
                }
                for s in slots
            ]
        )

    elif name == "book_appointment":
        location = _decode_location(
            arguments.get("location_kind"), arguments.get("location_value")
        )
        raw = RawBookingRequest(
            provider=AppointmentSource.MANUAL.value,
            provider_event_id="",
            event_kind="booking.created",
            client_name=arguments.get("client_name", ""),
            client_email=arguments["client_email"],
            client_phone=arguments.get("client_phone", ""),
            service_slug=arguments["service_slug"],
            requested_start_at=datetime.fromisoformat(arguments["start_at"]),
            duration_minutes=int(arguments.get("duration_minutes", 30)),
            timezone=arguments.get("timezone", config.business.timezone),
            location=location,
            notes=arguments.get("notes", ""),
        )
        # Pre-compute end_at from duration if not on the raw envelope.
        if raw.requested_start_at and raw.requested_end_at is None:
            from datetime import timedelta as _td

            raw.requested_end_at = raw.requested_start_at + _td(minutes=raw.duration_minutes)
        appt, is_new = await ingest_booking(config, db, raw)
        return _json({"created": is_new, "appointment": _appt_view(appt)})

    elif name == "confirm_appointment":
        appt = await confirm_appointment(
            config, keys, db,
            appointment_id=arguments["appointment_id"],
            drafter_cls=MESSAGE_DRAFTER_CLASS,
            channel=arguments.get("channel", "email"),
            guidance=arguments.get("guidance", ""),
        )
        if appt is None:
            return _json({"error": "Could not confirm appointment (not found or wrong status)."})
        return _json(
            {
                "confirmed": appt.status.value == "confirmed",
                "appointment": _appt_view(appt),
                "confirmation_subject": appt.confirmation_subject,
                "confirmation_body": appt.confirmation_body,
            }
        )

    elif name == "reschedule_appointment":
        appt, slots = await reschedule_appointment(
            config, keys, db,
            appointment_id=arguments["appointment_id"],
            drafter_cls=MESSAGE_DRAFTER_CLASS,
            scheduler_cls=AVAILABILITY_SCHEDULER_CLASS,
            customer_request_text=arguments.get("customer_request_text", ""),
            channel=arguments.get("channel", "email"),
            guidance=arguments.get("guidance", ""),
        )
        if appt is None:
            return _json({"error": "Appointment not found."})
        return _json(
            {
                "appointment_id": appt.id,
                "alternatives": [
                    {"start_at": s.start_at, "end_at": s.end_at, "timezone": s.timezone}
                    for s in slots
                ],
                "draft_subject": appt.confirmation_subject,
                "draft_body": appt.confirmation_body,
            }
        )

    elif name == "cancel_appointment":
        appt = await cancel_appointment(
            config, keys, db,
            appointment_id=arguments["appointment_id"],
            reason=arguments.get("reason", ""),
            drafter_cls=MESSAGE_DRAFTER_CLASS,
            channel=arguments.get("channel", "email"),
            guidance=arguments.get("guidance", ""),
        )
        if appt is None:
            return _json({"error": "Appointment not found."})
        return _json(
            {
                "cancelled": True,
                "appointment": _appt_view(appt),
                "cancellation_subject": appt.confirmation_subject,
                "cancellation_body": appt.confirmation_body,
            }
        )

    elif name == "send_reminder":
        appt = await db.get_appointment(arguments["appointment_id"])
        if appt is None:
            return _json({"error": "Appointment not found."})
        from schedbot.models import ReminderChannel, ReminderRecord, ReminderStatus
        from datetime import timedelta as _td

        offset = int(arguments["offset_minutes_before"])
        channel_str = (arguments.get("channel") or "email").lower()
        channel = ReminderChannel.SMS if channel_str == "sms" else ReminderChannel.EMAIL

        scheduled_for = (
            appt.start_at - _td(minutes=offset) if appt.start_at else None
        )
        drafter = MESSAGE_DRAFTER_CLASS(config, keys)
        subject, body = await drafter.draft_reminder(
            appt, offset_minutes_before=offset,
            channel=channel.value,
            guidance=arguments.get("guidance", ""),
        )
        record = ReminderRecord(
            channel=channel,
            offset_minutes_before=offset,
            scheduled_for=scheduled_for,
            status=ReminderStatus.DRAFTED,
            subject=subject,
            body=body,
        )
        await db.insert_reminder(appt.id, record)
        return _json(
            {
                "drafted": True,
                "reminder_id": record.id,
                "subject": subject,
                "body": body,
                "scheduled_for": scheduled_for,
            }
        )

    elif name == "get_appointment_detail":
        appt = await db.get_appointment(arguments["appointment_id"])
        if appt is None:
            return _json({"error": "Appointment not found."})
        reminders = await db.list_reminders_for_appointment(appt.id)
        return _json(
            {
                "appointment": appt.model_dump(mode="json"),
                "reminders": [r.model_dump(mode="json") for r in reminders],
            }
        )

    elif name == "add_to_waitlist":
        manager = WaitlistManager(config, db)
        client = ClientInfo(
            name=arguments.get("client_name", ""),
            email=arguments["client_email"],
            phone=arguments.get("client_phone", ""),
        )
        entry = await manager.add(
            client=client,
            service_slug=arguments["service_slug"],
            notes=arguments.get("notes", ""),
        )
        if entry is None:
            return _json({"error": "Waitlist disabled in config."})
        return _json({"added": True, "entry": entry.model_dump(mode="json")})

    elif name == "get_waitlist":
        status = (
            WaitlistStatus(arguments["status"])
            if arguments.get("status")
            else None
        )
        entries = await db.list_waitlist(
            status=status,
            service_slug=arguments.get("service_slug"),
            limit=arguments.get("limit", 50),
        )
        return _json([e.model_dump(mode="json") for e in entries])

    elif name == "mark_no_show":
        appt = await mark_no_show(
            config, keys, db,
            appointment_id=arguments["appointment_id"],
            drafter_cls=MESSAGE_DRAFTER_CLASS,
            channel=arguments.get("channel", "email"),
            guidance=arguments.get("guidance", ""),
        )
        if appt is None:
            return _json({"error": "Appointment not found."})
        return _json(
            {
                "marked": True,
                "appointment": _appt_view(appt),
                "followup_subject": appt.no_show_followup_subject,
                "followup_body": appt.no_show_followup_body,
            }
        )

    return _json({"error": f"Unknown tool: {name}"})


def _decode_location(kind: str | None, value: str | None) -> Location:
    kind = (kind or "in_person").lower()
    value = value or ""
    if kind == "video":
        return Location(kind=LocationKind.VIDEO, link=value)
    if kind == "phone":
        return Location(kind=LocationKind.PHONE, phone_number=value)
    return Location(kind=LocationKind.IN_PERSON, address=value)


# ── Entry point ───────────────────────────────────────────────────────────────


async def main(
    request_classifier_cls: type[RequestClassifier] | None = None,
    message_drafter_cls: type[MessageDrafter] | None = None,
    availability_scheduler_cls: type[AvailabilityScheduler] | None = None,
) -> None:
    """Start the MCP server.

    Optionally inject persona-specific subclasses (e.g. NovaRequestClassifier,
    NovaMessageDrafter, NovaAvailabilityScheduler). Defaults use the generic
    engine classes.
    """
    global REQUEST_CLASSIFIER_CLASS, MESSAGE_DRAFTER_CLASS, AVAILABILITY_SCHEDULER_CLASS
    global config, keys, db

    if request_classifier_cls is not None:
        REQUEST_CLASSIFIER_CLASS = request_classifier_cls
        logger.info(
            f"MCP request classifier overridden: "
            f"{request_classifier_cls.__module__}.{request_classifier_cls.__name__}"
        )
    if message_drafter_cls is not None:
        MESSAGE_DRAFTER_CLASS = message_drafter_cls
        logger.info(
            f"MCP message drafter overridden: "
            f"{message_drafter_cls.__module__}.{message_drafter_cls.__name__}"
        )
    if availability_scheduler_cls is not None:
        AVAILABILITY_SCHEDULER_CLASS = availability_scheduler_cls
        logger.info(
            f"MCP availability scheduler overridden: "
            f"{availability_scheduler_cls.__module__}.{availability_scheduler_cls.__name__}"
        )

    # Load config / keys / db now that cwd is final. Doing this here (instead
    # of at module import) lets an outer agent runtime chdir to a client-
    # specific directory before the engine reads config.yaml — the same
    # pattern CustComm and LeadGen use.
    config = load_config()
    keys = load_api_keys()
    db = AppointmentDatabase(config.database.sqlite_path)

    # Route ALL logs to stderr so stdout stays sacred for JSON-RPC frames.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    agent_label = display_agent_name(config)
    logger.info("Starting SchedBot MCP server (agent=%s)...", agent_label)
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


# Suppress flake8 unused-import warnings for the typing-only imports.
_ = (RequestClassifier, draft_pending_reminders)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
