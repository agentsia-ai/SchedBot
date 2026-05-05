# SchedBot Architecture

A short reference for contributors and for the downstream personas that
subclass these base classes (e.g. Agentsia's Nova).

---

## Data flow

```
┌──────────────┐  pull/push  ┌──────────────┐ ingest  ┌──────────────┐
│  cal.com /   │────────────▶│ RawBooking   │────────▶│ Appointment  │
│  Calendly /  │             │ Request      │ idempot.│ (REQUESTED)  │
│  webhook /   │             └──────────────┘ on       └──────┬───────┘
│  email /     │                              external_id     │ confirm
│  manual      │                                              ▼
└──────────────┘                                       ┌──────────────┐
                                                       │ Appointment  │
                                                       │ (CONFIRMED)  │
                                                       │ + drafted    │
                                                       │   confirm    │
                                                       │ + queued     │
                                                       │   reminders  │
                                                       └──────┬───────┘
                                                              │ approve (CLI/MCP)
                                                              ▼
                                                       ┌──────────────┐
                                                       │ Sent         │
                                                       │ (SMTP/SMS)   │
                                                       └──────┬───────┘
                                                              │ event happens
                                                              ▼
                                                       ┌──────────────┐
                                                       │ COMPLETED /  │
                                                       │ NO_SHOW      │
                                                       │ + drafted    │
                                                       │   follow-up  │
                                                       └──────────────┘

Customer reschedules ──▶ reschedule_appointment ──▶ alternative slots + drafted reply
Customer cancels     ──▶ cancel_appointment     ──▶ cancelled + waitlist promotion
```

Every transition is explicit and one-step. The engine has no "confirm
and send in one call" path, no "draft and send in one call" path, and
no background autosend.

---

## Which base class touches what

| Stage                       | Class / module                         | Inputs                                    | Outputs                          |
|-----------------------------|----------------------------------------|-------------------------------------------|----------------------------------|
| Pull bookings               | `BookingSource` subclass               | None                                      | `RawBookingRequest` stream       |
| Push bookings (webhooks)    | `WebhookReceiver` subclass             | HTTP body + headers                       | `RawBookingRequest`              |
| Ingest into DB              | `service.ingest_booking`               | `RawBookingRequest` + DB                  | `Appointment` (idempotent)       |
| Classify ambiguous request  | `RequestClassifier` **(pluggable)**    | inbound text                              | `ClassificationResult`           |
| Generate candidate slots    | `scheduler.AvailabilityEngine`         | `ServiceConfig` + busy intervals + window | `list[TimeSlot]`                 |
| Pick which slots to offer   | `AvailabilityScheduler` **(pluggable)** | candidate slots + customer text          | chosen `TimeSlot[]` + reasoning  |
| Atomic slot reservation     | `AppointmentDatabase.reserve_slot`     | appt_id + service + window                | bool (true iff this caller won)  |
| Draft customer messages     | `MessageDrafter` **(pluggable)**       | Appointment + context                     | `(subject, body)`                |
| Enqueue reminder records    | `scheduler.ReminderScheduler`          | Appointment + RemindersConfig             | `list[ReminderRecord]`           |
| Manage waitlist             | `scheduler.WaitlistManager`            | service_slug + Appointment                | `WaitlistEntry` lifecycle ops    |
| Send confirmation/reminder  | (downstream sender, not in engine)     | APPROVED records                          | provider message id              |

The three pluggable base classes (`RequestClassifier`, `MessageDrafter`,
`AvailabilityScheduler`) are the only AI extension points a productized
persona needs. Every non-AI component (sources, scheduler, DB, CLI, MCP
server) is engine-owned and downstream deployments use it unchanged.

---

## Database layout

Single SQLite file (default `./data/schedbot.db`). Three tables:

- `appointments` — one row per appointment (every state)
- `reminders` — one row per reminder record (drafted / approved / sent / failed / cancelled)
- `waitlist` — one row per waitlist entry

JSON blobs hold list-valued and nested fields (`location_json`,
`tags_json`, `raw_data_json`). Denormalized columns enable fast
filtering:

- `appointments.status`, `appointments.service_slug`,
  `appointments.start_at`, `appointments.client_email`,
  `appointments.external_id` (looked up alongside `source` for webhook
  idempotency)
- `reminders.appointment_id`, `reminders.status`,
  `reminders.scheduled_for`
- `waitlist.service_slug`, `waitlist.status`

---

## Two database interlocks

The two most dangerous code paths in a scheduling engine are
double-booking and double-sending. Both are guarded by single-statement
SQLite UPDATEs whose WHERE clauses include the safety checks.

### 1. Atomic slot reservation (`reserve_slot`)

```sql
UPDATE appointments
   SET status='confirmed', confirmed_at=?, updated_at=?
 WHERE id = ?
   AND status = 'requested'
   AND NOT EXISTS (
       SELECT 1 FROM appointments other
        WHERE other.service_slug = ?
          AND other.status IN ('confirmed', 'reminded')
          AND other.start_at < ?
          AND other.end_at > ?
          AND other.id != ?
   )
```

If two clients (CLI + MCP, or two MCP calls) race on the same
REQUESTED row, exactly one UPDATE succeeds; the other affects 0 rows
and the caller is told the slot is now gone.

Buffer time isn't enforced inside this UPDATE — it's already baked into
`start_at` / `end_at` upstream by `AvailabilityEngine` when the slot is
generated. That keeps this UPDATE simple and lets per-service buffer
tuning work without schema changes.

### 2. Reminder send-guard (`mark_reminder_sent`)

```sql
UPDATE reminders
   SET status='sent', sent_at=?, provider_message_id=?
 WHERE id = ?
   AND status = 'approved'
   AND approval_token = ?
```

Each reminder has an `approval_token` (uuid) generated at draft time.
Approving the reminder doesn't change the token. The send path requires
both `status='approved'` and the matching token, so a CLI/MCP race or a
retried send can't double-fire.

---

## MCP server lifecycle

```
                   ┌─────────────────────────────┐
                   │  agentsia nova mcp   OR     │
                   │  schedbot mcp               │
                   │  OR python -m schedbot.mcp  │
                   └──────────────┬──────────────┘
                                  │ imports
                                  ▼
                ┌────────────────────────────────────┐
                │  schedbot.mcp_server.server.main() │
                │                                    │
                │  1. apply *_cls kwargs →           │
                │     REQUEST_CLASSIFIER_CLASS, etc. │
                │  2. load_config() ← cwd now set    │
                │  3. load_api_keys()                │
                │  4. AppointmentDatabase(...)       │
                │  5. stdio_server() run loop        │
                └──────────────┬─────────────────────┘
                               │ tool calls
                               ▼
                   ┌───────────────────────────┐
                   │ call_tool(name, args)     │
                   │   reads module globals:   │
                   │   config, keys, db,       │
                   │   *_CLASS constants       │
                   └───────────────────────────┘
```

Key property: steps 2–4 happen *inside* `main()`, not at module import.
This lets an outer caller (`agentsia-core`'s `AgentContext.activate()`)
`chdir` to a client-specific directory *before* the engine reads
`config.yaml` — so `./data/schedbot.db` resolves to the client's DB
and not whatever happened to be cwd when Python first loaded the module.

Stdout is reserved for JSON-RPC frames. Everything else (banners,
progress, debug) goes to stderr via `logging` or a `Console(stderr=True)`.
Violating this shows up as "Unexpected token" errors in the Claude
Desktop log.

---

## Time handling

SchedBot is timezone-sensitive in a way LeadGen / CustComm aren't.
Every appointment has a presentation timezone (`business.timezone`)
that's distinct from the canonical UTC instant we store.

The rule:
- **Store everything in UTC, always** — `now_utc()`, `to_iso()`, `parse_iso()`.
- **Display in the configured business timezone** — `to_local()`, `format_local()`.
- The DB schema only stores ISO strings; the row-to-model helpers in
  `crm/database.py` parse them back through `parse_iso()` so every
  Pydantic instance you handle is aware-UTC.

There is no path through which a naive datetime should enter the
database. If you ever see a `datetime.utcnow()` call sneak in, that's a
bug; use `now_utc()` from `_time.py`.

---

## Adding a persona subclass

A productized persona overrides only the three AI base classes,
keeping everything else default:

```python
# somewhere in agents/nova/drafter.py
from pathlib import Path
from schedbot.ai.drafter import MessageDrafter

_PROMPT_PATH = Path(__file__).parent / "prompts" / "drafter.txt"

class NovaMessageDrafter(MessageDrafter):
    SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")
```

The MCP server accepts `request_classifier_cls`, `message_drafter_cls`,
and `availability_scheduler_cls` kwargs on `main()`:

```python
from schedbot.mcp_server.server import main as mcp_main
await mcp_main(
    request_classifier_cls=NovaRequestClassifier,
    message_drafter_cls=NovaMessageDrafter,
    availability_scheduler_cls=NovaAvailabilityScheduler,
)
```

The CLI path does not currently expose this injection because the
engine's own CLI is the generic path; productized CLIs like
`agentsia nova ...` do the injection themselves at the MCP layer.
