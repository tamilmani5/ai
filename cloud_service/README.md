# Cloud assistant service

This is a separate cloud process for tasks that must keep working while the
Windows voice client is closed. It currently supports a 6:00 AM daily briefing
call, a spoken conversation during that call, task-history queries, and durable
reminder creation. It does not yet connect Gmail, WhatsApp, or Instagram.

## Required Railway variables

Set these on the Railway service. Do not put their values in source control.

- `ASSISTANT_API_TOKEN`: long random secret required by `/api/*` routes.
- `ANTHROPIC_API_KEY`: cloud language-model credential for chat and voice turns.
- `ANTHROPIC_MODEL`: optional; defaults to `claude-sonnet-4-6`.
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`: Twilio Voice credentials.
- `OWNER_PHONE_NUMBER`: owner's number in E.164 format.
- `PUBLIC_BASE_URL`: public HTTPS URL for this service, without a trailing slash.
- `ASSISTANT_TIMEZONE`: defaults to `Asia/Kolkata`; the briefing runs at 06:00.
- `CLOUD_DB_PATH`: optional; defaults to `/data/assistant.sqlite3`.

Attach a Railway volume at `/data` before relying on reminders/task history.
Set the service to one replica to avoid duplicate scheduled calls. The scheduled
call is disabled in practice until the required Twilio variables are present.

## Endpoints

- `GET /healthz`: readiness and feature-configuration status, no secret values.
- `POST /api/chat`: authenticated conversational turn. JSON: `{"text":"...","session_id":"optional"}`.
- `POST /api/reminders`: authenticated reminder. JSON: `{"task":"...","minutes":10,"call_when_due":false}`. Set `true` only when the owner explicitly requested a call when it is due.
- `GET /api/reminders`: authenticated list of pending reminders.
- `POST /api/task-events`: authenticated task outcome for the morning briefing.
- `GET /api/briefing`: authenticated preview of the current briefing.
- `POST /twilio/turn`: Twilio signed speech webhook used during the phone call.

The service does not send messages or place arbitrary calls. It asks the model
to be clear when a requested account integration is not connected. Only the
scheduled wake-up call is implemented here.

