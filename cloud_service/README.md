# Cloud assistant service

This service runs without the Windows laptop. It supports a 6:00 AM daily
briefing call, conversational voice turns, reminders, task history, and Gmail
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
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`, `OWNER_PHONE_NUMBER`.
- `PUBLIC_BASE_URL`: public HTTPS service URL.
- `ASSISTANT_TIMEZONE`: defaults to `Asia/Kolkata`; briefing at 06:00.

Attach a Railway volume at `/data`, and use one replica to avoid duplicate
scheduler jobs. Gmail stays idle until the owner completes one-time OAuth.

## Endpoints

- `GET /healthz`: service and integration readiness, no secret values.
- `POST /api/chat`: authenticated conversation.
- `POST /api/google/credentials`: authenticated OAuth token handoff from desktop.
- `GET /api/google/status`: Gmail connection and pending draft subjects.
- `POST /api/reminders`, `GET /api/reminders`: durable reminders.
- `POST /api/task-events`, `GET /api/briefing`: overnight event history and preview.
- `/dashboard`: private owner dashboard, review inbox, memory and connection management.
- `POST /twilio/turn`: Twilio signed speech webhook.

