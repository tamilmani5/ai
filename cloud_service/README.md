# Cloud assistant service

This service runs without the Windows laptop. It supports a 6:00 AM daily
briefing call through Vonage Voice API by default, with Vapi and Twilio options, conversational voice turns,
persistent pending/ongoing tasks,
reminders, task history, and Gmail
background review. When Gmail is connected, it checks unread mail every 15
minutes and saves reply drafts for messages that appear to need a personal
response. It never marks messages read or sends automatically.

The service also provides a phone-friendly owner dashboard at `/dashboard`.
Sign in with the same `ASSISTANT_API_TOKEN` configured for the desktop app. It
shows cloud connection status, reminders, needs-attention events, pending Gmail
drafts, encrypted preferences, and saved chat history. A user must review a
Gmail draft and confirm its recipient and subject before sending from the
dashboard. Gmail can be revoked there by typing `DISCONNECT GMAIL`.

Cloud conversation history and saved preferences are encrypted in SQLite using
a key derived from `ASSISTANT_API_TOKEN`. Keep that token stable to retain
access to saved memory; changing it makes older encrypted memory unreadable.
The dashboard login uses a 12-hour secure, HTTP-only cookie and does not store
the access token in browser storage. Keep the persistent Railway volume mounted
at `/data`.

Cloud chat can use Anthropic web search for current or explicitly researched
questions and returns source links. Searches consume Anthropic API usage.

## Gmail consent and send approval

Connect Gmail once from the desktop assistant by saying **connect Gmail**.
Google opens a sign-in and consent page. The desktop app sends the resulting
offline refresh token to this service over HTTPS; the service encrypts it at
rest in its Railway volume. Google may ask again if access is revoked or the
refresh grant expires. The Gmail integration needs read-only, compose, and send
scopes because approved drafts can be sent during a voice conversation.

The cloud assistant may read mail and save reply drafts without asking each
time. It must never send until the owner explicitly says “send” and identifies
the draft by its exact subject in the same turn. A morning call lists pending
draft subjects and confirms none were sent. Email text is treated as untrusted
content and cannot override assistant instructions.

## Required Railway variables

Set these on the Railway service. Keep values out of source control.

- `ASSISTANT_API_TOKEN`: random secret for `/api/*` routes.
- `ANTHROPIC_API_KEY`: cloud language-model credential.
- `GMAIL_TOKEN_ENCRYPTION_KEY`: Fernet key used to encrypt Gmail credentials.
- `VOICE_PROVIDER=vonage` (default), `VONAGE_API_KEY`, `VONAGE_APPLICATION_ID`,
  `VONAGE_PRIVATE_KEY`, `VONAGE_SIGNATURE_SECRET`, `VONAGE_FROM_NUMBER`,
  `OWNER_PHONE_NUMBER`.
- For Vapi, set `VOICE_PROVIDER=vapi`, `VAPI_API_KEY`, `VAPI_ASSISTANT_ID`,
  `VAPI_PHONE_NUMBER_ID`, `VAPI_SERVER_SECRET`, and `OWNER_PHONE_NUMBER`.
- `PUBLIC_BASE_URL`: public HTTPS service URL.
- `ASSISTANT_TIMEZONE`: defaults to `Asia/Kolkata`; briefing at 06:00.

Vonage needs a Voice API application with signed webhooks enabled and a number
or verified caller ID assigned to the account. Store the downloaded private
key in Railway as `VONAGE_PRIVATE_KEY`, with newlines preserved or represented
as `\\n`. International calls to India are supported, while domestic calls
from a local Indian caller ID are not supported by Vonage. To keep the previous
Twilio adapter instead, set `VOICE_PROVIDER=twilio` and provide the four
`TWILIO_*` variables.

### Vapi setup

Create and publish a Vapi assistant, then import a phone number that supports
outbound calls. Vapi free phone numbers do not support outbound calls. In the
assistant's system prompt, instruct it to use the `assistant_turn` function for
requests that need the cloud assistant's task, reminder, or Gmail capabilities.
Add this function to the assistant's model tools:

```json
{
  "type": "function",
  "function": {
    "name": "assistant_turn",
    "description": "Ask the owner's cloud personal assistant to handle a request or answer using its task, reminder, and Gmail tools. Use for each substantive user request.",
    "parameters": {
      "type": "object",
      "properties": { "request": { "type": "string", "description": "The owner's request in their own words." } },
      "required": ["request"]
    }
  }
}
```

Set the assistant's Webhook Server URL to
`https://<your-railway-domain>/vapi/webhook`. Add an `X-Vapi-Secret` custom
credential in Vapi with the same random value as `VAPI_SERVER_SECRET`. Add that
same value as a Railway variable; do not put it in the URL. Set the Vapi API
key, published assistant ID, and imported phone-number ID in Railway. The call
endpoint always calls the configured `OWNER_PHONE_NUMBER`, not a model-provided
destination. Vapi sends `assistant_turn` requests to the webhook, which forwards
them to the cloud assistant's existing conversational tools and returns its
answer. In Railway, set `VOICE_PROVIDER=vapi`, review staged changes, and
deploy. You can then check `GET /healthz` for `morning_call_configured` before
relying on the scheduled call.

Attach a Railway volume at `/data`, and use one replica to avoid duplicate
scheduler jobs. The 06:00 briefing uses `ASSISTANT_TIMEZONE` (default
`Asia/Kolkata`). The owner can speak during the call to add a tracked task,
change its status, ask about reminders, or set a reminder; speech turns use the
same cloud conversation tools as `/api/chat`. A reminder only triggers a phone
call when the owner explicitly requests a call. Gmail stays idle until the
owner completes one-time OAuth.

## Endpoints

- `GET /healthz`: service and integration readiness, no secret values.
- `POST /api/chat`: authenticated conversation.
- `POST /api/google/credentials`: authenticated OAuth token handoff from desktop.
- `GET /api/google/status`: Gmail connection and pending draft subjects.
- `POST /api/reminders`, `GET /api/reminders`: durable reminders.
- `POST /api/tasks`, `GET /api/tasks`, `PATCH /api/tasks/status`: tracked tasks.
- `POST /api/task-events`, `GET /api/briefing`: overnight event history and preview.
- `/dashboard`: private owner dashboard, review inbox, memory and connection management.
- `POST /twilio/turn`: Twilio signed speech webhook.
- `POST /vonage/answer`, `POST /vonage/turn`, `POST /vonage/event`: signed Vonage Voice API webhooks.
- `POST /vapi/webhook`: authenticated Vapi server webhook for assistant tools.

