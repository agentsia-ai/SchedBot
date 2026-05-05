# CLAUDE.md — SchedBot

This file provides context and instructions for Claude (or any AI assistant)
working in this codebase. Read this before making any changes.

---

## What This Project Is

SchedBot is a generic, AGPL-licensed, AI-powered scheduling and
appointment-management engine. It's designed to be the open-source core
that any operator can configure and deploy. Its job:

1. Ingest booking requests from cal.com (REST + webhooks), Google Calendar,
   Calendly, generic webhooks (website forms), or parsed inbound emails.
2. Classify each request against a small taxonomy: new booking,
   reschedule, cancel, question, uncertain.
3. Reason about availability — generate candidate slots that respect
   business hours and per-service buffer time, then have Claude pick
   the best 1–N to actually offer.
4. Draft customer-facing messages (confirmations, reminders, reschedule
   options, cancellation acknowledgements, no-show follow-ups, waitlist
   offers) with consistent tone and explicit approval interlock.
5. Manage a waitlist — add clients when fully booked, auto-promote when
   slots open.
6. Expose all functionality as an MCP server for conversational control
   via Claude Desktop — *never* autonomously sending.
7. Support productized deployments via subclassing or config-based prompt
   overrides (see *Customization Patterns* below).

This repository is intentionally **identity-free**. Anything specific to a
particular operator, brand, named agent, or voice belongs in a downstream
private repository or a local `config.yaml` — never in this codebase.

---

## Architecture Overview

```
src/schedbot/
├── _time.py                  # UTC-aware datetime helpers (now_utc, to_iso, parse_iso, format_local)
├── models.py                 # Appointment, TimeSlot, BusinessHours, ReminderRecord, WaitlistEntry, RawBookingRequest
├── service.py                # High-level coordination (book / confirm / reschedule / digest)
├── config/
│   └── loader.py             # Pydantic SchedBotConfig + APIKeys
├── sources/                  # Inbound booking-request connectors
│   ├── base.py               # BookingSource + WebhookReceiver ABCs
│   ├── calcom.py             # cal.com REST API — recommended primary
│   ├── calcom_webhooks.py    # cal.com webhook receiver (HMAC-verified)
│   ├── google_calendar.py    # Google Calendar API — secondary, OAuth2
│   ├── calendly.py           # Calendly API — alternative
│   ├── webhook.py            # Generic webhook receiver (website forms)
│   └── email_parser.py       # Parse booking requests from inbound emails
├── ai/
│   ├── classifier.py         # RequestClassifier (pluggable)
│   ├── drafter.py            # MessageDrafter (pluggable)
│   └── scheduler.py          # AvailabilityScheduler (pluggable, AI-side)
├── scheduler/                # Deterministic scheduling logic (no AI)
│   ├── availability.py       # AvailabilityEngine — generates candidate slots
│   ├── reminders.py          # ReminderScheduler — enqueues reminder records
│   └── waitlist.py           # WaitlistManager — add / promote / expire
├── crm/
│   └── database.py           # AppointmentDatabase — async SQLite store
├── mcp_server/
│   └── server.py             # MCP server exposing all tools to Claude Desktop
└── cli.py                    # Click CLI entry point — `schedbot ...`
```

### Data Flow

```
Source (cal.com / webhook / email) → RawBookingRequest
     → service.ingest_booking → Appointment (REQUESTED)
     → RequestClassifier (only on email/uncertain inputs)
     → service.confirm_appointment → atomic slot reserve + drafted confirmation + queued reminders
     → Operator approve (CLI or MCP) → confirmation_status=APPROVED
     → SMTP / Twilio sender → confirmation_sent_at set, reminders go through same loop
     → Event happens → COMPLETED  (or → NO_SHOW → drafted follow-up)
     → Customer reschedules → service.reschedule_appointment → drafted alternatives
     → Customer cancels → service.cancel_appointment → cancelled + waitlist promotion
```

Every transition is explicit. The engine will never collapse multiple steps
(e.g. "confirm and send in one call") — that's the core guardrail.

---

## Key Design Principles

### 1. Never auto-send
`outreach.require_approval = true` and `outreach.auto_send = false` are the
defaults and the only values the engine ships with verified. The drafter
output for confirmations / reminders / cancellations / follow-ups is
written to the row in `DRAFTED` status; nothing leaves the box without an
explicit `APPROVED` flip and a sender pass. The MCP tool surface has no
"draft and send" combo.

### 2. Never double-book
Slot reservation is atomic. `AppointmentDatabase.reserve_slot` is a single
guarded UPDATE whose WHERE includes both a status check (`status='requested'`)
and a `NOT EXISTS` anti-join against any CONFIRMED/REMINDED appointment in
the same window. Buffer time is baked into the window upstream by
`AvailabilityEngine`. Concurrent CLI + MCP confirms on the same slot can
never both succeed.

### 3. Config-driven, not code-driven
Everything client-specific lives in `config.yaml` and `.env`. The engine
code should never contain hardcoded identity, brand, or voice. When adding
features, ask: "should this be configurable?" If yes, add it to the config
schema in `src/schedbot/config/loader.py` first.

### 4. The data models are the contract
`src/schedbot/models.py` defines `Appointment`, `TimeSlot`, `ServiceType`,
`BusinessHours`, `ReminderRecord`, `WaitlistEntry`, and the
`RawBookingRequest` envelope. Every layer (sources, AI, scheduler, CRM,
outreach) speaks in these objects. Never pass raw dicts between layers.

### 5. Async everywhere
All I/O is async (`httpx`, `aiosqlite`, `anthropic` async client). Use
`async/await` consistently. Don't introduce synchronous blocking calls in
the hot path.

### 6. Timezone-aware everywhere
All datetimes are stored in UTC and presented in the configured business
timezone. The rule:
- Anything written to the DB goes through `to_iso(now_utc())` shape.
- Anything read from the DB comes back as a `tz-aware UTC` datetime via `parse_iso`.
- Anything shown to a human goes through `format_local(dt, business.timezone)`.

There is no path through which a naive datetime should enter the database.
Don't add one.

### 7. MCP tools must be self-describing
The MCP server is the primary operator interface. Tool names and
descriptions must be clear enough that Claude can reason about when to use
them without additional context. Keep tool schemas tight — prefer fewer,
well-named parameters over many optional ones.

### 8. White-label ready / identity-free
The engine code must not contain any operator- or client-specific
identity, brand name, named agent persona, or voice. All identity flows in
through `config.yaml` at runtime, or through a downstream subclass that
overrides the base classes (see *Customization Patterns* below). The only
exception is the LICENSE copyright line, which is required by AGPL-3.0.

If you find yourself wanting to bake "Nova says..." or "Greenleaf
Landscaping handles it this way..." into a prompt or default value here,
**stop** — that belongs in a downstream private repo, not in this engine.

### 9. Engines stay public, personas live in downstream private repos
SchedBot is published and forked as a standalone engine. Named personas
(e.g. Agentsia's Nova) live in a separate private repository and consume
SchedBot as an installed dependency, subclassing the base classes for
voice. Do not accept PRs that add persona-specific content to this repo.

---

## Customization Patterns

There are two supported ways to customize prompt behavior without
modifying this engine:

### Pattern A — Config-based prompt override (no code)

Point at external prompt files in your `config.yaml`:

```yaml
ai:
  model: "claude-sonnet-4-20250514"
  classifier_prompt_path: "./prompts/classifier.txt"
  drafter_prompt_path: "./prompts/drafter.txt"
  scheduler_prompt_path: "./prompts/scheduler.txt"
```

The base `RequestClassifier` / `MessageDrafter` / `AvailabilityScheduler`
will read these files at construction time and use them as the system
prompt. Missing files log a warning and fall back to the class-constant
default.

### Pattern B — Subclassing (for productized agents)

For named agents with personas (e.g. a downstream private repo defining
"Nova"), subclass and override the class constants:

```python
from schedbot.ai.classifier import RequestClassifier
from schedbot.ai.drafter import MessageDrafter
from schedbot.ai.scheduler import AvailabilityScheduler

class NovaRequestClassifier(RequestClassifier):
    SYSTEM_PROMPT = "You are Nova's request triage brain..."

class NovaMessageDrafter(MessageDrafter):
    SYSTEM_PROMPT = "You are Nova's voice — warm, plain-spoken..."

class NovaAvailabilityScheduler(AvailabilityScheduler):
    SYSTEM_PROMPT = "You are Nova's slot-picking specialist..."
```

The MCP server accepts `request_classifier_cls`, `message_drafter_cls`,
and `availability_scheduler_cls` kwargs on `main()` so an agent runtime
can inject these subclasses at startup. See `mcp_server/server.py`.

Both patterns can be combined (subclass for code-level customization,
then override per-deployment via config). This three-tier model
(engine → named agent → per-client config) is the canonical
productization shape.

---

## Working With This Codebase

### Running locally
```bash
# Install dependencies (uv-managed)
uv sync --extra dev

# Set up config
cp .env.example .env                       # fill in API keys
cp config.example.yaml config.yaml         # customize identity + services

# Initialize database (safe to run anytime)
uv run schedbot pipeline

# See today + tomorrow's bookings
uv run schedbot digest

# Confirm a requested appointment (atomic; drafts confirmation + queues reminders)
uv run schedbot confirm <appointment-id>

# Draft pending reminders that don't yet have bodies
uv run schedbot remind

# Review drafted confirmations + reminders awaiting approval
uv run schedbot review

# Start MCP server
uv run schedbot mcp
```

### Adding a new booking source
1. Create `src/schedbot/sources/<source>.py`
2. Subclass `BookingSource` (poll-style) or `WebhookReceiver` (push-style)
   and implement `fetch_new()` / `handle()`. Both must produce
   `RawBookingRequest` envelopes.
3. Add config fields to a new sub-model in `src/schedbot/config/loader.py`
   and attach it to `SchedBotConfig`.
4. Add any required env vars to `.env.example` and `APIKeys`.
5. Document the setup in `docs/API_KEYS.md`.

### Adding a new MCP tool
1. Add the `Tool` definition in `list_tools()` in `src/schedbot/mcp_server/server.py`
2. Add the handler branch in `call_tool()`
3. Update `docs/MCP_SETUP.md` with the new tool in the tools table
4. Keep tool names snake_case and descriptions action-oriented

### Modifying the data models
- Add new fields with sensible defaults so existing DB rows don't break
- If a field needs fast filtering, denormalize it into a column in `crm/database.py`
- Update the row-to-model helper (`_row_to_appt`, `_row_to_reminder`,
  `_row_to_waitlist`) in `crm/database.py` after adding persisted fields.

---

## Claude API Usage in This Project

SchedBot uses Claude for three things:

### 1. Request Classification (`src/schedbot/ai/classifier.py`)
- Default model: `claude-sonnet-4-20250514` (override via `ai.model` in config)
- Returns structured JSON
  `{"kind": "new_booking|reschedule|cancel|question|uncertain", "confidence": 0.0, "reasoning": "..."}`
- Default prompt lives on `RequestClassifier.SYSTEM_PROMPT`
- Confidence below `ai.min_request_confidence` is collapsed to
  `RequestKind.UNCERTAIN`

### 2. Message Drafting (`src/schedbot/ai/drafter.py`)
- Default model: `claude-sonnet-4-20250514`
- Six methods, one per outbound message type (confirmation, reminder,
  cancellation, no-show follow-up, reschedule options, waitlist offer);
  each returns `(subject, body)`.
- Default prompt lives on `MessageDrafter.SYSTEM_PROMPT`
- Hard-capped at `ai.max_message_chars` (default 1500)

### 3. Availability Reasoning (`src/schedbot/ai/scheduler.py`)
- Default model: `claude-sonnet-4-20250514`
- One method: `pick_slots(candidate_slots, customer_request_text, ...)` —
  returns the chosen `TimeSlot[]` plus a one-line reasoning string.
- Default prompt lives on `AvailabilityScheduler.SYSTEM_PROMPT`
- Capped at `scheduler.max_proposed_slots` (default 3)

### Prompt tuning tips
- Classifier quality improves when the prompt enumerates concrete
  edge cases (cancellation phrased as a question, rescheduling without an
  explicit "reschedule" word, etc.).
- Drafter quality improves when the operator's voice is described
  concretely ("warm, plain-spoken, never over-promises" beats
  "professional").
- Scheduler quality improves when you tell it the operator's preferences
  ("prefer mornings", "avoid same-day", "cluster appointments by
  geography") in the prompt rather than just letting it guess.
- Keep `max_tokens` tight — 400 for classify, 800 for draft, 500 for
  schedule.

---

## MCP Server Ground Rules

The MCP server uses stdio transport. The stdio stream is the transport
for JSON-RPC frames — anything we write to it that isn't a JSON-RPC frame
shows up as "Unexpected token" errors in the Claude Desktop client.

Therefore, in any code path the MCP server can reach:

- **No `print()`** — ever. Use `logging` (stderr by default).
- **No `Console()` without `stderr=True`** — Rich's default console writes
  to stdout. Always construct `Console(stderr=True)` for MCP-facing output.
- **No config / credentials / DB loading at module import time** —
  initialize them inside `main()` after the caller has had a chance to
  `chdir` and set env vars. Use module-level `None` placeholders declared
  `global` inside `main()`.
- **Module-level pluggable class globals** —
  `REQUEST_CLASSIFIER_CLASS`, `MESSAGE_DRAFTER_CLASS`,
  `AVAILABILITY_SCHEDULER_CLASS` default to the engine base classes.
  `main()` accepts `*_cls` kwargs and overwrites the globals before the
  server starts. This is the seam that productized agents plug their
  subclasses into.

See `src/schedbot/mcp_server/server.py` for the reference implementation.

---

## Environment Variables

See `.env.example` for the full list. Required to run anything:
- `ANTHROPIC_API_KEY` — classification, drafting, slot reasoning

Required for the recommended (cal.com) integration:
- `CALCOM_API_KEY` — generated at https://cal.com/settings/developer/api-keys
- `CALCOM_WEBHOOK_SECRET` — for HMAC verification on inbound webhook calls

Required for the Google Calendar (secondary) integration:
- `GOOGLE_CALENDAR_CREDENTIALS_PATH` — path to the OAuth2 client JSON
- `GOOGLE_CALENDAR_TOKEN_PATH` — defaults to `./.google_calendar_token.json` (gitignored)

Optional alternatives:
- `CALENDLY_API_KEY` — Calendly personal access token
- `ACUITY_USER_ID` + `ACUITY_API_KEY` — Acuity Scheduling
- `SMTP_*` — for confirmation/reminder email sending
- `TWILIO_*` — for SMS reminders (install with `uv sync --extra sms`)
- `WEBHOOK_SIGNING_SECRET` — for the generic inbound webhook receiver

Never commit `.env` or `config.yaml` to git. Both are in `.gitignore`.

---

## Testing

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=src

# Run a specific test file
uv run pytest tests/test_availability.py
```

Tests use `pytest-asyncio` for async test support. Mock external API
calls with `unittest.mock.AsyncMock` — never make real API calls
(Anthropic, cal.com, Google) in tests.

---

## Common Gotchas

- **`schedbot` command not found:** activate `.venv` or prefix with `uv run`.
- **cal.com webhook signature failures:** confirm `CALCOM_WEBHOOK_SECRET`
  matches the secret you set on the cal.com side, and confirm your edge
  proxy isn't rewriting the body before signature verification.
- **Google Calendar OAuth first-run** requires a browser for the consent
  flow; subsequent runs use the cached token at `GOOGLE_CALENDAR_TOKEN_PATH`.
- **MCP server must use stdio transport.** Don't switch to HTTP without
  updating Claude Desktop config.
- **Config reload:** config is loaded once at MCP server startup; restart
  the server after changing `config.yaml`.
- **Buffer-time off-by-some-minutes surprises:** the buffer is owned by
  `ServiceConfig`, not by the appointment row. If you change a service's
  buffer mid-deployment, existing CONFIRMED rows still occupy the window
  they were originally written with — adjust by re-confirming or by
  cancelling+rebooking.
- **Naive datetimes:** if you ever see a `datetime.utcnow()` call sneak
  in, that's a bug; use `now_utc()` from `_time.py`.

---

## Project Status

See `README.md` for the full feature overview. Current phase:
**v0.1.0 / initial scaffold**.

Stubs that still need implementation:
- `src/schedbot/sources/email_parser.py` — heuristic + AI parser is
  scaffolded; deployments wire it to a real inbox elsewhere.
- Acuity Scheduling connector — listed in config + .env but not yet
  implemented as a `BookingSource`.
- SMTP/Twilio sender layer — `OutreachConfig` knobs exist; the actual
  send-side worker is wired up in downstream personas for now.

---

## License

AGPL-3.0. Same rules as every AGPL codebase: modifications served over
the network must be open-sourced under AGPL. See `LICENSE`.
