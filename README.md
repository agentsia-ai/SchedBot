# SchedBot

> An AI-powered scheduling and appointment-management engine for small-business operators. Take bookings, draft confirmations, send reminders, manage cancellations and waitlists — all under human approval.

---

## Vision

Most scheduling tools either stop at "show me an availability widget" (Calendly, cal.com) or balloon into full operational suites you have to learn (Acuity, Setmore). The actual labor — replying to "can we move it?", chasing no-shows, drafting reminders that don't sound like a robot, juggling a waitlist — still falls on the operator.

**SchedBot** sits on top of whatever calendar tool you already use and does that labor:

- **AI-first** — Claude classifies inbound requests, drafts confirmations / reminders / reschedule options, and reasons about which slots to offer.
- **Calendar-agnostic** — cal.com is the recommended primary integration (simple API key, no OAuth dance). Google Calendar, Calendly, and Acuity are supported as alternatives so you don't have to switch tools.
- **MCP-native** — runs as an MCP server so you can run the entire scheduling desk conversationally from Claude Desktop.
- **Human-in-the-loop by default** — the engine *never* auto-sends. Every confirmation, reminder, reschedule reply, and waitlist offer requires an explicit approve step.
- **Atomic by construction** — slot reservation is a single guarded UPDATE that includes a non-overlap check, so CLI and MCP can't race each other into a double-booking.
- **Yours to white-label** — the engine is identity-free. Named personas (like Agentsia's Nova) live in downstream private repos as subclasses.

The goal: you spend 10 minutes a day approving AI-drafted confirmations, reschedules, and reminders. SchedBot does the rest.

---

## Core Concepts

### The flow

```
┌──────────────┐   ingest    ┌──────────────────┐   classify   ┌──────────────┐
│  cal.com /   │ ──────────▶ │ Appointment      │ ───────────▶ │ Appointment  │
│  webhook /   │             │ (REQUESTED)      │  Classifier  │ (TRIAGED)    │
│  email /     │             └────────┬─────────┘              └──────┬───────┘
│  manual      │                      │ confirm                       │ reschedule
└──────────────┘                      ▼                               ▼
                              ┌──────────────────┐            ┌──────────────────┐
                              │ Appointment      │            │ Drafted          │
                              │ (CONFIRMED)      │            │ reschedule reply │
                              │ + drafted        │            │ + alternative    │
                              │   confirmation   │            │   slots          │
                              │ + drafted        │            └────────┬─────────┘
                              │   reminders      │                     │ operator approves
                              └────────┬─────────┘                     ▼
                                       │ operator approves      ┌──────────────────┐
                                       ▼                        │ Customer picks,  │
                                ┌──────────────────┐            │ goes back to     │
                                │ Confirmation +   │            │ confirm path     │
                                │ reminders SENT   │            └──────────────────┘
                                └────────┬─────────┘
                                         │ event happens (or doesn't)
                                         ▼
                                ┌──────────────────┐
                                │ COMPLETED /      │
                                │ NO_SHOW          │
                                │ + drafted        │
                                │   follow-up      │
                                └──────────────────┘
```

No step can be skipped. A draft moves to `sent` only via the explicit approve → send path, with a DB-level token interlock that prevents double-sends across CLI and MCP.

### Three pluggable AI seams

Every class that talks to Claude is subclassable and prompt-overridable:

| Base class              | Where it lives                  | What it does                                            |
|-------------------------|---------------------------------|---------------------------------------------------------|
| `RequestClassifier`     | `src/schedbot/ai/classifier.py` | Classifies an inbound request: book / reschedule / cancel / question / uncertain |
| `MessageDrafter`        | `src/schedbot/ai/drafter.py`    | Drafts confirmations, reminders, reschedule options, cancellations, waitlist offers, no-show follow-ups |
| `AvailabilityScheduler` | `src/schedbot/ai/scheduler.py`  | Picks which candidate slots to actually offer the customer |

See [`CLAUDE.md`](./CLAUDE.md) → *Customization Patterns* for both customization paths (config-based prompt swap, or subclassing).

---

## Architecture

```
SchedBot/
├── src/
│   └── schedbot/                      # The package (standard Python src-layout)
│       ├── __init__.py
│       ├── cli.py                     # Click CLI entry point (`nova ...`)
│       ├── mcp.py                     # `python -m schedbot.mcp` MCP entry shim
│       ├── _time.py                   # UTC-aware datetime helpers
│       ├── models.py                  # Appointment, TimeSlot, BusinessHours, ReminderRecord, WaitlistEntry
│       ├── service.py                 # High-level coordination (book / confirm / reschedule / digest)
│       │
│       ├── ai/                        # Claude integration
│       │   ├── classifier.py          # RequestClassifier — pluggable
│       │   ├── drafter.py             # MessageDrafter — pluggable
│       │   └── scheduler.py           # AvailabilityScheduler — pluggable
│       │
│       ├── config/
│       │   └── loader.py              # Pydantic SchedBotConfig + APIKeys
│       │
│       ├── sources/                   # Booking-request connectors (inbound)
│       │   ├── base.py                # BookingSource + WebhookReceiver ABCs
│       │   ├── calcom.py              # cal.com REST API (recommended primary)
│       │   ├── calcom_webhooks.py     # cal.com webhook receiver + signature verify
│       │   ├── google_calendar.py     # Google Calendar API (secondary)
│       │   ├── calendly.py            # Calendly API (alternative)
│       │   ├── webhook.py             # Generic webhook receiver (website forms)
│       │   └── email_parser.py        # Parse booking requests from emails
│       │
│       ├── scheduler/                 # Deterministic scheduling logic
│       │   ├── availability.py        # AvailabilityEngine — generates candidate slots
│       │   ├── reminders.py           # ReminderScheduler — enqueues reminder records
│       │   └── waitlist.py            # WaitlistManager — add / promote / expire
│       │
│       ├── crm/
│       │   └── database.py            # AppointmentDatabase — async SQLite store
│       │
│       └── mcp_server/
│           └── server.py              # MCP server exposing all tools to Claude Desktop
│
├── docs/
│   ├── GETTING_STARTED.md
│   ├── API_KEYS.md                    # cal.com API key + Google Calendar OAuth setup
│   ├── MCP_SETUP.md
│   └── ARCHITECTURE.md
│
├── config.example.yaml                # Template config (copy → config.yaml)
├── pyproject.toml
├── .env.example
├── .gitignore
├── CLAUDE.md                          # AI-assistant context for this repo
└── LICENSE                            # AGPL-3.0
```

> **Customizing for a productized agent (named persona, tuned prompts)?**
> See [`CLAUDE.md`](./CLAUDE.md) → *Customization Patterns*. The base classes are
> designed to be subclassed or have their prompts swapped from a downstream repo.

---

## Quickstart

```bash
# 1. Clone
git clone https://github.com/agentsia-ai/SchedBot.git
cd SchedBot

# 2. Install (uv-managed; uv.lock pins exact deps)
uv sync --extra dev

# 3. Configure
cp .env.example .env                     # add your API keys (ANTHROPIC + CALCOM at minimum)
cp config.example.yaml config.yaml       # customize identity + services + working hours

# 4. Initialize (creates the SQLite DB; safe to run anytime)
uv run nova pipeline

# 5. See today + tomorrow's bookings
uv run nova digest

# 6. Check availability for a service
uv run nova availability estimate

# 7. Confirm a requested appointment (drafts confirmation + queues reminders)
uv run nova confirm <appointment-id>

# 8. Review drafts awaiting your approval
uv run nova review

# 9. Start the MCP server (connect to Claude Desktop)
uv run nova mcp
```

See [`docs/GETTING_STARTED.md`](./docs/GETTING_STARTED.md) for the full walkthrough, [`docs/API_KEYS.md`](./docs/API_KEYS.md) for the cal.com / Google Calendar credential setup, and [`docs/MCP_SETUP.md`](./docs/MCP_SETUP.md) for Claude Desktop setup.

---

## MCP Integration

SchedBot exposes itself as an MCP server so you can run the scheduling desk directly from Claude Desktop:

> "What's on the schedule today and tomorrow?"
>
> "A new estimate request just came in from Sam at 555-0148 — find me three options next Tuesday or Wednesday and draft the reply."
>
> "The 2pm reschedule went through — confirm it and queue the reminders."

### Available MCP tools

| Tool                       | What it does                                                  |
|----------------------------|---------------------------------------------------------------|
| `get_schedule_summary`     | Today and tomorrow's appointments with status                 |
| `check_availability`       | Find open slots for a service over the next N days            |
| `book_appointment`         | Create a new REQUESTED appointment from explicit fields       |
| `confirm_appointment`      | Reserve the slot atomically + draft confirmation + queue reminders |
| `reschedule_appointment`   | Generate alternative slots and draft the reply                |
| `cancel_appointment`       | Cancel + draft acknowledgement + (optional) waitlist offer    |
| `send_reminder`            | Draft (or re-draft) a reminder for a specific appointment     |
| `get_appointment_detail`   | Full detail on a specific appointment incl. reminder history  |
| `add_to_waitlist`          | Add a client to the waitlist for a service                    |
| `get_waitlist`             | View current waitlist entries (filterable)                    |
| `mark_no_show`             | Flag a missed appointment + draft the follow-up message       |

---

## Booking Source Maturity

Not all booking sources are equally battle-tested. As of v0.1.0:

| Source                    | Status              | Notes                                                            |
|---------------------------|---------------------|------------------------------------------------------------------|
| cal.com REST              | Primary             | Simple API key — the recommended quickstart path                 |
| cal.com webhooks          | Primary             | HMAC-verified; covers booking.created/cancelled/rescheduled/meeting.ended |
| Google Calendar API       | Secondary           | OAuth2 — see `docs/API_KEYS.md` for the full setup walkthrough   |
| Calendly                  | Supported           | Personal access token; pull-style                                |
| Acuity Scheduling         | Optional            | Basic auth; minimal connector skeleton                           |
| Generic webhook receiver  | Supported           | For website forms / embedded widgets; HMAC verification optional |
| Email parser              | Supported           | Heuristics + AI fallback; useful as a "plain-text request" fallback |

If you're starting fresh: **use cal.com**. The setup is one API key plus a webhook subscription, and the operator already runs `https://cal.com/agentsia/discovery-call` so the path is dogfooded.

---

## Safety / Guardrails

The engine ships locked down. A downstream deployment may relax some of these, but the defaults err toward humans:

- `outreach.require_approval = true` and `outreach.auto_send = false` — the engine physically refuses to send anything without an explicit approve step.
- Approve → send interlock: each reminder has an `approval_token` checked at send time, so CLI and MCP can't race each other into a double-send.
- **Atomic slot reservation** — `confirm` is a single guarded UPDATE with a NOT-EXISTS overlap check; concurrent confirms on the same slot can never both succeed.
- Buffer-time enforcement is per-service, baked into `find_overlapping` so AI proposals never collide with travel/reset time.
- Hard `max_message_chars` on generated drafts (default 1500).
- Confidence floor on the classifier — anything below `min_request_confidence` becomes `uncertain` and refuses to auto-act.
- Default `MessageDrafter` prompt forbids Claude from inventing prices, availability, or commitments not present in the appointment record.
- All datetimes are stored UTC and presented in `business.timezone` — there is no path for a naive datetime to enter the database.

---

## Productization / White-Label

SchedBot is the open-source engine. Named personas (voice, tone, tuned prompts) live in downstream private repos as subclasses:

```python
from schedbot.ai.drafter import MessageDrafter

class MyBrandMessageDrafter(MessageDrafter):
    SYSTEM_PROMPT = "You are MyBrand's scheduling voice..."
```

That subclass, plus a `config.yaml` pointing at it via the agent runtime, is the whole productization surface. See [`CLAUDE.md`](./CLAUDE.md).

---

## License

AGPL-3.0 — free to use, modify, and distribute. If you run a modified version as a network service, you must open-source your modifications under the same license. See [LICENSE](LICENSE) for full terms.

---

*A sibling engine to [LeadGen](https://github.com/agentsia-ai/LeadGen) (outbound) and [CustComm](https://github.com/agentsia-ai/CustComm) (conversational). Same architecture, different surface: SchedBot runs the calendar.*
