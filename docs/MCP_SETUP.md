# MCP Setup — Connecting SchedBot to Claude Desktop

SchedBot ships an MCP (Model Context Protocol) server so you can run the
entire scheduling desk conversationally from Claude Desktop.

There are two ways to run it depending on whether you're using SchedBot
standalone or as the engine for a productized agent like Nova.

---

## Option A: Standalone (SchedBot as a generic engine)

For operators running the public SchedBot engine on its own.

### Step 1: Install SchedBot

```bash
git clone https://github.com/agentsia-ai/SchedBot.git
cd SchedBot
uv sync --extra dev                      # creates .venv and installs SchedBot
cp .env.example .env                     # fill in Anthropic + cal.com keys
cp config.example.yaml config.yaml       # customize identity + services
```

### Step 2: Configure Claude Desktop

Find your Claude Desktop config file:

| OS      | Path                                                                |
|---------|---------------------------------------------------------------------|
| macOS   | `~/Library/Application Support/Claude/claude_desktop_config.json`   |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json`                       |

Add SchedBot to the `mcpServers` section:

```json
{
  "mcpServers": {
    "schedbot": {
      "command": "python",
      "args": ["-m", "schedbot.mcp"],
      "cwd": "/absolute/path/to/your/SchedBot"
    }
  }
}
```

On Windows, because Claude Desktop doesn't inherit your shell's PATH, it's
safest to point at the venv's absolute executable path directly. Replace
`C:\\path\\to\\your\\SchedBot` with the real directory where you cloned
the repo:

```json
{
  "mcpServers": {
    "schedbot": {
      "command": "C:\\path\\to\\your\\SchedBot\\.venv\\Scripts\\schedbot.exe",
      "args": ["mcp"],
      "cwd": "C:\\path\\to\\your\\SchedBot"
    }
  }
}
```

The `cwd` matters: the MCP server resolves relative paths in `config.yaml`
(database, prompt overrides, Google token cache) from this working
directory.

---

## Option B: Productized agent (e.g. Nova via agentsia-core)

If you're running a named-persona deployment of SchedBot, use the agent
runtime's CLI entry point instead. It injects the agent's tuned
`RequestClassifier` / `MessageDrafter` / `AvailabilityScheduler`
subclasses into the MCP server automatically.

For Nova (in the `agentsia-core` private repo):

```json
{
  "mcpServers": {
    "nova": {
      "command": "agentsia",
      "args": ["nova", "mcp"]
    }
  }
}
```

On Windows, again, point at the absolute exe path. Replace
`C:\\path\\to\\your\\agentsia-core` with your real `agentsia-core` clone
root:

```json
{
  "mcpServers": {
    "nova": {
      "command": "C:\\path\\to\\your\\agentsia-core\\.venv\\Scripts\\agentsia.exe",
      "args": ["nova", "mcp"]
    }
  }
}
```

For per-client deployments of Nova:

```json
{
  "mcpServers": {
    "nova-greenleaf": {
      "command": "agentsia",
      "args": ["nova", "--client", "greenleaf_landscaping", "mcp"]
    }
  }
}
```

The `agentsia` CLI handles config layering, env loading, and
working-directory resolution itself — no `cwd` needed in the JSON.

> Heads up: Claude Desktop runs MCP commands without your shell's PATH on
> some systems. If `agentsia` isn't found, replace `"command": "agentsia"`
> with the absolute path output by `which agentsia` (or `where agentsia`
> on Windows).

---

## Step 3: Restart Claude Desktop

After saving the config, fully quit and relaunch Claude Desktop. You
should see a tools icon indicating MCP tools are available.

## Step 4: Talk to your schedule

You can now say things like:

> "What's on the schedule today and tomorrow?"

> "A new estimate request just came in from Sam at 555-0148 — find me
> three options next Tuesday or Wednesday and draft the reply."

> "The 2pm reschedule went through — confirm it and queue the reminders."

> "Mark the 9am as a no-show and draft the follow-up."

> "Add Pat Jones to the waitlist for lawn-cut, then tell me who's on
> there."

---

## Available MCP tools

| Tool                       | What it does                                                     |
|----------------------------|------------------------------------------------------------------|
| `get_schedule_summary`     | Today and tomorrow's appointments, grouped, with status          |
| `check_availability`       | Find open slots for a service over the next N days               |
| `book_appointment`         | Create a new REQUESTED appointment from explicit fields          |
| `confirm_appointment`      | Atomically reserve the slot + draft confirmation + queue reminders |
| `reschedule_appointment`   | Generate alternative slots and draft the reply                   |
| `cancel_appointment`       | Cancel + draft acknowledgement + (optional) waitlist offer       |
| `send_reminder`            | Draft (or re-draft) a reminder for a specific appointment        |
| `get_appointment_detail`   | Full detail on a specific appointment incl. reminder history     |
| `add_to_waitlist`          | Add a client to the waitlist for a service                       |
| `get_waitlist`             | View current waitlist entries (filterable)                       |
| `mark_no_show`             | Flag a missed appointment + draft the follow-up message          |

`confirm_appointment` and any sender are intentionally separate — the MCP
protocol must never allow a single conversational turn to both create
the confirmation and push it to a customer. That's the core "no
auto-send" guardrail expressed at the tool surface.

---

## Troubleshooting

**Tools not showing up in Claude Desktop:**
- Confirm the `cwd` path is correct and absolute (Option A only).
- Confirm `schedbot` / `agentsia` resolves on your PATH, or use the absolute
  executable path (see the Windows example above).
- Check Claude Desktop logs:
  - macOS: `~/Library/Logs/Claude/`
  - Windows: `%APPDATA%\Claude\Logs\`

**"Unexpected token" or JSON parse warnings in the Claude Desktop log:**
- Something in the engine wrote non-JSON to stdout. SchedBot routes all
  logs to stderr by design; if you see this after a code change, find
  the stray `print()` or default `Console()` and switch it to `logging`
  or `Console(stderr=True)`. See `CLAUDE.md` → *MCP Server Ground Rules*.

**`check_availability` always returns 0 slots:**
- Confirm `business.working_hours` covers the relevant weekday.
- Confirm the service slug exists in `business.services`.
- Lower `scheduler.min_lead_minutes` if you're testing inside the
  default 60-minute lookahead floor.

**Classifier always returns `uncertain`:**
- Confirm `ANTHROPIC_API_KEY` is set.
- Lower `ai.min_request_confidence` if your inbound messages are
  legitimately ambiguous.
- Run `schedbot --debug ...` on a related CLI command to see raw Claude
  responses surfaced through the logger.

**`confirm_appointment` returns "Could not confirm":**
- The appointment isn't in `REQUESTED` status (it may already be
  `CONFIRMED` or `CANCELLED`).
- An overlapping CONFIRMED appointment already exists for the same
  service. Run `schedbot show <id>` and `schedbot availability <service>` to
  inspect.

**cal.com webhook signature failures:**
- `CALCOM_WEBHOOK_SECRET` must exactly match the webhook secret you set
  on cal.com.
- Some edge proxies rewrite or re-encode the JSON body before
  forwarding; verify the signature against the *raw* incoming bytes.
