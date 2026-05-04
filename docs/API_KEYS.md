# API Keys & Credentials Setup

SchedBot is calendar-agnostic, but each integration has a slightly
different credentials shape. This doc walks through every supported
provider; you only need to set up the one(s) you actually plan to use.

The strong recommendation is **start with cal.com** — it's a single API
key, no OAuth flow, and the operator already runs
`https://cal.com/agentsia/discovery-call` so the path is dogfooded.

---

## Anthropic (required for everything)

SchedBot uses Claude for request classification, message drafting, and
slot-picking reasoning.

1. Go to https://console.anthropic.com → sign up
2. **API Keys → Create Key**
3. Copy the key into `.env`:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```

Free tier includes $5 of credits — plenty to test the engine end to end.

---

## cal.com (recommended primary)

Why this one first: simple API key, no OAuth dance, comprehensive REST +
webhook coverage, free tier is generous.

### API key

1. Sign up / log in at https://cal.com
2. **Settings → Developer → API Keys → Create**
3. Save the key — it looks like `cal_live_...`
4. Add to `.env`:
   ```
   CALCOM_API_KEY=cal_live_...
   ```

That's the entire setup for the pull (REST polling) path. The connector at
`src/schedbot/sources/calcom.py` will start working immediately.

### Webhook (recommended for real-time event ingestion)

For booking.created / cancelled / rescheduled and meeting.ended (used for
no-show detection), wire up a webhook subscription:

1. **Settings → Developer → Webhooks → New Webhook**
2. Subscriber URL: `https://your-domain.example/webhooks/calcom`
   (whatever public HTTPS endpoint you've stood up to forward into
   `CalComWebhookReceiver.handle()`)
3. Set a webhook secret — anything random and long. Save it.
4. Subscribe to: `BOOKING_CREATED`, `BOOKING_CANCELLED`,
   `BOOKING_RESCHEDULED`, `MEETING_ENDED`
5. Add the secret to `.env`:
   ```
   CALCOM_WEBHOOK_SECRET=<the-secret-you-just-set>
   ```

The receiver verifies the HMAC-SHA256 signature on every incoming request
against this secret. **Leaving the secret blank disables verification** —
only safe if you're already behind an authenticated edge.

### Username + default event type

In `config.yaml`:
```yaml
calcom:
  enabled: true
  username: "your-cal-com-username"        # the part after cal.com/
  default_event_type_slug: "estimate"      # one of your event types
```

The username is the slug after `cal.com/` in your booking URL. For
`https://cal.com/agentsia/discovery-call`, the username is `agentsia` and
the event-type slug is `discovery-call`.

---

## Google Calendar (secondary, optional)

Use this only if a client doesn't want to move off Google Calendar onto
cal.com. The OAuth dance is more involved than the cal.com path.

### Step 1 — Google Cloud project

1. Go to https://console.cloud.google.com/
2. **Select project → New Project** — give it any name (e.g. "SchedBot —
   $client").
3. With the project selected, go to **APIs & Services → Library** and
   enable the **Google Calendar API**.

### Step 2 — OAuth consent screen

1. **APIs & Services → OAuth consent screen**
2. User type: **External** (unless you're inside a Workspace org and want
   to limit to internal users — then use Internal).
3. Fill in:
   - App name: `SchedBot — <client name>`
   - User support email: your email
   - Developer contact email: your email
4. Scopes — click **Add or Remove Scopes**, search for and add:
   - `.../auth/calendar.readonly` (always required)
   - `.../auth/calendar.events` (only if `google_calendar.write_enabled: true`)
5. Test users — while the app is in `Testing` status, you must add every
   Google account that will authorize SchedBot here. For a single-tenant
   deployment, just your own.

### Step 3 — OAuth client

1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
2. Application type: **Desktop app**
3. Name: anything (e.g. "SchedBot Desktop")
4. **Download JSON** — save it inside your SchedBot checkout. The default
   path is `./google_credentials.json`, which is `.gitignored`.

### Step 4 — Wire it into `.env`

```
GOOGLE_CALENDAR_CREDENTIALS_PATH=./google_credentials.json
GOOGLE_CALENDAR_TOKEN_PATH=./.google_calendar_token.json
```

The token path is where the cached refresh token will live after the
first authorization (also `.gitignored`).

### Step 5 — Wire it into `config.yaml`

```yaml
google_calendar:
  enabled: true
  calendar_id: "primary"          # or a specific calendar id
  write_enabled: false             # true = SchedBot can create events
```

Use `"primary"` for the user's main calendar, or the calendar id from
**Calendar Settings → Integrate calendar → Calendar ID** if you want
SchedBot to operate against a shared business calendar.

### Step 6 — First-run consent

The first time SchedBot makes a Google Calendar request, the connector
will open a browser window asking you to grant the requested scopes.
After consent, a refresh token is cached at `GOOGLE_CALENDAR_TOKEN_PATH`
and subsequent runs are non-interactive.

If you're running headless (CI / a server), do the first consent on your
laptop, then copy the resulting `.google_calendar_token.json` to the
server alongside the credentials JSON.

### "Access blocked: has not completed Google verification"

While your OAuth app is in `Testing` mode it can only be authorized by
accounts you've explicitly added under **OAuth consent screen → Test
users**. Add the account you're trying to authorize, then retry.

To remove this warning entirely, you have to go through Google's
verification process — which for a single-tenant calendar integration is
usually unnecessary. Stay in Testing.

---

## Calendly (alternative)

For clients already on Calendly who don't want to move.

1. Sign in at https://calendly.com
2. **Integrations & Apps → API & Webhooks → Personal access token**
3. Copy the token into `.env`:
   ```
   CALENDLY_API_KEY=...
   ```
4. In `config.yaml`:
   ```yaml
   calendly:
     enabled: true
     organization_uri: "https://api.calendly.com/organizations/<your-org-uuid>"
     user_uri: "https://api.calendly.com/users/<your-user-uuid>"
   ```

You can find your `organization_uri` and `user_uri` by hitting
`https://api.calendly.com/users/me` with the personal token and reading
the response.

---

## Acuity Scheduling (alternative)

For clients on Acuity. Simpler than Google but less rich than cal.com.

1. Log in at https://secure.acuityscheduling.com
2. **Integrations → API**
3. Note the User ID and API Key shown there.
4. Add to `.env`:
   ```
   ACUITY_USER_ID=...
   ACUITY_API_KEY=...
   ```

The Acuity connector is intentionally minimal in v0.1.0; expand the
`BookingSource` implementation in `src/schedbot/sources/acuity.py` (TBD)
when you wire up an Acuity-first deployment.

---

## SMTP (sending confirmations + reminders by email)

The engine doesn't include a "sender" by default; downstream personas
plug their own SMTP / Gmail / SendGrid sender into the outreach layer.
The `.env` shape covers the common case (Gmail SMTP):

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=<app-password>          # https://support.google.com/accounts/answer/185833
SMTP_FROM_EMAIL=you@gmail.com
SMTP_FROM_NAME="Your Business Name"
```

For Gmail accounts, **use an app password**, not your real password.
2-step verification must be on first.

---

## Twilio (optional SMS reminders)

Install the optional dep:

```bash
uv sync --extra sms
```

Then:

1. Sign up at https://www.twilio.com
2. **Console → Account → API Keys & Tokens** — copy the Account SID + Auth
   Token.
3. Buy or port a phone number under **Phone Numbers → Manage → Active**.
4. Add to `.env`:
   ```
   TWILIO_ACCOUNT_SID=AC...
   TWILIO_AUTH_TOKEN=...
   TWILIO_FROM_NUMBER=+15555550100
   ```

In `config.yaml`, make sure `reminders.channels` includes `"sms"` and
that your services collect a phone number for the client.

---

## Inbound webhook receiver

If you accept booking requests from a website contact form or an
embedded widget, sign every request HMAC-SHA256 with a shared secret:

```
WEBHOOK_SIGNING_SECRET=<long random string>
```

Then on the form-handler side, sign the JSON body with the same secret
and pass the signature in the header configured under
`webhook.hmac_header` in `config.yaml` (`X-SchedBot-Signature` by
default). Leaving the secret blank disables verification — only safe if
the receiver is already behind an authenticated edge.

---

## Where credentials live

| Item                         | Lives in            | Gitignored? |
|------------------------------|---------------------|-------------|
| API keys, tokens, secrets    | `.env`              | yes         |
| OAuth client JSON (Google)   | `./google_credentials.json` (path configurable) | yes |
| OAuth refresh token (Google) | `./.google_calendar_token.json` (path configurable) | yes |
| Business config + identity   | `config.yaml`       | yes         |

Never commit any of these. The `.gitignore` already covers them.
