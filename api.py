"""Loopback-only HTTP API for the personal AI terminal application.

Task and calendar writes are delegated to functions in main.py. Run with:
    python api.py
"""

from datetime import datetime, timedelta
import hmac
import ipaddress
import json
import logging
import os
import re
from typing import Optional
from urllib.parse import quote
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field
import uvicorn

import main
import gemini_service
import whatsapp_service
import productivity_service
import gmail_service

HOST = "127.0.0.1"
PORT = 8787
_pending_events: dict[str, dict] = {}
_confirmed_events: dict[str, dict] = {}
logger = logging.getLogger(__name__)

app = FastAPI(title="Personal AI Agent", version="1.0")

@app.on_event("startup")
def connect_calendar_on_startup():
    main.connect_google_calendar(interactive=False)
    gmail_service.start_background_sync()


@app.on_event("shutdown")
def stop_background_services():
    gmail_service.stop_background_sync()


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:1420",
        "http://localhost:1420",
        "tauri://localhost",
        "http://tauri.localhost",
    ],
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.middleware("http")
async def loopback_only(request: Request, call_next):
    peer = request.client.host if request.client else ""
    is_test_client = peer == "testclient"
    try:
        peer_address = ipaddress.ip_address(peer)
        is_loopback = peer_address.is_loopback or bool(
            getattr(peer_address, "ipv4_mapped", None)
            and peer_address.ipv4_mapped.is_loopback
        )
    except ValueError:
        is_loopback = False
    if not (is_loopback or is_test_client):
        return JSONResponse(status_code=403, content={"detail": "This API accepts loopback clients only."})
    public_webhook_paths = {whatsapp_service.WEBHOOK_PATH, whatsapp_service.LEGACY_WEBHOOK_PATH}
    forwarded = any(request.headers.get(header) for header in (
        "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded",
    ))
    if forwarded and not is_test_client:
        if request.url.path not in public_webhook_paths:
            return JSONResponse(status_code=404, content={"detail": "Not found."})
        configured_marker = (os.getenv("WHATSAPP_NGROK_PROXY_TOKEN") or "").strip()
        supplied_marker = request.headers.get("x-planner-ngrok-auth", "")
        if not configured_marker or not hmac.compare_digest(supplied_marker, configured_marker):
            return JSONResponse(status_code=403, content={"detail": "Webhook proxy is not authorized."})
    if request.url.path.startswith("/api/gmail/") and peer != "testclient":
        allowed_origins = {
            "http://127.0.0.1:1420", "http://localhost:1420",
            "tauri://localhost", "http://tauri.localhost",
        }
        origin = request.headers.get("origin", "")
        host = request.headers.get("host", "").lower()
        if origin not in allowed_origins or host not in {"127.0.0.1:8787", "localhost:8787"}:
            return JSONResponse(status_code=403, content={"detail": "Gmail API requests must originate from the local application."})
    return await call_next(request)


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=4000)
    deadline: Optional[str] = None
    scheduled_at: Optional[str] = None
    priority: str = "medium"
    duration_minutes: int = Field(default=60, ge=5, le=1440)
    project_id: Optional[str] = None
    depends_on: list[int] = Field(default_factory=list)


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    start: str
    duration_minutes: int = Field(default=60, ge=5, le=1440)
    description: str = Field(default="", max_length=2000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class TaskUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    description: Optional[str] = Field(default=None, max_length=4000)
    deadline: Optional[str] = None
    scheduled_at: Optional[str] = None
    priority: Optional[str] = None
    duration_minutes: Optional[int] = Field(default=None, ge=5, le=1440)
    project_id: Optional[str] = None
    depends_on: Optional[list[int]] = None


class CalendarEventUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    start: Optional[str] = None
    duration_minutes: Optional[int] = Field(default=None, ge=5, le=1440)
    description: Optional[str] = Field(default=None, max_length=2000)


class InboxCreate(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    source: str = "manual"
    notes: str = Field(default="", max_length=4000)


class InboxUpdate(BaseModel):
    original_text: Optional[str] = Field(default=None, max_length=4000)
    notes: Optional[str] = Field(default=None, max_length=4000)
    suggestion: Optional[dict] = None


class InboxStatusUpdate(BaseModel):
    status: str


class InboxConvert(BaseModel):
    record_type: str
    details: dict = Field(default_factory=dict)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    target_deadline: Optional[str] = None
    start_date: Optional[str] = None
    priority: str = "medium"


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=4000)
    notes: Optional[str] = Field(default=None, max_length=4000)
    target_deadline: Optional[str] = None
    start_date: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None


class MilestoneCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    target_date: Optional[str] = None


class MilestoneUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=4000)
    target_date: Optional[str] = None
    completed: Optional[bool] = None


class TaskProjectUpdate(BaseModel):
    project_id: Optional[str] = None


class NoteCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=20000)
    project_id: Optional[str] = None
    task_id: Optional[int] = None
    tags: list[str] = Field(default_factory=list, max_length=20)


class NoteUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    content: Optional[str] = Field(default=None, min_length=1, max_length=20000)
    project_id: Optional[str] = None
    task_id: Optional[int] = None
    tags: Optional[list[str]] = Field(default=None, max_length=20)


class PlanningSettingsUpdate(BaseModel):
    work_start: Optional[str] = None
    work_end: Optional[str] = None
    working_days: Optional[list[int]] = None
    break_minutes: Optional[int] = None
    break_after_minutes: Optional[int] = None
    buffer_minutes: Optional[int] = None
    reminders_enabled: Optional[bool] = None
    remind_overdue: Optional[bool] = None
    remind_due_today: Optional[bool] = None
    reminder_minutes_before: Optional[int] = None
    widget_enabled: Optional[bool] = None
    widget_always_on_top: Optional[bool] = None


class DailyPlanCreate(BaseModel):
    date: Optional[str] = None


class OverviewBriefingRequest(BaseModel):
    date: Optional[str] = None


class RescheduleBlock(BaseModel):
    start: str


class InsightAccept(BaseModel):
    review_date: str
    expected_start: str


class CapturePreview(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class CaptureSave(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    title: Optional[str] = Field(default=None, max_length=300)
    deadline: Optional[str] = None
    duration_minutes: Optional[int] = Field(default=None, ge=5, le=1440)
    priority: Optional[str] = None
    project_id: Optional[str] = None
    convert_to_task: bool = False


class GmailMessageAction(BaseModel):
    action: str


class GmailDraftRequest(BaseModel):
    to: str = Field(default="", max_length=4000)
    cc: str = Field(default="", max_length=4000)
    subject: str = Field(default="", max_length=998)
    body: str = Field(default="", max_length=100000)
    draft_id: Optional[str] = None
    thread_id: Optional[str] = None
    source_message_id: Optional[str] = None
    mode: str = "compose"


class GmailSendRequest(GmailDraftRequest):
    pass


class GmailTaskSuggestion(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    deadline: Optional[str] = None
    priority: str = "medium"
    duration_minutes: int = Field(default=60, ge=5, le=1440)


class GmailClassificationUpdate(BaseModel):
    categories: Optional[list[str]] = None
    dismissed: bool = False


class GmailSettingsUpdate(BaseModel):
    sync_enabled: Optional[bool] = None
    auto_sync: Optional[bool] = None
    sync_interval_minutes: Optional[int] = None
    classification_enabled: Optional[bool] = None
    notifications_enabled: Optional[bool] = None
    briefing_enabled: Optional[bool] = None


class GeminiModelUpdate(BaseModel):
    model: str = Field(min_length=1, max_length=100)


@app.get("/api/health")
def health():
    calendar = main.calendar_status()
    return {
        "status": "ok",
        "calendar_connected": calendar["connected"],
        "calendar": calendar,
        "timezone": str(main.TIMEZONE),
        "gemini": gemini_service.configuration(),
        "whatsapp": whatsapp_service.configuration_status(),
    }


@app.put("/api/gemini/model")
def update_gemini_model(payload: GeminiModelUpdate):
    try:
        return gemini_service.set_model(payload.model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/gemini/diagnostics")
def diagnose_gemini():
    configuration = gemini_service.configuration()
    if not configuration["configured"]:
        raise HTTPException(
            status_code=503,
            detail="Gemini is not configured. Add GEMINI_API_KEY to the local .env file and restart the backend.",
        )
    try:
        gemini_service.generate_reply(
            "Reply with the single word: ready",
            "",
            [],
        )
    except gemini_service.GeminiError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "connected", "model": configuration["model"]}


@app.get("/api/overview")
def get_daily_overview(date: Optional[str] = None):
    overview = _productivity_call(productivity_service.daily_overview, date)
    overview["email"] = gmail_service.cached_email_overview()
    return overview


@app.post("/api/overview/briefing")
def get_daily_briefing(payload: OverviewBriefingRequest):
    overview = _productivity_call(productivity_service.daily_overview, payload.date)
    overview["email"] = gmail_service.cached_email_overview()
    context = json.dumps(overview, ensure_ascii=True, default=str)[:16000]
    prompt = (
        "Write a concise personal daily briefing from these verified planner records. "
        "Separate confirmed calendar events from proposed task blocks. State when calendar or Gmail data is unavailable. "
        "Do not invent deadlines, events, or completed work. Recommend a realistic next action."
    )
    try:
        briefing = generate_assistant_reply(prompt, "VERIFIED RECORDS:\n" + context, [])
    except gemini_service.GeminiError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"date": overview["date"], "briefing": briefing,
            "calendar_available": overview["calendar_available"],
            "email_last_sync": overview["email"].get("last_sync")}


@app.get("/api/calendar/day/{day_text}")
def get_calendar_day(day_text: str):
    return _productivity_call(productivity_service.daily_overview, day_text)


@app.get("/api/whatsapp/status")
def whatsapp_status():
    return whatsapp_service.configuration_status()


@app.get(whatsapp_service.LEGACY_WEBHOOK_PATH, response_class=PlainTextResponse, include_in_schema=False)
@app.get(whatsapp_service.WEBHOOK_PATH, response_class=PlainTextResponse)
def verify_whatsapp_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    challenge = whatsapp_service.verify_challenge(hub_mode, hub_verify_token, hub_challenge)
    if challenge is None:
        raise HTTPException(status_code=403, detail="Webhook verification failed.")
    return challenge


@app.post(whatsapp_service.LEGACY_WEBHOOK_PATH, include_in_schema=False)
@app.post(whatsapp_service.WEBHOOK_PATH)
async def receive_whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    if not whatsapp_service.enabled():
        raise HTTPException(status_code=503, detail="WhatsApp webhook is disabled.")
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > whatsapp_service.MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="Webhook payload is too large.")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > whatsapp_service.MAX_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="Webhook payload is too large.")
    raw_body = bytes(body)
    if not whatsapp_service.verify_signature(raw_body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    try:
        payload = json.loads(raw_body)
        accepted = whatsapp_service.enqueue_verified_messages(payload, background_tasks)
    except (json.JSONDecodeError, whatsapp_service.InvalidWebhookPayload) as exc:
        raise HTTPException(status_code=400, detail="Malformed webhook payload.") from exc
    except Exception as exc:
        logger.exception("WhatsApp webhook could not be queued (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Webhook could not be queued.") from exc
    return {"received": True, "queued": accepted}


@app.post("/api/calendar/connect")
def calendar_connect():
    if main.connect_google_calendar(interactive=True):
        return {"connected": True}
    raise HTTPException(
        status_code=503,
        detail=main.calendar_connection_error
        or "Google Calendar could not be connected. Check OAuth configuration and reconnect in Settings.",
    )


chat_history: list[dict] = []


def generate_assistant_reply(message: str, context: str, history=None) -> str:
    if gemini_service.configuration()["configured"]:
        return gemini_service.generate_reply(message, context, history or [])
    raise gemini_service.GeminiError(
        "Gemini is not configured. Add GEMINI_API_KEY to the local .env file and restart the backend."
    )


def _execute_chat_action(action: dict) -> tuple[str, dict | None]:
    action_type = action["type"]
    if action_type == "none":
        return "", None
    if action_type == "generate_plan":
        proposal = _productivity_call(productivity_service.generate_daily_plan, action.get("date") or None)
        task_blocks = [block for block in proposal.get("blocks", []) if block.get("kind") == "task"]
        reply = (
            f"I drafted a plan for {proposal['date']} with {len(task_blocks)} task blocks "
            f"({proposal.get('scheduled_minutes', 0)} minutes of work). Review it in Daily Planner; "
            "nothing has been added to Google Calendar."
        )
        return reply, {
            "type": "review_plan",
            "proposal_id": proposal["id"],
            "date": proposal["date"],
        }
    if action_type == "propose_event":
        if main.calendar_service is None:
            return main.calendar_connection_error or "Google Calendar is disconnected. Reconnect in Settings.", None
        try:
            title = str(action.get("title", "")).strip()
            day = datetime.strptime(str(action.get("date", "")), "%Y-%m-%d").date()
            duration = int(action.get("duration_minutes", 60))
        except (TypeError, ValueError):
            return "Please provide an event title, date, and valid duration so I can prepare it.", None
        if not title or not 5 <= duration <= 1440:
            return "The event needs a title and a duration between 5 minutes and 24 hours.", None
        end_of_day = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=main.TIMEZONE)
        day_start = datetime.combine(day, datetime.min.time(), tzinfo=main.TIMEZONE)
        if day_start >= end_of_day or day < main.now_local().date():
            return "That date is in the past. Choose a future date and I can prepare the event.", None
        existing = get_events(start=day_start.isoformat(), end=end_of_day.isoformat())["events"]
        busy_events = list(existing)
        for task in main.load_tasks():
            if task.get("status") == "completed" or not task.get("scheduled_at"):
                continue
            try:
                task_start = datetime.fromisoformat(task["scheduled_at"].replace("Z", "+00:00"))
                if task_start.tzinfo is None:
                    task_start = task_start.replace(tzinfo=main.TIMEZONE)
                task_end = task_start + timedelta(minutes=int(task.get("duration_minutes", 60)))
            except (TypeError, ValueError):
                continue
            busy_events.append({
                "summary": f"Scheduled task: {task.get('title', 'Untitled')}",
                "start": task_start.isoformat(),
                "end": task_end.isoformat(),
            })

        start_time_value = str(action.get("start_time", "")).strip()
        if start_time_value:
            try:
                start_time = datetime.strptime(start_time_value, "%H:%M").time()
            except ValueError:
                return "Please provide a valid start time in CAT.", None
            start = datetime.combine(day, start_time, tzinfo=main.TIMEZONE)
            end = start + timedelta(minutes=duration)
            conflicts = main.find_conflicts(start, end, busy_events)
            if conflicts:
                return f"That time overlaps {', '.join(conflicts)}. Choose another time; nothing was added.", None
        else:
            preference = str(action.get("time_preference", "")).strip().lower()
            preferred_windows = {
                "morning": (datetime.strptime("06:00", "%H:%M").time(), datetime.strptime("12:00", "%H:%M").time()),
                "afternoon": (datetime.strptime("12:00", "%H:%M").time(), datetime.strptime("17:00", "%H:%M").time()),
                "evening": (datetime.strptime("17:00", "%H:%M").time(), datetime.strptime("22:00", "%H:%M").time()),
                "anytime": (None, None),
            }
            if preference not in preferred_windows:
                return "What time should I use? You can give a start time or say morning, afternoon, or evening.", None
            settings = productivity_service.load_state()["settings"]
            free_windows = productivity_service._free_windows(day, settings, busy_events)
            preferred_start, preferred_end = preferred_windows[preference]
            start = None
            end = None
            for window_start, window_end in free_windows:
                candidate_start = window_start
                candidate_end = window_end
                if preferred_start:
                    candidate_start = max(candidate_start, datetime.combine(day, preferred_start, tzinfo=main.TIMEZONE))
                    candidate_end = min(candidate_end, datetime.combine(day, preferred_end, tzinfo=main.TIMEZONE))
                if candidate_end - candidate_start >= timedelta(minutes=duration):
                    start = candidate_start
                    end = start + timedelta(minutes=duration)
                    break
            if start is None or end is None:
                return (
                    f"I couldn't find a conflict-free {preference} slot on {day.isoformat()} "
                    "that fits your working hours and task schedule. Choose another time or date.",
                    None,
                )
        if start < main.now_local():
            return "That start time is in the past. Choose a future time and I can prepare the event.", None
        description = str(action.get("description", ""))[:2000]
        preview = propose_event(EventCreate(
            title=title,
            start=start.isoformat(),
            duration_minutes=duration,
            description=description,
        ))
        return (
            f"Please review this event before adding it: {title} on "
            f"{start.strftime('%A, %d %B at %H:%M')} CAT ({duration} minutes)."
        ), {
            "type": "confirm_event",
            "pending_id": preview["pending_id"],
            "event": preview["event"],
        }
    if action_type == "add_task":
        title = str(action.get("title", "")).strip()
        if not title:
            return "What task would you like me to add?", None
        try:
            duration = int(action.get("duration_minutes", 60))
        except (TypeError, ValueError):
            return "Please give the task a valid estimated duration.", None
        if not 5 <= duration <= 1440:
            return "Task duration must be between 5 minutes and 24 hours.", None
        task, error = main.add_task(
            title,
            action.get("deadline") or None,
            str(action.get("priority", "medium")),
            duration,
        )
        if error:
            return str(error), None
        return f"Added task [{task['id']}] {task['title']}.", None
    if action_type == "complete_task":
        task_ref = str(action.get("task_ref", "")).strip()
        if not task_ref:
            return "Which task would you like me to complete?", None
        task, error = main.complete_task(task_ref)
        if error:
            return str(error), None
        return f"Marked task [{task['id']}] {task['title']} complete.", None
    if action_type == "create_project":
        name = str(action.get("project_name", "")).strip()
        if not name:
            return "What should I name the project?", None
        try:
            project = productivity_service.create_project(name)
        except ValueError as exc:
            return str(exc), None
        return f"Created the project {project['name']}.", None
    if action_type in {"add_project_milestone", "list_project_tasks"}:
        project_ref = str(action.get("project_ref", "")).strip()
        if not project_ref:
            return "Which project do you mean?", None
        projects = productivity_service._project_matches(project_ref)
        if len(projects) > 1:
            return "Which project did you mean? " + "; ".join(project["name"] for project in projects[:5]), None
        if not projects:
            return "I couldn't find that project. Please give me its project name.", None
        project = projects[0]
        if action_type == "list_project_tasks":
            tasks = [
                task for task in main.load_tasks()
                if task.get("project_id") == project["id"] and task.get("status") != "completed"
            ]
            if not tasks:
                return f"No outstanding tasks are recorded for {project['name']}.", None
            return (
                f"Outstanding tasks in {project['name']}: "
                + "; ".join(task["title"] for task in tasks),
                None,
            )
        title = str(action.get("milestone_title", "")).strip()
        if not title:
            return "What should I call the milestone?", None
        try:
            milestone = productivity_service.create_milestone(project["id"], title)
        except ValueError as exc:
            return str(exc), None
        return f"Added the milestone {milestone['title']} to {project['name']}.", None
    return "I couldn't complete that planner action.", None


_WEEKDAY_NAMES = (
    "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
)
_MONTH_NAMES = (
    "january|february|march|april|may|june|july|august|september|october|"
    "november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec"
)
_EVENT_ACTION_RE = re.compile(
    r"\b(?:add|create|schedule|book|put|make)\b.{0,50}\b(event|meeting|appointment)\b",
    re.IGNORECASE,
)


def _event_date_from_text(text: str) -> str | None:
    patterns = (
        r"\b20\d{2}-\d{1,2}-\d{1,2}\b",
        rf"\b(?:{_WEEKDAY_NAMES}\s*,?\s*)?\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{_MONTH_NAMES})(?:,?\s+20\d{{2}})?\b",
        rf"\b(?:{_WEEKDAY_NAMES}\s*,?\s*)?(?:{_MONTH_NAMES})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+20\d{{2}})?\b",
        r"\b(?:day after tomorrow|tomorrow|today)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        value = match.group(0).strip()
        value = re.sub(rf"^(?:{_WEEKDAY_NAMES})\s*,?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", value, flags=re.IGNORECASE)
        value = re.sub(r",", " ", value)
        parsed = main.parse_date(value)
        if parsed:
            return parsed
    return None


def _event_time_from_text(text: str):
    patterns = (
        r"\b\d{1,2}(?::[0-5]\d)?\s*[ap]\.?\s*m\.?\b",
        r"\b(?:at|start(?:\s+time)?(?:\s+is)?|time\s+is)\s*(\d{1,2}:[0-5]\d)(?![\w-])",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = match.group(1) if match.lastindex else match.group(0)
            parsed = main.parse_time(value)
            if parsed:
                return parsed
    return None


def _event_title_from_text(text: str) -> str | None:
    title_match = re.search(
        r"\b(?:called|named)\s+(.+?)(?=\s+(?:on|at|for)\b|[,.!?;]|$)",
        text,
        re.IGNORECASE,
    )
    if title_match:
        title = title_match.group(1).strip(" \t.,;:-")
        if title:
            return title[:300]

    title_match = re.search(
        r"\b(?:event|meeting|appointment)\s+(?:title|name)\s+(?:is|:)\s*(.+?)\s*[.!?]*$",
        text,
        re.IGNORECASE,
    )
    if title_match:
        title = title_match.group(1).strip(" \t.,;:-")
        if title:
            return title[:300]

    title_match = re.search(
        r"\bfor\s+(?:a|an|the)\s+(.+?)\s+(?=(?:at|on)\s+)",
        text,
        re.IGNORECASE,
    )
    if title_match:
        title = title_match.group(1).strip(" \t.,;:-")
        if title:
            return title[:300]
    return None


def _parse_explicit_event_action(message: str, history: list[dict]) -> dict | None:
    source_texts = [message]
    continuation = False
    if history and history[-1].get("role") == "assistant":
        continuation = bool(re.search(
            r"provide the event title|event title, date, and start time",
            str(history[-1].get("content", "")),
            re.IGNORECASE,
        ))
    if continuation:
        previous_request = next(
            (str(item.get("content", "")) for item in reversed(history[:-1])
             if item.get("role") == "user"),
            "",
        )
        if _EVENT_ACTION_RE.search(previous_request):
            source_texts.append(previous_request)

    if not _EVENT_ACTION_RE.search(message) and not continuation:
        return None
    combined_text = "\n".join(source_texts)
    title = next((value for value in (_event_title_from_text(text) for text in source_texts) if value), None)
    event_date = next((value for value in (_event_date_from_text(text) for text in source_texts) if value), None)
    start_time = next((value for value in (_event_time_from_text(text) for text in source_texts) if value), None)
    if not title or not event_date or not start_time:
        return None
    duration_match = re.search(
        r"\b(\d{1,4})\s*(minutes?|mins?|hours?|hrs?)\b", combined_text, re.IGNORECASE
    )
    duration = 60
    if duration_match:
        amount = int(duration_match.group(1))
        duration = amount * 60 if duration_match.group(2).lower().startswith(("hour", "hr")) else amount
    return {
        "type": "propose_event",
        "title": title,
        "date": event_date,
        "start_time": start_time.strftime("%H:%M"),
        "duration_minutes": duration,
    }


@app.post("/api/chat")
def chat(payload: ChatRequest):
    global chat_history
    message = payload.message.strip()
    _, saved_context = productivity_service.desktop_chat_context(message)
    task_context = "CURRENT PENDING TASKS:\n" + main.format_tasks(False)
    calendar_context = "CALENDAR: " + (main.tool_get_calendar_events(14) if hasattr(main, "tool_get_calendar_events") else "Calendar status unavailable")
    context = (
        f"CURRENT LOCAL DATE/TIME: {main.now_local().isoformat()} CAT\n"
        + task_context
        + "\n"
        + calendar_context
    )
    if saved_context:
        context += "\nRETRIEVED PROJECT AND NOTE RECORDS:\n" + saved_context

    try:
        decision = gemini_service.generate_agent_command(message, context, chat_history[-12:])
        action_reply, chat_action = _execute_chat_action(decision["action"])
        reply = action_reply or decision["reply"]
    except gemini_service.GeminiError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Planner chat action failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail="The planner action could not be completed.") from exc

    chat_history.append({"role": "user", "content": message})
    chat_history.append({"role": "assistant", "content": reply})
    chat_history = chat_history[-24:]
    action_taken = decision["action"].get("type") != "none"
    return {"reply": reply, "action_taken": action_taken, "action": chat_action}


@app.get("/api/tasks")
def get_tasks(include_completed: bool = False):
    return {"tasks": [t for t in main.load_tasks() if include_completed or t.get("status") != "completed"]}


@app.post("/api/tasks", status_code=201)
def add_task(payload: TaskCreate):
    if payload.project_id and not productivity_service._find_record("projects", payload.project_id):
        raise HTTPException(status_code=404, detail="Project not found.")
    task, error = main.add_task(
        payload.title, payload.deadline, payload.priority, payload.duration_minutes,
        payload.project_id, payload.depends_on, description=payload.description,
        scheduled_at=payload.scheduled_at,
    )
    if error:
        raise HTTPException(status_code=400, detail=error)
    return task


@app.patch("/api/tasks/{task_id}")
def update_task(task_id: int, payload: TaskUpdate):
    updates = payload.model_dump(exclude_unset=True)
    if updates.get("project_id") and not productivity_service._find_record("projects", updates["project_id"]):
        raise HTTPException(status_code=404, detail="Project not found.")
    task, error = main.update_task(task_id, updates)
    if error:
        raise HTTPException(status_code=404 if "not found" in error.lower() else 400, detail=error)
    return task


@app.delete("/api/tasks/{task_id}", status_code=200)
def delete_task(task_id: int):
    task, error = main.delete_task(task_id)
    if error:
        raise HTTPException(status_code=404, detail=error)
    return task


@app.patch("/api/tasks/{task_id}/complete")
def complete_task(task_id: int):
    task, error = main.complete_task(task_id)
    if error:
        raise HTTPException(status_code=404 if "find" in error.lower() else 400, detail=error)
    return task


@app.get("/api/events")
def get_events(days: int = 14, start: Optional[str] = None, end: Optional[str] = None):
    if main.calendar_service is None:
        raise HTTPException(
            status_code=503,
            detail=main.calendar_connection_error or "Google Calendar is disconnected. Reconnect in Settings.",
        )
    if bool(start) != bool(end):
        raise HTTPException(status_code=400, detail="Provide both start and end date-times.")
    if start and end:
        try:
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="start and end must be ISO 8601 date-times")
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=main.TIMEZONE)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=main.TIMEZONE)
        if end_dt <= start_dt:
            raise HTTPException(status_code=400, detail="end must be later than start.")
        try:
            response = main.calendar_service.events().list(
                calendarId="primary", timeMin=start_dt.isoformat(), timeMax=end_dt.isoformat(),
                maxResults=250, singleEvents=True, orderBy="startTime"
            )
            response = main._execute_calendar_request(response)
            events = []
            while True:
                for item in response.get("items", []):
                    start_info = item.get("start", {})
                    start_text = start_info.get("dateTime") or start_info.get("date")
                    if start_text:
                        events.append({
                            "id": item.get("id"), "summary": item.get("summary", "(Untitled event)"),
                            "start": start_text,
                            "end": item.get("end", {}).get("dateTime") or item.get("end", {}).get("date"),
                            "description": item.get("description", ""),
                            "location": item.get("location", ""),
                            "htmlLink": item.get("htmlLink", ""),
                        })
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
                response = main.calendar_service.events().list(
                    calendarId="primary", timeMin=start_dt.isoformat(), timeMax=end_dt.isoformat(),
                    maxResults=250, singleEvents=True, orderBy="startTime", pageToken=page_token
                )
                response = main._execute_calendar_request(response)
            return {"events": events}
        except Exception as exc:
            detail = main.record_calendar_failure(exc)
            raise HTTPException(status_code=502, detail=detail) from exc
    events = main.list_calendar_events(days)
    if events is None:
        raise HTTPException(
            status_code=502,
            detail=main.calendar_connection_error or "Google Calendar could not be reached. Check your connection and retry.",
        )
    return {"events": events}


@app.post("/api/events", status_code=201)
def create_event(payload: EventCreate):
    if main.calendar_service is None:
        raise HTTPException(
            status_code=503,
            detail=main.calendar_connection_error or "Google Calendar is disconnected. Reconnect in Settings.",
        )
    try:
        start = datetime.fromisoformat(payload.start.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail="Start must be an ISO 8601 date-time.")
    if start.tzinfo is None:
        start = start.replace(tzinfo=main.TIMEZONE)
    if start < main.now_local():
        raise HTTPException(status_code=400, detail="The event cannot start in the past.")
    success, result = main.create_calendar_event(
        payload.title, start, start + timedelta(minutes=payload.duration_minutes), payload.description
    )
    if not success:
        raise HTTPException(status_code=502, detail=result)
    return {"created": True, "event": result}


@app.post("/api/events/propose", status_code=202)
def propose_event(payload: EventCreate):
    if main.calendar_service is None:
        raise HTTPException(
            status_code=503,
            detail=main.calendar_connection_error or "Google Calendar is disconnected. Reconnect in Settings.",
        )
    try:
        start = datetime.fromisoformat(payload.start.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail="Start must be an ISO 8601 date-time.")
    if start.tzinfo is None:
        start = start.replace(tzinfo=main.TIMEZONE)
    if start < main.now_local():
        raise HTTPException(status_code=400, detail="The event cannot start in the past.")
    end = start + timedelta(minutes=payload.duration_minutes)
    conflicts = main.find_conflicts(start, end, get_events(
        start=start.isoformat(), end=end.isoformat()
    )["events"])
    if conflicts:
        raise HTTPException(
            status_code=409,
            detail=f"That time overlaps {', '.join(conflicts)}. Choose another time; nothing was added.",
        )
    pending_id = uuid4().hex
    _pending_events[pending_id] = {
        "title": payload.title,
        "start": start,
        "end": end,
        "description": payload.description,
    }
    return {
        "pending_id": pending_id,
        "requires_confirmation": True,
        "event": {"title": payload.title, "start": start.isoformat(), "end": end.isoformat()},
    }


@app.patch("/api/events/{event_id}")
def update_event(event_id: str, payload: CalendarEventUpdate):
    start = None
    end = None
    if payload.start:
        try:
            start = datetime.fromisoformat(payload.start.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Start must be ISO 8601.")
        if start.tzinfo is None:
            start = start.replace(tzinfo=main.TIMEZONE)
        end = start + timedelta(minutes=payload.duration_minutes or 60)
    success, result = main.update_calendar_event(
        event_id, payload.title, start, end, payload.description
    )
    if not success:
        raise HTTPException(status_code=502, detail=result)
    return result


@app.delete("/api/events/{event_id}")
def remove_event(event_id: str):
    success, result = main.delete_calendar_event(event_id)
    if not success:
        raise HTTPException(status_code=502, detail=result)
    return {"deleted": True, "result": result}


@app.post("/api/events/{pending_id}/confirm")
def confirm_event(pending_id: str):
    if pending_id in _confirmed_events:
        return _confirmed_events[pending_id]
    pending = _pending_events.get(pending_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="Pending event not found.")
    if pending["start"] < main.now_local():
        raise HTTPException(status_code=409, detail="The proposed event is no longer in the future.")
    if main.calendar_service is None:
        raise HTTPException(
            status_code=503,
            detail=main.calendar_connection_error or "Google Calendar is disconnected. Reconnect in Settings.",
        )
    day_start = datetime.combine(pending["start"].date(), datetime.min.time(), tzinfo=main.TIMEZONE)
    day_end = day_start + timedelta(days=1)
    conflicts = main.find_conflicts(
        pending["start"],
        pending["end"],
        get_events(start=day_start.isoformat(), end=day_end.isoformat())["events"],
    )
    if conflicts:
        raise HTTPException(
            status_code=409,
            detail=f"Calendar availability changed: the event now overlaps {', '.join(conflicts)}. Review a new time.",
        )
    success, result = main.create_calendar_event(
        pending["title"],
        pending["start"],
        pending["end"],
        pending["description"],
        idempotency_key=f"chat-proposal:{pending_id}",
    )
    if not success:
        raise HTTPException(status_code=502, detail=result)
    response = {"created": True, "event": result}
    _pending_events.pop(pending_id, None)
    _confirmed_events[pending_id] = response
    while len(_confirmed_events) > 100:
        _confirmed_events.pop(next(iter(_confirmed_events)))
    return response


@app.post("/api/events/{pending_id}/cancel")
def cancel_event_proposal(pending_id: str):
    if _pending_events.pop(pending_id, None) is None:
        raise HTTPException(status_code=404, detail="Pending event not found.")
    return {"cancelled": True}


def _productivity_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/inbox")
def get_inbox(status: Optional[str] = None):
    return {"items": _productivity_call(productivity_service.list_inbox, status)}


@app.post("/api/inbox", status_code=201)
def add_inbox_item(payload: InboxCreate):
    return _productivity_call(
        productivity_service.create_inbox_item, payload.text, payload.source, None, payload.notes
    )


@app.patch("/api/inbox/{item_id}")
def edit_inbox_item(item_id: str, payload: InboxUpdate):
    return _productivity_call(productivity_service.update_inbox_item, item_id,
                              payload.model_dump(exclude_unset=True))


@app.post("/api/inbox/{item_id}/status")
def change_inbox_status(item_id: str, payload: InboxStatusUpdate):
    return _productivity_call(productivity_service.set_inbox_status, item_id, payload.status)


@app.post("/api/inbox/{item_id}/convert")
def convert_inbox_item(item_id: str, payload: InboxConvert):
    return _productivity_call(productivity_service.convert_inbox_item, item_id,
                              payload.record_type, payload.details)


@app.get("/api/projects")
def get_projects():
    return {"projects": productivity_service.list_projects()}


@app.post("/api/projects", status_code=201)
def add_project(payload: ProjectCreate):
    return _productivity_call(productivity_service.create_project, payload.name, payload.description,
                              payload.target_deadline, payload.priority, payload.start_date)


@app.patch("/api/projects/{project_id}")
def edit_project(project_id: str, payload: ProjectUpdate):
    return _productivity_call(productivity_service.update_project, project_id,
                              payload.model_dump(exclude_unset=True))


@app.get("/api/projects/{project_id}/tasks")
def get_project_tasks(project_id: str):
    if not productivity_service._find_record("projects", project_id):
        raise HTTPException(status_code=404, detail="Project not found.")
    return {"tasks": [task for task in main.load_tasks() if task.get("project_id") == project_id]}


@app.post("/api/projects/{project_id}/milestones", status_code=201)
def add_milestone(project_id: str, payload: MilestoneCreate):
    return _productivity_call(productivity_service.create_milestone, project_id, payload.title,
                              payload.description, payload.target_date)


@app.patch("/api/milestones/{milestone_id}")
def edit_milestone(milestone_id: str, payload: MilestoneUpdate):
    return _productivity_call(productivity_service.update_milestone, milestone_id,
                              payload.model_dump(exclude_unset=True))


@app.delete("/api/milestones/{milestone_id}")
def remove_milestone(milestone_id: str):
    return _productivity_call(productivity_service.delete_milestone, milestone_id)


@app.patch("/api/tasks/{task_id}/project")
def set_task_project(task_id: int, payload: TaskProjectUpdate):
    return _productivity_call(productivity_service.link_task_to_project, task_id, payload.project_id)


@app.get("/api/notes")
def get_notes(q: Optional[str] = None):
    return {"notes": productivity_service.search_notes(q) if q else productivity_service.load_state()["notes"]}


@app.post("/api/notes", status_code=201)
def add_note(payload: NoteCreate):
    return _productivity_call(productivity_service.create_note, payload.title, payload.content,
                              payload.project_id, payload.task_id, payload.tags)


@app.patch("/api/notes/{note_id}")
def edit_note(note_id: str, payload: NoteUpdate):
    return _productivity_call(productivity_service.update_note, note_id,
                              payload.model_dump(exclude_unset=True))


@app.delete("/api/notes/{note_id}")
def remove_note(note_id: str):
    return _productivity_call(productivity_service.delete_note, note_id)


@app.get("/api/planner/settings")
def get_planner_settings():
    return productivity_service.load_state()["settings"]


@app.put("/api/planner/settings")
def edit_planner_settings(payload: PlanningSettingsUpdate):
    return _productivity_call(productivity_service.update_planning_settings,
                              payload.model_dump(exclude_unset=True))


@app.post("/api/planner/daily", status_code=201)
def create_daily_plan(payload: DailyPlanCreate):
    return _productivity_call(productivity_service.generate_daily_plan, payload.date)


@app.get("/api/planner/proposals")
def get_schedule_proposals():
    return {"proposals": productivity_service.list_proposals()}


@app.post("/api/planner/proposals/{proposal_id}/approve")
def approve_schedule(proposal_id: str):
    return _productivity_call(productivity_service.approve_proposal, proposal_id)


@app.post("/api/planner/proposals/{proposal_id}/reject")
def reject_schedule(proposal_id: str):
    return _productivity_call(productivity_service.reject_proposal, proposal_id)


@app.post("/api/planner/proposals/{proposal_id}/commit")
def commit_schedule(proposal_id: str):
    if main.calendar_service is None:
        raise HTTPException(status_code=503, detail="Google Calendar is not connected.")
    result = _productivity_call(productivity_service.commit_proposal_to_calendar, proposal_id)
    if any(not item.get("created") for item in result.get("results", [])):
        raise HTTPException(
            status_code=502,
            detail="Google Calendar did not confirm every block. Successfully created blocks were saved; retry to continue with the remaining blocks.",
        )
    return result


@app.post("/api/planner/proposals/{proposal_id}/blocks/{block_id}/complete")
def complete_scheduled_task(proposal_id: str, block_id: str):
    return _productivity_call(productivity_service.complete_proposed_task, proposal_id, block_id)


@app.patch("/api/planner/proposals/{proposal_id}/blocks/{block_id}")
def reschedule_task_block(proposal_id: str, block_id: str, payload: RescheduleBlock):
    return _productivity_call(productivity_service.reschedule_proposed_task,
                              proposal_id, block_id, payload.start)


@app.get("/api/weekly-review")
def get_weekly_review(date: Optional[str] = None):
    return _productivity_call(productivity_service.weekly_review, date)


@app.get("/api/insights")
def get_workload_insights(date: Optional[str] = None):
    return {"insights": _productivity_call(productivity_service.workload_insights, date)}


@app.post("/api/insights/{insight_id}/dismiss")
def dismiss_workload_insight(insight_id: str):
    return productivity_service.dismiss_insight(insight_id)


@app.post("/api/insights/{insight_id}/accept")
def accept_workload_insight(insight_id: str, payload: InsightAccept):
    return _productivity_call(
        productivity_service.accept_workload_insight,
        insight_id,
        payload.review_date,
        payload.expected_start,
    )


@app.post("/api/capture/preview")
def preview_quick_capture(payload: CapturePreview):
    return _productivity_call(productivity_service.extract_capture, payload.text)


@app.post("/api/capture", status_code=201)
def save_quick_capture(payload: CaptureSave):
    suggestion = _productivity_call(productivity_service.extract_capture, payload.text)
    details = {
        "title": payload.title or suggestion["title"],
        "deadline": payload.deadline if payload.deadline is not None else suggestion["deadline"],
        "duration_minutes": payload.duration_minutes or suggestion["duration_minutes"],
        "priority": payload.priority or suggestion["priority"],
        "project_id": payload.project_id,
    }
    item = _productivity_call(productivity_service.create_inbox_item, payload.text, "quick_capture")
    item["suggestion"] = details
    _productivity_call(productivity_service.update_inbox_item, item["id"], {"suggestion": details})
    if payload.convert_to_task:
        return _productivity_call(productivity_service.convert_inbox_item, item["id"], "task", details)
    return item


def _gmail_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except gmail_service.GmailNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except gmail_service.GmailError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/gmail/status")
def get_gmail_status():
    return gmail_service.connection_status()


@app.post("/api/gmail/connect")
def connect_gmail():
    return _gmail_call(gmail_service.connect_gmail)


@app.post("/api/gmail/disconnect")
def disconnect_gmail(clear_cache: bool = False):
    return _gmail_call(gmail_service.disconnect_gmail, clear_cache)


@app.post("/api/gmail/cache/clear")
def clear_gmail_cache():
    return _gmail_call(gmail_service.clear_email_cache)


@app.get("/api/gmail/labels")
def get_gmail_labels():
    return {"labels": _gmail_call(gmail_service.list_labels)}


@app.get("/api/gmail/messages")
def get_gmail_messages(folder: str = "inbox", page_token: Optional[str] = None,
                       q: str = "", category: Optional[str] = None, page_size: int = 30,
                       gmail_category: Optional[str] = None):
    if gmail_category and gmail_category not in gmail_service.GMAIL_CATEGORIES:
        raise HTTPException(status_code=400, detail="Unsupported Gmail inbox category.")
    result = _gmail_call(gmail_service.list_messages, folder, page_token, q, page_size,
                         gmail_category=gmail_category)
    if category:
        if category not in gmail_service.CATEGORIES:
            raise HTTPException(status_code=400, detail="Unsupported email category.")
        result["messages"] = [message for message in result["messages"]
                              if message.get("classification") and
                              category in message["classification"].get("categories", [])]
    return result


@app.get("/api/gmail/messages/{message_id}")
def get_gmail_message(message_id: str):
    return _gmail_call(gmail_service.get_message, message_id)


@app.get("/api/gmail/threads/{thread_id}")
def get_gmail_thread(thread_id: str):
    return _gmail_call(gmail_service.get_thread, thread_id)


@app.get("/api/gmail/messages/{message_id}/attachment/{attachment_id}")
def download_gmail_attachment(message_id: str, attachment_id: str):
    content, filename, media_type = _gmail_call(gmail_service.get_attachment, message_id, attachment_id)
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
               "X-Content-Type-Options": "nosniff"}
    return Response(content=content, media_type=media_type, headers=headers)


@app.post("/api/gmail/messages/{message_id}/action")
def gmail_message_action(message_id: str, payload: GmailMessageAction):
    return _gmail_call(gmail_service.modify_message, message_id, payload.action)


@app.post("/api/gmail/messages/{message_id}/classify")
def classify_gmail_message(message_id: str, force: bool = False):
    return _gmail_call(gmail_service.classify_message, message_id, force)


@app.patch("/api/gmail/messages/{message_id}/classification")
def update_gmail_classification(message_id: str, payload: GmailClassificationUpdate):
    return _gmail_call(gmail_service.update_classification, message_id, payload.categories, payload.dismissed)


@app.post("/api/gmail/messages/{message_id}/summary")
def summarize_gmail_message(message_id: str):
    return {"summary": _gmail_call(gmail_service.summarize_email, message_id)}


@app.post("/api/gmail/threads/{thread_id}/summary")
def summarize_gmail_thread(thread_id: str):
    return {"summary": _gmail_call(gmail_service.summarize_thread, thread_id)}


@app.post("/api/gmail/messages/{message_id}/reply-draft")
def draft_gmail_reply(message_id: str, payload: dict):
    return _gmail_call(gmail_service.draft_ai_reply, message_id,
                       str(payload.get("tone", "professional")), str(payload.get("instructions", "")),
                       str(payload.get("mode", "reply")))


@app.post("/api/gmail/messages/{message_id}/reply")
def prepare_gmail_reply(message_id: str, payload: dict):
    return _gmail_call(gmail_service.prepare_reply, message_id, str(payload.get("mode", "reply")))


@app.post("/api/gmail/messages/{message_id}/forward")
def prepare_gmail_forward(message_id: str):
    message = _gmail_call(gmail_service.get_message, message_id)
    return {"to": "", "cc": "", "subject": f"Fwd: {message['subject']}",
            "body": f"\n\n---------- Forwarded message ----------\nFrom: {message['from']}\nDate: {message['date']}\n"
                    f"Subject: {message['subject']}\n\n{message['body_text']}",
            "source_message_id": message_id, "thread_id": None}


@app.post("/api/gmail/drafts")
def save_gmail_draft(payload: GmailDraftRequest):
    reply_to = _gmail_call(gmail_service.get_message, payload.source_message_id) if payload.source_message_id else None
    return _gmail_call(gmail_service.create_draft, payload.to, payload.cc, payload.subject, payload.body,
                       payload.draft_id, payload.thread_id, reply_to)


@app.post("/api/gmail/send")
def send_gmail_message(payload: GmailSendRequest):
    reply_to = _gmail_call(gmail_service.get_message, payload.source_message_id) if payload.source_message_id else None
    return _gmail_call(gmail_service.send_message, payload.to, payload.cc, payload.subject, payload.body,
                       payload.thread_id, reply_to, payload.draft_id)


@app.post("/api/gmail/messages/{message_id}/task")
def create_task_from_gmail(message_id: str, payload: GmailTaskSuggestion):
    return _gmail_call(gmail_service.create_task_from_email, message_id, payload.title,
                       payload.deadline, payload.priority, payload.duration_minutes)


@app.get("/api/gmail/messages/{message_id}/calendar-suggestion")
def gmail_calendar_suggestion(message_id: str):
    classification = _gmail_call(gmail_service.classify_message, message_id)
    return {"source_message_id": message_id,
            "meeting_details": classification.get("meeting_details", []),
            "explicit_deadlines": classification.get("explicit_deadlines", []),
            "requires_user_review": True,
            "calendar_modified": False}


@app.post("/api/gmail/sync", status_code=202)
def sync_gmail(background_tasks: BackgroundTasks):
    if not gmail_service.connection_status()["connected"]:
        raise HTTPException(status_code=503, detail="Connect Gmail before synchronizing.")
    background_tasks.add_task(gmail_service.synchronize)
    return {"accepted": True, "message": "Gmail synchronization started."}


@app.get("/api/gmail/briefing")
def get_gmail_briefing(days: int = 1):
    return _gmail_call(gmail_service.daily_briefing, days)


@app.patch("/api/gmail/settings")
def update_gmail_settings(payload: GmailSettingsUpdate):
    return _gmail_call(gmail_service.update_settings, payload.model_dump(exclude_unset=True))


def run():
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        access_log=False,
        log_level="warning",
        proxy_headers=False,
        forwarded_allow_ips="127.0.0.1,::1",
    )


if __name__ == "__main__":
    run()
