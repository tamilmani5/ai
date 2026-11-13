"""Cloud companion service for scheduled briefings, reminders, and voice turns.

The Windows assistant stays local. This small service handles cloud-safe
features while that computer is off. Secrets are read only from environment
variables supplied by the hosting provider.
"""
from __future__ import annotations

import html
import json
import logging
import os
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from twilio.request_validator import RequestValidator
from twilio.rest import Client

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("assistant.cloud")

APP_TIMEZONE = os.environ.get("ASSISTANT_TIMEZONE", "Asia/Kolkata")
DATA_DIR = Path(os.environ.get("CLOUD_DATA_DIR", "/data"))
DB_PATH = Path(os.environ.get("CLOUD_DB_PATH", str(DATA_DIR / "assistant.sqlite3")))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
API_TOKEN = os.environ.get("ASSISTANT_API_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER", "")
OWNER_PHONE_NUMBER = os.environ.get("OWNER_PHONE_NUMBER", "")

app = FastAPI(title="Personal Assistant Cloud Service", docs_url=None, redoc_url=None)
_scheduler: BackgroundScheduler | None = None
_conversation_lock = threading.RLock()
_conversations: dict[str, list[dict[str, str]]] = {}


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id TEXT PRIMARY KEY,
                task TEXT NOT NULL,
                due_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                notify_by TEXT NOT NULL DEFAULT 'briefing',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_events (
                id TEXT PRIMARY KEY,
                task TEXT NOT NULL,
                outcome TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS task_events_time_idx ON task_events(occurred_at);
            CREATE TABLE IF NOT EXISTS service_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _local_now() -> datetime:
    return _now().astimezone(ZoneInfo(APP_TIMEZONE))


def _require_api_token(authorization: str | None) -> None:
    if not API_TOKEN:
        raise HTTPException(status_code=503, detail="Cloud API token is not configured.")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not secrets.compare_digest(supplied, API_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized.")


def _morning_briefing() -> str:
    since = (_now() - timedelta(hours=12)).isoformat()
    with _connect() as conn:
        events = conn.execute(
            "SELECT task, outcome FROM task_events WHERE occurred_at >= ? ORDER BY occurred_at DESC LIMIT 12",
            (since,),
        ).fetchall()
        reminders = conn.execute(
            "SELECT task, due_at FROM reminders WHERE status = 'pending' ORDER BY due_at LIMIT 8"
        ).fetchall()

    completed = [row["task"] for row in events if row["outcome"] == "completed"]
    attempted = [row["task"] for row in events if row["outcome"] != "completed"]
    parts = ["Good morning. Here is your overnight update."]
    if completed:
        parts.append("Completed: " + "; ".join(completed[:6]) + ".")
    else:
        parts.append("I have no completed cloud tasks recorded in the last 12 hours.")
    if attempted:
        parts.append("Needs attention: " + "; ".join(attempted[:4]) + ".")
    if reminders:
        local_items = []
        for row in reminders:
            due = datetime.fromisoformat(row["due_at"]).astimezone(ZoneInfo(APP_TIMEZONE))
            local_items.append(f"{row['task']} at {due.strftime('%I:%M %p').lstrip('0')}")
        parts.append("Upcoming reminders: " + "; ".join(local_items) + ".")
    parts.append("What would you like to work on first?")
    return " ".join(parts)


def _twiml_say(text: str, *, gather: bool = True) -> str:
    safe = html.escape(text, quote=False)
    if not gather:
        return f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response><Say language=\"en-IN\">{safe}</Say><Hangup/></Response>"
    action = f"{PUBLIC_BASE_URL}/twilio/turn" if PUBLIC_BASE_URL else "/twilio/turn"
    action = html.escape(action, quote=True)
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response>"
        f"<Gather input=\"speech\" action=\"{action}\" method=\"POST\" "
        "speechTimeout=\"auto\" timeout=\"8\" language=\"en-IN\">"
        f"<Say language=\"en-IN\">{safe}</Say></Gather>"
        "<Say language=\"en-IN\">I didn't catch that. I'll let you get back to your morning. Goodbye.</Say>"
        "<Hangup/></Response>"
    )


def _add_reminder(task: str, minutes: int, call_when_due: bool = False) -> dict[str, Any]:
    task = task.strip()
    if not task or len(task) > 500 or minutes < 1 or minutes > 30 * 24 * 60:
        raise ValueError("A task and a deadline from 1 minute to 30 days are required.")
    reminder_id = str(uuid.uuid4())
    due = _now() + timedelta(minutes=minutes)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO reminders(id, task, due_at, status, notify_by, created_at) VALUES (?, ?, ?, 'pending', ?, ?)",
            (reminder_id, task, due.isoformat(), "call" if call_when_due else "briefing", _now().isoformat()),
        )
    return {
        "id": reminder_id, "task": task, "due_at": due.isoformat(), "status": "pending",
        "notify_by": "phone_call" if call_when_due else "morning_briefing",
    }


def _recent_tasks(hours: int = 24) -> list[dict[str, str]]:
    hours = max(1, min(int(hours), 24 * 30))
    since = (_now() - timedelta(hours=hours)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT task, outcome, occurred_at FROM task_events WHERE occurred_at >= ? ORDER BY occurred_at DESC LIMIT 30",
            (since,),
        ).fetchall()
    return [dict(row) for row in rows]


def _pending_reminders() -> list[dict[str, str]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, task, due_at, notify_by FROM reminders WHERE status = 'pending' ORDER BY due_at LIMIT 30"
        ).fetchall()
    return [dict(row) for row in rows]


_TOOLS = [
    {
        "name": "create_reminder",
        "description": "Create a reminder for the owner. Only use when the owner requested a reminder or deadline.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "What the owner wants to remember."},
                "minutes": {"type": "integer", "description": "Minutes from now, from 1 to 43200."},
                "call_when_due": {"type": "boolean", "description": "True only when the owner explicitly asked for a phone call at the reminder time."},
            },
            "required": ["task", "minutes"],
        },
    },
    {
        "name": "list_recent_tasks",
        "description": "Read task outcomes recorded by the cloud assistant for a recent time range.",
        "input_schema": {
            "type": "object",
            "properties": {"hours": {"type": "integer", "description": "Lookback period, 1 to 720 hours."}},
            "required": [],
        },
    },
    {
        "name": "list_reminders",
        "description": "List the owner's pending cloud reminders.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


def _claude_turn(session_id: str, user_text: str) -> str:
    if not ANTHROPIC_API_KEY:
        return "I can hear you, but my cloud language model isn't connected yet. Once it's configured, I can help with that."
    with _conversation_lock:
        history = _conversations.setdefault(session_id, [])
        history.append({"role": "user", "content": user_text[:2000]})
        history[:] = history[-12:]
        messages: list[dict[str, Any]] = [dict(m) for m in history]

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    system_prompt = (
        "You are a personal assistant speaking to the account owner. Be conversational, concise, and truthful. "
        "Use tools for requested reminders and task-history questions. Do not claim a message was sent, an account was checked, "
        "or a computer task was completed unless a connected tool actually did it. The cloud service currently supports reminders "
        "and its own recorded task history. For ordinary reminders, include them in the morning briefing; "
        "set call_when_due true only when the owner explicitly asked for a phone call at that time. "
        "For other requests, explain what connection or capability is missing. "
        "Ask for confirmation before any external message, post, or non-wakeup phone call. Never ask for passwords or API keys."
    )
    for _ in range(3):
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json={"model": ANTHROPIC_MODEL, "max_tokens": 500, "system": system_prompt, "tools": _TOOLS, "messages": messages},
            timeout=25,
        )
        response.raise_for_status()
        result = response.json()
        content = result.get("content", [])
        calls = [block for block in content if block.get("type") == "tool_use"]
        if not calls:
            answer = " ".join(block.get("text", "") for block in content if block.get("type") == "text").strip()
            with _conversation_lock:
                history.append({"role": "assistant", "content": answer})
                history[:] = history[-12:]
            return answer or "I'm here. What would you like me to do?"

        messages.append({"role": "assistant", "content": content})
        tool_results = []
        for call in calls:
            args = call.get("input", {})
            try:
                if call["name"] == "create_reminder":
                    output = _add_reminder(
                        str(args.get("task", "")), int(args.get("minutes", 0)),
                        bool(args.get("call_when_due", False)),
                    )
                elif call["name"] == "list_recent_tasks":
                    output = _recent_tasks(int(args.get("hours", 24)))
                elif call["name"] == "list_reminders":
                    output = _pending_reminders()
                else:
                    output = {"error": "Tool unavailable."}
                is_error = False
            except Exception as exc:
                output = {"error": str(exc)}
                is_error = True
            tool_results.append({
                "type": "tool_result", "tool_use_id": call["id"],
                "content": json.dumps(output, ensure_ascii=False), "is_error": is_error,
            })
        messages.append({"role": "user", "content": tool_results})

    return "I couldn't finish that response. Please ask me again."


def _twilio_call(body: str) -> str:
    if not all((TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, OWNER_PHONE_NUMBER, PUBLIC_BASE_URL)):
        raise RuntimeError("Twilio and public URL configuration is incomplete.")
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    call = client.calls.create(to=OWNER_PHONE_NUMBER, from_=TWILIO_PHONE_NUMBER, twiml=_twiml_say(body))
    return call.sid


def _morning_call_job() -> None:
    try:
        call_sid = _twilio_call(_morning_briefing())
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO service_state(key, value) VALUES ('last_morning_call', ?)",
                (json.dumps({"sid": call_sid, "at": _now().isoformat()}),),
            )
        log.info("Scheduled morning briefing call started (%s)", call_sid)
    except Exception:
        log.exception("Scheduled morning briefing call could not be started")


def _due_reminder_job() -> None:
    """Call for reminders only when the owner explicitly requested a call."""
    if not all((TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, OWNER_PHONE_NUMBER, PUBLIC_BASE_URL)):
        return
    now = _now().isoformat()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, task FROM reminders WHERE status = 'pending' AND notify_by = 'call' AND due_at <= ? ORDER BY due_at LIMIT 5",
            (now,),
        ).fetchall()
        if not rows:
            return
        reminder_ids = [row["id"] for row in rows]
        prompt = "Your requested reminder is due: " + "; ".join(row["task"] for row in rows) + "."
        try:
            call_sid = _twilio_call(prompt)
        except Exception:
            log.exception("A due reminder call could not be started")
            return
        conn.executemany("UPDATE reminders SET status = 'notified' WHERE id = ?", [(item_id,) for item_id in reminder_ids])
        conn.execute(
            "INSERT OR REPLACE INTO service_state(key, value) VALUES (?, ?)",
            ("reminder_call_" + call_sid, json.dumps({"ids": reminder_ids, "at": now})),
        )


def _valid_twilio_request(request: Request, form: dict[str, str]) -> bool:
    signature = request.headers.get("X-Twilio-Signature", "")
    if not TWILIO_AUTH_TOKEN or not signature or not PUBLIC_BASE_URL:
        return False
    url = PUBLIC_BASE_URL + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    return RequestValidator(TWILIO_AUTH_TOKEN).validate(url, form, signature)


class ChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(default=None, max_length=120)


class TaskEvent(BaseModel):
    task: str = Field(min_length=1, max_length=500)
    outcome: str = Field(pattern="^(completed|failed|needs_attention)$")
    occurred_at: datetime | None = None


class ReminderRequest(BaseModel):
    task: str = Field(min_length=1, max_length=500)
    minutes: int = Field(ge=1, le=43200)
    call_when_due: bool = False


@app.on_event("startup")
def startup() -> None:
    global _scheduler
    _init_db()
    try:
        tz = ZoneInfo(APP_TIMEZONE)
    except Exception as exc:
        raise RuntimeError(f"Invalid ASSISTANT_TIMEZONE: {APP_TIMEZONE}") from exc
    _scheduler = BackgroundScheduler(timezone=tz)
    _scheduler.add_job(_morning_call_job, "cron", hour=6, minute=0, id="morning_briefing", max_instances=1)
    _scheduler.add_job(_due_reminder_job, "interval", minutes=1, id="due_reminders", max_instances=1)
    _scheduler.start()
    log.info("Cloud assistant started; morning call schedule uses %s", APP_TIMEZONE)


@app.on_event("shutdown")
def shutdown() -> None:
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "timezone": APP_TIMEZONE,
        "morning_call_configured": all((TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, OWNER_PHONE_NUMBER, PUBLIC_BASE_URL)),
        "language_model_configured": bool(ANTHROPIC_API_KEY),
    }


@app.post("/api/chat")
def chat(body: ChatRequest, authorization: str | None = Header(default=None)) -> dict[str, str]:
    _require_api_token(authorization)
    session_id = body.session_id or str(uuid.uuid4())
    try:
        reply = _claude_turn(session_id, body.text)
    except requests.RequestException:
        log.exception("Cloud model request failed")
        raise HTTPException(status_code=502, detail="The cloud model request failed.")
    return {"session_id": session_id, "reply": reply}


@app.post("/api/reminders")
def add_reminder(body: ReminderRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _require_api_token(authorization)
    return _add_reminder(body.task, body.minutes, body.call_when_due)


@app.get("/api/reminders")
def list_reminders(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _require_api_token(authorization)
    return {"reminders": _pending_reminders()}


@app.post("/api/task-events", status_code=status.HTTP_201_CREATED)
def record_task_event(body: TaskEvent, authorization: str | None = Header(default=None)) -> dict[str, str]:
    _require_api_token(authorization)
    event_id = str(uuid.uuid4())
    event_time = body.occurred_at or _now()
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)
    occurred_at = event_time.astimezone(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO task_events(id, task, outcome, occurred_at) VALUES (?, ?, ?, ?)",
            (event_id, body.task, body.outcome, occurred_at),
        )
    return {"id": event_id, "status": "recorded"}


@app.get("/api/briefing")
def get_briefing(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _require_api_token(authorization)
    return {"text": _morning_briefing(), "tasks": _recent_tasks(12)}


@app.post("/twilio/turn", response_class=Response)
async def twilio_turn(request: Request) -> Response:
    form = {key: str(value) for key, value in (await request.form()).items()}
    if not _valid_twilio_request(request, form):
        raise HTTPException(status_code=403, detail="Invalid voice webhook signature.")
    caller = form.get("From", "")
    if not OWNER_PHONE_NUMBER or caller != OWNER_PHONE_NUMBER:
        return Response(_twiml_say("This assistant call is restricted to its owner.", gather=False), media_type="application/xml")
    speech = form.get("SpeechResult", "").strip()
    if not speech:
        reply = "I didn't catch that. You can ask about your tasks or set a reminder."
    else:
        try:
            reply = _claude_turn(form.get("CallSid", "morning-call"), speech)
        except Exception:
            log.exception("Voice turn failed")
            reply = "I couldn't complete that request just now. Please try again later."
    return Response(_twiml_say(reply), media_type="application/xml")


