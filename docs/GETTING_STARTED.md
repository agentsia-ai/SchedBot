# Getting Started with SchedBot

This guide takes you from a fresh clone to your first AI-drafted appointment
confirmation. Should take about 30 minutes.

---

## Prerequisites

- Python 3.12+
- `uv` package manager — install at https://docs.astral.sh/uv/
- An Anthropic API key (free credits available at https://console.anthropic.com)
- A cal.com account (free; the recommended primary integration)
- *(Optional)* a Google Cloud project + Calendar OAuth credentials, **only**
  if you want to use Google Calendar instead of cal.com — see
  `docs/API_KEYS.md` for that walkthrough.

---

## Step 1 — Clone the Repo

```bash
git clone https://github.com/agentsia-ai/SchedBot.git
cd SchedBot
```

---

## Step 2 — Install Dependencies

SchedBot uses `uv` for fast, reliable dependency management. **Do not mix
in raw `pip install` — it will diverge from `uv.lock`.**

```bash
# Install uv if you haven't already
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create the venv and install all dependencies (incl. dev tools)
uv sync --extra dev
```

After this, the `schedbot` command is available inside `.venv`.

To activate the environment for the session:
```bash
# macOS / Linux
source .venv/bin/activate

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

Or prefix all commands with `uv run`:
```bash
uv run schedbot pipeline
```

---

## Step 3 — Get Your API Keys

### Anthropic (required)
1. Go to https://console.anthropic.com → sign up
2. API Keys → Create Key
3. You get $5 in free credits — plenty for early testing

### cal.com API key (recommended primary integration)
1. Sign up / log in at https://cal.com
2. Settings → Developer → API Keys → Create
3. Save the key — it looks like `cal_live_...`
4. (Optional) Settings → Developer → Webhooks → add a subscription pointing
   at your endpoint, set a webhook secret, and grab that secret too.

That's it for the quick path. If you'd rather use Google Calendar, skip
ahead to `docs/API_KEYS.md` for the OAuth walkthrough — it takes ~15 min
longer than the cal.com path.

---

## Step 4 — Configure Your Environment

```bash
cp .env.example .env
```

Open `.env` and fill in at minimum:
```
ANTHROPIC_API_KEY=sk-ant-...
CALCOM_API_KEY=cal_live_...
CALCOM_WEBHOOK_SECRET=    # leave blank if you haven't set up webhooks yet
```

Everything else can stay at its default.

---

## Step 5 — Configure SchedBot

```bash
cp config.example.yaml config.yaml
```

Open `config.yaml` and set the identity + business fields at the top:

```yaml
operator_name: "Your Name"
operator_email: "you@example.com"

business:
  name: "Your Business Name"
  business_type: "your industry"
  timezone: "America/New_York"
  working_hours:
    - { weekday: 0, open_at: "09:00", close_at: "17:00" }
    # ... etc.
  services:
    - slug: "estimate"
      name: "On-site Estimate"
      duration_minutes: 30
      buffer_after_minutes: 15
```

The `services` list is the most important part — every booking has a
`service_slug` that must match one of these. Add one entry per bookable
service you offer; SchedBot uses the per-service `duration_minutes` and
`buffer_*_minutes` to generate availability and prevent overlaps.

Take some time on the `outreach.email_signature` once you're sending
live messages — it's appended verbatim to every outbound confirmation
and reminder.

---

## Step 6 — Initialize the Database

```bash
uv run schedbot pipeline
```

This creates `./data/schedbot.db` and prints an (empty) pipeline summary:

```
┌────────────────────┬───────┐
│ Status             │ Count │
├────────────────────┼───────┤
│ TOTAL              │ 0     │
└────────────────────┴───────┘
```

---

## Step 7 — Bring in Your First Booking

For the cal.com path, you have two options:

### Option A — Manual seed (fastest)

```bash
uv run schedbot schedule
```

→ no rows yet. Add one for testing via the MCP `book_appointment` tool, or
write a small script that calls `service.ingest_booking(...)` directly.
Manual seeding is fine for kicking the tires.

### Option B — Subscribe to cal.com webhooks

1. Stand up a public HTTPS endpoint (a small FastAPI / Lambda / Cloudflare
   Worker that calls into `schedbot.sources.calcom_webhooks.CalComWebhookReceiver.handle()`).
2. In cal.com → Settings → Developer → Webhooks → add a subscription:
   - URL: `https://your-domain.example/webhooks/calcom`
   - Secret: the value of `CALCOM_WEBHOOK_SECRET`
   - Events: `booking.created`, `booking.cancelled`, `booking.rescheduled`,
     `meeting.ended`
3. Trigger a test booking on your cal.com profile and watch your endpoint
   receive it.

Each webhook hit is decoded into a `RawBookingRequest` and persisted via
`service.ingest_booking`, which is idempotent on `(provider, external_id)`
— webhook replays are safe.

---

## Step 8 — See Today + Tomorrow's Schedule

```bash
uv run schedbot digest
```

This is the morning briefing. It groups appointments by day with status
and the first 60 characters of any intake notes:

```
Today (3)
┌───────┬───────────┬─────────────┬────────────────────┬──────────────────┐
│ Time  │ Status    │ Service     │ Client             │ Notes            │
├───────┼───────────┼─────────────┼────────────────────┼──────────────────┤
│ 09:30 │ confirmed │ Lawn Cut    │ s.smith@…          │ Side gate code…  │
│ 11:00 │ requested │ Estimate    │ jane@…             │ Asked about the… │
│ 14:00 │ confirmed │ Lawn Cut    │ a.jones@…          │                  │
└───────┴───────────┴─────────────┴────────────────────┴──────────────────┘
```

---

## Step 9 — Confirm an Appointment

For requests that came in REQUESTED status, confirming reserves the slot
atomically (defending against double-bookings) and drafts a confirmation
message and the configured reminder sequence:

```bash
uv run schedbot confirm <appointment-id>
# or just the first 8 chars of the id:
uv run schedbot confirm abcd1234
```

Output:
```
OK Confirmed abcd1234 — status=confirmed
  draft subject: Confirmed: Your On-site Estimate on Wed Apr 15
```

Both the confirmation and every reminder are sitting in `DRAFTED` status,
waiting for your approval. Nothing has been sent.

---

## Step 10 — Check Availability

When a customer asks "what do you have next Tuesday?", get answers from:

```bash
uv run schedbot availability estimate --days 7 --limit 10
```

This generates candidate slots that respect business hours, the service's
own duration + buffer, and any existing CONFIRMED appointment. Buffer time
is enforced — slots that would collide with the buffer of a neighbor are
dropped.

---

## Step 11 — Review Drafts

```bash
uv run schedbot review
```

Lists every drafted reminder and every drafted confirmation awaiting your
approval. To see one in detail:

```bash
uv run schedbot show abcd1234
```

You'll see the appointment, the drafted confirmation body, and the queued
reminder records (each with its scheduled fire time and the channel it
will use).

---

## Step 12 — Reschedule / Cancel / No-show

Each operation drafts the appropriate customer-facing message; nothing
sends until you approve.

```bash
# Reschedule — finds alternatives + drafts a "here are some options" reply
uv run schedbot reschedule abcd1234 --message "Customer wants to move to next week"

# Cancel — drafts the cancellation acknowledgement, optionally promotes
# the longest-waiting waitlist entry to OFFERED
uv run schedbot cancel abcd1234 --reason "weather"

# No-show — flags as missed, drafts the follow-up
uv run schedbot no-show abcd1234
```

---

## Step 13 — Connect to Claude Desktop (Recommended)

SchedBot's MCP server is the most pleasant way to operate day-to-day —
Claude can run all of the above tools conversationally. Start it:

```bash
uv run schedbot mcp
```

Then see `docs/MCP_SETUP.md` for the Claude Desktop configuration block.

---

## Common Issues

**`schedbot: command not found`**
→ Your virtual environment isn't activated. Run `source .venv/bin/activate`
  (or `.venv\Scripts\Activate.ps1` on Windows), or prefix commands with
  `uv run`.

**`Config file not found: config.yaml`**
→ Run `cp config.example.yaml config.yaml` and edit the identity fields.

**`ANTHROPIC_API_KEY is not set`**
→ Your `.env` file is missing or the key isn't set correctly.

**cal.com webhook signature failures**
→ Make sure `CALCOM_WEBHOOK_SECRET` exactly matches the secret you
  configured on cal.com's webhook subscription. Also: many edge proxies
  rewrite or re-encode the request body before forwarding it; if you're
  behind one, verify the signature on the *raw* incoming bytes.

**MCP server prints "Unexpected token" warnings in Claude Desktop**
→ Something in the engine is writing to stdout. Every new code path
  reachable from `mcp_server/server.py` must use `logging` or
  `Console(stderr=True)`, not `print` or a default `Console()`. See
  `CLAUDE.md` → *MCP Server Ground Rules*.

---

## Next Steps

- Read `docs/ARCHITECTURE.md` for the deep dive on how everything fits
  together
- Read `docs/API_KEYS.md` for the Google Calendar OAuth walkthrough (if
  you'd rather use Google Calendar than cal.com)
- Read `docs/MCP_SETUP.md` to wire SchedBot into Claude Desktop
- Read `CLAUDE.md` → *Customization Patterns* when you're ready to plug
  in a custom voice or persona
