"""
Personal AI Planner and Task Manager
------------------------------------
A local planner using Ollama, Google Calendar, and a JSON task store.

Existing credentials.json, token.json, and tasks.json are preserved.
Timezone: Africa/Harare

Run:
    python main.py

Useful commands:
    /tasks                 Pending tasks
    /alltasks              All tasks
    /addtask               Add a task using a guided prompt
    /complete <id>         Complete a task by ID
    /events [days]         Show upcoming calendar events
    /plan [days]           Build a study/work plan using tasks + calendar
    /addevent              Create a calendar event using a guided prompt
    /help                  Show commands
    /exit                  Exit
"""

import base64
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from dotenv import load_dotenv


# ------------------------------ Configuration ------------------------------

MODEL = "qwen3:4b"
OLLAMA_URL = "http://localhost:11434/api/chat"
TIMEZONE = ZoneInfo("Africa/Harare")

BASE_DIR = Path(os.environ.get("PERSONAL_AI_AGENT_DATA_DIR", Path(__file__).resolve().parent))
load_dotenv(BASE_DIR / ".env")
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"
TASKS_FILE = BASE_DIR / "tasks.json"

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
MAX_HISTORY_MESSAGES = 12
calendar_service = None
conversation = []
_task_lock = threading.RLock()
logger = logging.getLogger(__name__)
calendar_credentials = None
calendar_connection_error = None
_calendar_lock = threading.RLock()
_calendar_saved_token = None


# --------------------------------- Dates ------------------------------------

def now_local():
    return datetime.now(TIMEZONE)


def parse_date(value):
    """Parse a date from common human formats; return YYYY-MM-DD or None."""
    if not value:
        return None

    value = value.strip().lower()
    today = now_local().date()

    if value in {"today", "tod"}:
        return today.isoformat()
    if value in {"tomorrow", "tmrw", "tmr"}:
        return (today + timedelta(days=1)).isoformat()
    if value == "day after tomorrow":
        return (today + timedelta(days=2)).isoformat()

    # ISO format
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        pass

    # Day/month/year or day-month-year
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y",
                "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue

    # "21 October" / "Oct 21" assumes the next occurrence.
    for fmt in ("%d %B", "%d %b", "%B %d", "%b %d"):
        try:
            parsed = datetime.strptime(value, fmt).date().replace(year=today.year)
            if parsed < today:
                parsed = parsed.replace(year=today.year + 1)
            return parsed.isoformat()
        except ValueError:
            continue

    return None


def pretty_date(iso_value):
    try:
        return date.fromisoformat(iso_value).strftime("%a, %d %b %Y")
    except (TypeError, ValueError):
        return str(iso_value or "No deadline")


def parse_time(value):
    """Parse times like 7pm, 7:30 PM, or 19:30 into a time."""
    value = value.strip().lower().replace(".", "")
    for fmt in ("%I:%M%p", "%I%p", "%H:%M", "%H"):
        try:
            return datetime.strptime(value.replace(" ", ""), fmt).time()
        except ValueError:
            continue
    return None


# ----------------------------- Google Calendar -----------------------------

def connect_google_calendar(interactive=True):
    global calendar_service, calendar_credentials, calendar_connection_error, _calendar_saved_token
    with _calendar_lock:
        calendar_service = None
        calendar_credentials = None
        calendar_connection_error = None
        _calendar_saved_token = None

    if not CREDENTIALS_FILE.exists():
        calendar_connection_error = (
            f"Google Calendar OAuth credentials were not found at {CREDENTIALS_FILE}. "
            "Place your Google OAuth desktop-client credentials.json there, then choose Reconnect calendar."
        )
        logger.warning("Google Calendar OAuth credentials are not present in the configured data directory.")
        return False

    credentials = None
    if TOKEN_FILE.exists():
        try:
            credentials = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        except Exception as exc:
            logger.warning("Could not read the Google Calendar token (%s).", type(exc).__name__)

    if credentials and not credentials.has_scopes(SCOPES):
        logger.info("Saved Google Calendar authorization does not include the required events scope.")
        credentials = None

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            _save_calendar_credentials(credentials)
        except Exception as exc:
            calendar_connection_error = _calendar_failure_message(exc)
            logger.warning("Could not refresh the Google Calendar token (%s).", type(exc).__name__)
            credentials = None

    if not credentials or not credentials.valid:
        if not interactive:
            if calendar_connection_error is None:
                calendar_connection_error = (
                    "Google Calendar authorization is missing, expired, or needs additional permission. "
                    "Choose Reconnect calendar in Settings."
                )
            return False
        try:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            credentials = flow.run_local_server(port=0)
            _save_calendar_credentials(credentials)
        except Exception as exc:
            calendar_connection_error = _calendar_failure_message(exc)
            logger.warning("Google Calendar sign-in failed (%s).", type(exc).__name__)
            return False

    try:
        service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
        service.events().list(
            calendarId="primary",
            timeMin=now_local().isoformat(),
            maxResults=1,
        ).execute()
        with _calendar_lock:
            calendar_credentials = credentials
            _calendar_saved_token = credentials.token
            calendar_service = service
            calendar_connection_error = None
        return True
    except Exception as exc:
        calendar_connection_error = _calendar_failure_message(exc)
        logger.warning("Could not verify Google Calendar access (%s).", type(exc).__name__)
        return False


def _save_calendar_credentials(credentials):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=TOKEN_FILE.parent,
            prefix=".token-",
            suffix=".tmp",
            delete=False,
        ) as token_file:
            token_file.write(credentials.to_json())
            temporary_path = Path(token_file.name)
        os.replace(temporary_path, TOKEN_FILE)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _persist_refreshed_calendar_credentials():
    global _calendar_saved_token
    with _calendar_lock:
        credentials = calendar_credentials
        if not credentials or not credentials.valid or credentials.token == _calendar_saved_token:
            return
        try:
            _save_calendar_credentials(credentials)
        except OSError as exc:
            logger.warning("Could not persist refreshed Google Calendar authorization (%s).", type(exc).__name__)
            return
        _calendar_saved_token = credentials.token


def _calendar_failure_message(exc):
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status in {401, 403}:
        return (
            f"Google Calendar rejected authorization (HTTP {status}). Reconnect calendar in Settings "
            "to renew permission for calendar events."
        )
    if isinstance(status, int):
        return f"Google Calendar request failed (HTTP {status}). Check your connection and try again."
    return (
        f"Google Calendar could not be reached ({type(exc).__name__}). Check your internet connection "
        "and try again; reconnect calendar if the problem continues."
    )


def record_calendar_failure(exc):
    global calendar_service, calendar_connection_error
    message = _calendar_failure_message(exc)
    with _calendar_lock:
        calendar_connection_error = message
        if getattr(getattr(exc, "resp", None), "status", None) in {401, 403}:
            calendar_service = None
    return message


def calendar_status():
    return {
        "connected": calendar_service is not None,
        "credentials_present": CREDENTIALS_FILE.is_file(),
        "authorization_present": TOKEN_FILE.is_file(),
        "credentials_location": str(CREDENTIALS_FILE),
        "error": calendar_connection_error,
    }


def _execute_calendar_request(request):
    global calendar_connection_error
    result = request.execute()
    _persist_refreshed_calendar_credentials()
    calendar_connection_error = None
    return result


def _event_idempotency_id(title, start_dt, end_dt, description, idempotency_key=None):
    if idempotency_key:
        identity = idempotency_key
    else:
        identity = json.dumps(
            {
                "title": title.strip(),
                "start": start_dt.astimezone(TIMEZONE).isoformat(),
                "end": end_dt.astimezone(TIMEZONE).isoformat(),
                "description": description,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return base64.b32hexencode(hashlib.sha256(identity.encode("utf-8")).digest()).decode("ascii").rstrip("=").lower()


def _calendar_event_matches(event, title, start_dt, end_dt, description):
    if event.get("summary", "") != title or event.get("description", "") != description:
        return False
    try:
        actual_start = datetime.fromisoformat(event["start"]["dateTime"].replace("Z", "+00:00"))
        actual_end = datetime.fromisoformat(event["end"]["dateTime"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, TypeError, ValueError):
        return False
    return actual_start == start_dt and actual_end == end_dt


def list_calendar_events(days=14):
    if calendar_service is None:
        return None

    start = now_local()
    end = start + timedelta(days=max(1, min(int(days), 365)))

    try:
        request = calendar_service.events().list(
            calendarId="primary", timeMin=start.isoformat(), timeMax=end.isoformat(),
            maxResults=250, singleEvents=True, orderBy="startTime",
        )
        events = []
        while request is not None:
            response = _execute_calendar_request(request)
            for item in response.get("items", []):
                start_info = item.get("start", {})
                start_text = start_info.get("dateTime") or start_info.get("date")
                if not start_text:
                    continue
                try:
                    if "T" in start_text:
                        parsed = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=TIMEZONE)
                        display = parsed.astimezone(TIMEZONE).strftime("%a %d %b, %I:%M %p CAT")
                    else:
                        display = date.fromisoformat(start_text).strftime("%a %d %b (all day)")
                except ValueError:
                    display = start_text
                events.append({
                    "id": item.get("id"),
                    "summary": item.get("summary", "(Untitled event)"),
                    "start": start_text,
                    "display_start": display,
                    "end": item.get("end", {}).get("dateTime") or item.get("end", {}).get("date"),
                    "description": item.get("description", ""),
                    "location": item.get("location", ""),
                    "htmlLink": item.get("htmlLink"),
                })
            page_token = response.get("nextPageToken")
            request = calendar_service.events().list(
                calendarId="primary",
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                maxResults=250,
                singleEvents=True,
                orderBy="startTime",
                pageToken=page_token,
            ) if page_token else None
        return events
    except Exception as exc:
        record_calendar_failure(exc)
        logger.warning("Could not fetch Google Calendar events (%s).", type(exc).__name__)
        return None


def update_calendar_event(event_id, title=None, start_dt=None, end_dt=None, description=None):
    if calendar_service is None:
        return False, "Google Calendar is not connected."
    try:
        existing = _execute_calendar_request(calendar_service.events().get(calendarId="primary", eventId=event_id))
        if title is not None:
            existing["summary"] = title
        if description is not None:
            existing["description"] = description
        if start_dt is not None:
            existing["start"] = {"dateTime": start_dt.isoformat(), "timeZone": str(TIMEZONE)}
        if end_dt is not None:
            existing["end"] = {"dateTime": end_dt.isoformat(), "timeZone": str(TIMEZONE)}
        updated = _execute_calendar_request(
            calendar_service.events().update(calendarId="primary", eventId=event_id, body=existing)
        )
        return True, updated
    except Exception as exc:
        return False, record_calendar_failure(exc)


def delete_calendar_event(event_id):
    if calendar_service is None:
        return False, "Google Calendar is not connected."
    try:
        _execute_calendar_request(calendar_service.events().delete(calendarId="primary", eventId=event_id))
        return True, "Event deleted."
    except Exception as exc:
        return False, record_calendar_failure(exc)


def create_calendar_event(title, start_dt, end_dt, description="", idempotency_key=None):
    if calendar_service is None:
        return False, calendar_connection_error or "Google Calendar is disconnected. Reconnect calendar in Settings."

    body = {
        "summary": title,
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": str(TIMEZONE)},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": str(TIMEZONE)},
    }
    event_id = _event_idempotency_id(title, start_dt, end_dt, description, idempotency_key)
    body["id"] = event_id
    try:
        created = _execute_calendar_request(
            calendar_service.events().insert(calendarId="primary", body=body)
        )
    except Exception as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status != 409:
            return False, record_calendar_failure(exc)
        try:
            created = _execute_calendar_request(
                calendar_service.events().get(calendarId="primary", eventId=event_id)
            )
        except Exception as lookup_error:
            return False, record_calendar_failure(lookup_error)
        if not _calendar_event_matches(created, title, start_dt, end_dt, description):
            return False, "Google Calendar already has this request ID for different event details."

    if not isinstance(created, dict) or not created.get("id"):
        try:
            created = _execute_calendar_request(
                calendar_service.events().get(calendarId="primary", eventId=event_id)
            )
        except Exception as exc:
            return False, (
                "Google Calendar returned no verifiable event ID. "
                f"The event could not be confirmed ({record_calendar_failure(exc)})."
            )
    if (
        not isinstance(created, dict)
        or not created.get("id")
        or not _calendar_event_matches(created, title, start_dt, end_dt, description)
    ):
        return False, "Google Calendar did not return a verifiable event matching the requested details."
    event = {
        "id": created["id"],
        "summary": created.get("summary", title),
        "start": created.get("start", {}).get("dateTime"),
        "end": created.get("end", {}).get("dateTime"),
    }
    if created.get("htmlLink"):
        event["htmlLink"] = created["htmlLink"]
    return True, event


# ---------------------------------- Tasks -----------------------------------

def load_tasks():
    if not TASKS_FILE.exists():
        return []
    try:
        data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read the task file (%s).", type(exc).__name__)
        return []


def save_tasks(tasks):
    with _task_lock:
        TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = TASKS_FILE.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(tasks, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(TASKS_FILE)


def next_task_id(tasks):
    return max((int(task.get("id", 0)) for task in tasks), default=0) + 1


def add_task(title, deadline=None, priority="medium", duration_minutes=60, project_id=None, depends_on=None,
             source_email_id=None, description="", scheduled_at=None):
    with _task_lock:
        title = title.strip()
        if not title:
            return None, "Task title cannot be empty."

        priority = priority.lower()
        if priority not in {"low", "medium", "high"}:
            priority = "medium"

        if deadline:
            parsed = parse_date(deadline)
            if parsed is None:
                return None, "I couldn't understand that deadline. Try YYYY-MM-DD or '21 October 2026'."
            deadline = parsed
        if scheduled_at:
            try:
                scheduled = datetime.fromisoformat(str(scheduled_at).replace("Z", "+00:00"))
            except ValueError:
                return None, "Scheduled time must be an ISO 8601 date-time."
            if scheduled.tzinfo is None:
                scheduled = scheduled.replace(tzinfo=TIMEZONE)
            scheduled_at = scheduled.isoformat()

        try:
            duration_minutes = max(5, min(int(duration_minutes), 1440))
        except (TypeError, ValueError):
            duration_minutes = 60

        tasks = load_tasks()
        try:
            dependencies = list(dict.fromkeys(int(item) for item in (depends_on or [])))
        except (TypeError, ValueError):
            return None, "Dependencies must be valid task IDs."
        known_ids = {int(item.get("id", -1)) for item in tasks}
        if any(item not in known_ids for item in dependencies):
            return None, "A dependency task was not found."
        task = {
            "id": next_task_id(tasks),
            "title": title,
            "description": str(description or "").strip()[:4000],
            "deadline": deadline,
            "scheduled_at": scheduled_at,
            "priority": priority,
            "duration_minutes": duration_minutes,
            "project_id": project_id,
            "depends_on": dependencies,
            "source_email_id": source_email_id,
            "status": "pending",
            "created_at": now_local().isoformat(),
            "completed_at": None,
        }
        tasks.append(task)
        save_tasks(tasks)
        return task, None


def find_task(task_ref, include_completed=True):
    tasks = load_tasks()
    ref = str(task_ref).strip().lower()

    if ref.isdigit():
        for task in tasks:
            if int(task.get("id", -1)) == int(ref):
                if include_completed or task.get("status") != "completed":
                    return task
        return None

    candidates = []
    for task in tasks:
        if not include_completed and task.get("status") == "completed":
            continue
        title = task.get("title", "").lower()
        if ref and (ref in title or title in ref):
            candidates.append(task)

    return candidates[0] if len(candidates) == 1 else None


def complete_task(task_ref):
    with _task_lock:
        tasks = load_tasks()
        ref = str(task_ref).strip().lower()
        match = None

        if ref.isdigit():
            match = next((t for t in tasks if int(t.get("id", -1)) == int(ref)), None)
        else:
            matches = [t for t in tasks if ref and ref in t.get("title", "").lower()
                       and t.get("status") != "completed"]
            if len(matches) == 1:
                match = matches[0]
            elif len(matches) > 1:
                return None, "More than one task matches. Use its task ID."

        if not match:
            return None, "I couldn't find that task. Use /tasks to see task IDs."

        if match.get("status") == "completed":
            return match, "That task is already marked complete."

        match["status"] = "completed"
        match["completed_at"] = now_local().isoformat()
        save_tasks(tasks)
        return match, None


def update_task(task_id, updates):
    with _task_lock:
        tasks = load_tasks()
        task = next((item for item in tasks if int(item.get("id", -1)) == int(task_id)), None)
        if task is None:
            return None, "Task not found."
        if "title" in updates:
            title = str(updates["title"]).strip()
            if not title:
                return None, "Task title cannot be empty."
            task["title"] = title
        if "description" in updates:
            task["description"] = str(updates["description"] or "").strip()[:4000]
        if "deadline" in updates:
            deadline = updates["deadline"]
            parsed = parse_date(deadline) if deadline else None
            if deadline and parsed is None:
                return None, "Invalid deadline."
            task["deadline"] = parsed
        if "scheduled_at" in updates:
            scheduled_at = updates["scheduled_at"]
            if scheduled_at:
                try:
                    scheduled = datetime.fromisoformat(str(scheduled_at).replace("Z", "+00:00"))
                except ValueError:
                    return None, "Scheduled time must be an ISO 8601 date-time."
                if scheduled.tzinfo is None:
                    scheduled = scheduled.replace(tzinfo=TIMEZONE)
                task["scheduled_at"] = scheduled.isoformat()
            else:
                task["scheduled_at"] = None
        if "priority" in updates:
            priority = str(updates["priority"]).lower()
            if priority not in {"low", "medium", "high"}:
                return None, "Priority must be low, medium, or high."
            task["priority"] = priority
        if "duration_minutes" in updates:
            try:
                duration = int(updates["duration_minutes"])
            except (TypeError, ValueError):
                return None, "Duration must be a number of minutes."
            if not 5 <= duration <= 1440:
                return None, "Duration must be between 5 and 1440 minutes."
            task["duration_minutes"] = duration
        if "project_id" in updates:
            task["project_id"] = updates["project_id"] or None
        if "depends_on" in updates:
            dependencies = updates["depends_on"]
            if not isinstance(dependencies, list):
                return None, "Dependencies must be a list of task IDs."
            try:
                dependencies = list(dict.fromkeys(int(item) for item in dependencies))
            except (TypeError, ValueError):
                return None, "Dependencies must be valid task IDs."
            if int(task_id) in dependencies:
                return None, "A task cannot depend on itself."
            known_ids = {int(item.get("id", -1)) for item in tasks}
            if any(item not in known_ids for item in dependencies):
                return None, "A dependency task was not found."
            task["depends_on"] = dependencies
        task["updated_at"] = now_local().isoformat()
        save_tasks(tasks)
        return task, None


def delete_task(task_id):
    with _task_lock:
        tasks = load_tasks()
        task = next((item for item in tasks if int(item.get("id", -1)) == int(task_id)), None)
        if task is None:
            return None, "Task not found."
        tasks.remove(task)
        save_tasks(tasks)
        return task, None


def format_tasks(include_completed=False):
    tasks = load_tasks()
    if not include_completed:
        tasks = [t for t in tasks if t.get("status") != "completed"]

    if not tasks:
        return "No tasks to show."

    tasks.sort(key=lambda t: (
        t.get("status") == "completed",
        t.get("deadline") or "9999-12-31",
        {"high": 0, "medium": 1, "low": 2}.get(t.get("priority", "medium"), 1),
    ))

    lines = []
    for task in tasks:
        status = "✓" if task.get("status") == "completed" else "○"
        deadline = pretty_date(task.get("deadline")) if task.get("deadline") else "No deadline"
        duration = task.get("duration_minutes", 60)
        lines.append(
            f"{status} [{task.get('id')}] {task.get('title', 'Untitled')} "
            f"| {task.get('priority', 'medium').upper()} | {deadline} | {duration} min"
        )
    return "\n".join(lines)


# --------------------------- Planning context ------------------------------

def build_planning_context(days=7):
    days = max(1, min(int(days), 30))
    today = now_local().date()
    end_date = today + timedelta(days=days - 1)

    tasks = load_tasks()
    pending = [t for t in tasks if t.get("status") != "completed"]
    pending.sort(key=lambda t: (
        t.get("deadline") or "9999-12-31",
        {"high": 0, "medium": 1, "low": 2}.get(t.get("priority", "medium"), 1),
    ))

    events = list_calendar_events(days)
    lines = [
        f"Current local date/time: {now_local().strftime('%A, %d %B %Y, %I:%M %p')} (Africa/Harare).",
        f"Planning period: {today.isoformat()} through {end_date.isoformat()} ({days} day(s)).",
        "",
        "PENDING TASKS:",
    ]

    if pending:
        for task in pending:
            due = task.get("deadline") or "no deadline"
            lines.append(
                f"- ID {task.get('id')}: {task.get('title')} | priority {task.get('priority', 'medium')} "
                f"| due {due} | estimated effort {task.get('duration_minutes', 60)} minutes"
            )
    else:
        lines.append("- None.")

    lines.extend(["", "EXISTING CALENDAR EVENTS:"])
    if events is None:
        lines.append("- Calendar unavailable. Do not assume the calendar is empty.")
    elif events:
        for event in events:
            lines.append(f"- {event['display_start']}: {event['summary']}")
    else:
        lines.append("- No events found in this period.")

    lines.extend([
        "",
        "Planning rules:",
        "- Make a realistic plan with breaks and buffer time.",
        "- Prioritize tasks with nearer deadlines and higher priority.",
        "- Do not schedule work over existing calendar events.",
        "- Do not claim suggested work blocks are calendar events; they are recommendations only.",
        "- If the user has not supplied their available hours, state a reasonable assumption and invite correction.",
        "- Avoid placing a long task into a tiny time slot; split it into manageable blocks.",
        "- Be concise, practical, and specific with dates and times.",
    ])
    return "\n".join(lines)


# ---------------------------- Ollama chat ----------------------------------

def strip_thinking(text):
    """Remove Qwen thinking blocks if the model includes them in its content."""
    if not text:
        return ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<\|[^|]+\|>", "", text)
    return text.strip()


def call_ollama(messages, max_tokens=1800):
    # Qwen3 can spend most of its generation budget on internal reasoning.
    # Add its /no_think directive to request a direct, concise response.
    request_messages = [dict(message) for message in messages]
    if request_messages and request_messages[-1].get("role") == "user":
        request_messages[-1]["content"] = request_messages[-1].get("content", "") + "\n/no_think"

    payload = {
        "model": MODEL,
        "messages": request_messages,
        "stream": False,
        "options": {
            "temperature": 0.25,
            "num_predict": min(max_tokens, 900),
        },
    }
    request = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            result = json.loads(response.read().decode("utf-8"))
        message = result.get("message", {})
        content = strip_thinking(message.get("content", ""))
        # Some versions/models return a separate thinking field. Deliberately ignore it.
        return content, None
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        return "", f"Ollama returned HTTP {exc.code}: {detail[:500]}"
    except urllib.error.URLError as exc:
        return "", (
            f"Couldn't reach Ollama at {OLLAMA_URL}. Make sure the Ollama app/server is running. "
            f"Details: {exc}"
        )
    except TimeoutError:
        return "", "Ollama took too long to respond. Try again or use a shorter request."
    except Exception as exc:
        return "", f"Unexpected Ollama error: {exc}"


def ask_ai(user_message, planning=False, days=7):
    system_prompt = (
        "You are a practical personal planning assistant running locally for the user. "
        "Use clear, concise language. Never claim you created or changed a task or calendar event "
        "unless the application explicitly confirms it. If you lack information, say so rather than "
        "inventing it. Keep the user's local timezone in mind: Africa/Harare."
    )

    messages = [{"role": "system", "content": system_prompt}]
    if planning:
        context = build_planning_context(days)
        messages.append({
            "role": "system",
            "content": "Use this current task and calendar snapshot as factual context:\n\n" + context,
        })
    else:
        messages.extend(conversation[-MAX_HISTORY_MESSAGES:])

    messages.append({"role": "user", "content": user_message})
    answer, error = call_ollama(messages)

    if error:
        return f"WARNING: {error}"

    if not answer:
        # Retry once with a plain, direct instruction. This prevents silent blank replies.
        retry_messages = [
            {"role": "system", "content": system_prompt + " Do not output hidden reasoning. Reply with the answer only."},
            {"role": "user", "content": user_message if not planning else
             "Create a practical plan from the task/calendar context below. Reply with the plan only.\n\n"
             + build_planning_context(days)},
        ]
        answer, retry_error = call_ollama(retry_messages, max_tokens=2200)
        if retry_error:
            return f"WARNING: {retry_error}"
        if not answer:
            return (
                "I didn't receive a usable answer from the model. Your task and calendar data are safe. "
                "Try again with a shorter prompt, or check the Ollama terminal for errors."
            )

    if not planning:
        conversation.append({"role": "user", "content": user_message})
        conversation.append({"role": "assistant", "content": answer})
        del conversation[:-MAX_HISTORY_MESSAGES]

    return answer


# --------------------------- Natural-language intents ---------------------

def natural_task_add(message):
    """Best-effort parsing for common 'add a task' requests."""
    lowered = message.lower()
    if not re.search(r"\b(add|create|save|track|make)\b", lowered) or not re.search(
        r"\b(task|todo|to-do)\b", lowered
    ):
        return None

    priority = "high" if re.search(r"\b(high|urgent|important)\b", lowered) else (
        "low" if re.search(r"\blow\b", lowered) else "medium"
    )

    duration = 60
    duration_match = re.search(r"\b(\d{1,4})\s*(minutes?|mins?|hours?|hrs?)\b", lowered)
    if duration_match:
        amount = int(duration_match.group(1))
        unit = duration_match.group(2)
        duration = amount * 60 if unit.startswith(("hour", "hr")) else amount

    deadline = None
    # Match ISO or numeric date, then common "by/on 21 October 2026" forms.
    date_match = re.search(r"\b(20\d{2}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b", message)
    if date_match:
        deadline = parse_date(date_match.group(1))
    else:
        natural_match = re.search(
            r"\b(?:by|before|due(?:\s+on)?|deadline(?:\s+of)?|on)\s+"
            r"((?:\d{1,2}\s+)?(?:January|February|March|April|May|June|July|August|September|October|November|December)"
            r"(?:\s+\d{4})?|(?:tomorrow|today|day after tomorrow))\b",
            message, flags=re.IGNORECASE
        )
        if natural_match:
            deadline = parse_date(natural_match.group(1))

    title = message
    title = re.sub(r"^\s*(please\s+)?(add|create|save|track|make)\s+(?:me\s+)?(?:a\s+)?(?:new\s+)?(?:high[- ]priority\s+|low[- ]priority\s+)?(?:task|todo|to-do)\s*", "", title, flags=re.I)
    title = re.sub(r"\b(?:with\s+)?(?:a\s+)?(?:high|medium|low)\s+priority\b", "", title, flags=re.I)
    title = re.sub(r"\b(?:high|medium|low)[- ]priority\b", "", title, flags=re.I)
    title = re.sub(r"\b(?:estimated\s+)?(?:for\s+)?\d{1,4}\s*(?:minutes?|mins?|hours?|hrs?)\b", "", title, flags=re.I)
    title = re.sub(r"\b(?:by|before|due(?:\s+on)?|deadline(?:\s+of)?|on)\s+(?:20\d{2}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|(?:\d{1,2}\s+)?(?:January|February|March|April|May|June|July|August|September|October|November|December)(?:\s+\d{4})?|tomorrow|today|day after tomorrow)\b", "", title, flags=re.I)
    title = re.sub(r"\b(?:task|todo|to-do)\b", "", title, flags=re.I)
    title = re.sub(r"\b(?:priority|urgent|important)\b", "", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip(" .,:;-")
    title = re.sub(r"^(?:to|for|about)\s+", "", title, flags=re.I)

    if len(title) < 3:
        return None

    task, error = add_task(title, deadline, priority, duration)
    if error:
        return f"WARNING: {error}"
    return (
        f"OK: Task added: [{task['id']}] {task['title']}\n"
        f"Priority: {task['priority']}\n"
        f"Deadline: {pretty_date(task['deadline']) if task['deadline'] else 'No deadline'}\n"
        f"Estimated effort: {task['duration_minutes']} minutes"
    )


def natural_task_complete(message):
    # An instruction to add a task may contain words like "finish"; do not
    # mistake "add a task to finish X" for completing an existing task.
    if re.search(r"\b(add|create|save|track|make)\b", message, re.I) and re.search(
        r"\b(task|todo|to-do)\b", message, re.I
    ):
        return None
    if not re.search(r"\b(complete|completed|finish|finished|done|mark)\b", message, re.I):
        return None
    if not re.search(r"\b(task|todo|to-do)\b|\b(?:as\s+)?done\b", message, re.I):
        return None

    id_match = re.search(r"\b(?:task\s*)?#?(\d+)\b", message, re.I)
    if id_match:
        ref = id_match.group(1)
    else:
        title_match = re.search(
            r"\b(?:complete|finish|mark)\s+(?:the\s+)?(?:task\s+)?(.+?)(?:\s+as\s+done)?[.!?]*$",
            message, re.I
        )
        if not title_match:
            return None
        ref = title_match.group(1).strip(" .")
        ref = re.sub(r"\b(task|todo|to-do)\b", "", ref, flags=re.I).strip()

    task, error = complete_task(ref)
    if error:
        return f"WARNING: {error}"
    return f"OK: Completed task [{task['id']}]: {task['title']}"


# ----------------------------- Guided prompts ------------------------------

def guided_add_task():
    print("\nAdd a task")
    title = input("Task title: ").strip()
    if not title:
        print("Cancelled: title is required.")
        return

    deadline_input = input("Deadline (YYYY-MM-DD, e.g. 21 October 2026, or blank): ").strip()
    deadline = parse_date(deadline_input) if deadline_input else None
    if deadline_input and not deadline:
        print("I couldn't parse that deadline. Task not added.")
        return

    priority = input("Priority [low/medium/high] (default medium): ").strip().lower() or "medium"
    if priority not in {"low", "medium", "high"}:
        print("Invalid priority; using medium.")
        priority = "medium"

    duration_input = input("Estimated minutes (default 60): ").strip()
    try:
        duration = int(duration_input) if duration_input else 60
    except ValueError:
        duration = 60

    task, error = add_task(title, deadline, priority, duration)
    if error:
        print(f"WARNING: {error}")
    else:
        print(f"OK: Added [{task['id']}] {task['title']}.")


def guided_add_event():
    if calendar_service is None:
        print("WARNING: Google Calendar isn't connected.")
        return

    print("\nCreate a calendar event")
    title = input("Event title: ").strip()
    if not title:
        print("Cancelled: title is required.")
        return

    date_input = input("Date (YYYY-MM-DD): ").strip()
    parsed_date = parse_date(date_input)
    if not parsed_date:
        print("I couldn't parse that date. Event not created.")
        return

    start_input = input("Start time (e.g. 7:00 PM or 19:00): ").strip()
    start_time = parse_time(start_input)
    if not start_time:
        print("I couldn't parse that start time. Event not created.")
        return

    duration_input = input("Duration in minutes (default 60): ").strip()
    try:
        duration = int(duration_input) if duration_input else 60
        duration = max(5, min(duration, 1440))
    except ValueError:
        duration = 60

    event_date = date.fromisoformat(parsed_date)
    start_dt = datetime.combine(event_date, start_time, tzinfo=TIMEZONE)
    end_dt = start_dt + timedelta(minutes=duration)

    print("\nPlease confirm:")
    print(f"  {title}")
    print(f"  {start_dt.strftime('%A, %d %B %Y, %I:%M %p')}–{end_dt.strftime('%I:%M %p')}")
    confirm = input("Create this event? [y/N]: ").strip().lower()
    if confirm not in {"y", "yes"}:
        print("Cancelled. No event was created.")
        return

    success, result = create_calendar_event(title, start_dt, end_dt)
    if success:
        print(f"OK: Calendar event created: {result}")
    else:
        print(f"WARNING: {result}")


# --------------------------------- Commands ---------------------------------

def show_events(days=14):
    if calendar_service is None:
        print("WARNING: Google Calendar isn't connected.")
        return

    events = list_calendar_events(days)
    if events is None:
        print("WARNING: Couldn't load calendar events.")
    elif not events:
        print(f"No events found in the next {days} day(s).")
    else:
        print(f"\nCalendar events for the next {days} day(s):")
        for event in events:
            print(f"  • {event['display_start']} — {event['summary']}")


def show_help():
    print("""
Commands:
  /tasks                 Show pending tasks
  /alltasks              Show all tasks, including completed ones
  /addtask               Add a task using a guided prompt
  /complete <id or name> Mark a task complete
  /events [days]         Show upcoming Google Calendar events (default 14 days)
  /plan [days]           Generate a plan using tasks + calendar (default 7 days)
  /addevent              Create a Google Calendar event with confirmation
  /help                  Show this help
  /exit                  Quit

You can also use natural language, for example:
  Add a high-priority task to finish AquaVision by 21 October 2026, estimated 3 hours.
  Complete task 1.
  Plan my next 3 days around my deadlines and calendar.
  Help me break down my deep learning assignment.

Task management commands run locally; AI planning and chat use Ollama.
""")


def handle_command(message):
    """Return a string if handled as a command, otherwise None."""
    stripped = message.strip()
    lower = stripped.lower()

    if lower == "/tasks":
        return format_tasks(False)
    if lower == "/alltasks":
        return format_tasks(True)
    if lower == "/addtask":
        guided_add_task()
        return "Finished adding task."
    if lower.startswith("/complete"):
        ref = stripped[len("/complete"):].strip()
        if not ref:
            return "Usage: /complete <task ID or unique part of its title>"
        task, error = complete_task(ref)
        return f"WARNING: {error}" if error else f"OK: Completed task [{task['id']}]: {task['title']}"
    if lower.startswith("/events"):
        match = re.search(r"/events\s+(\d+)", lower)
        show_events(int(match.group(1)) if match else 14)
        return "Calendar lookup finished."
    if lower.startswith("/plan"):
        match = re.search(r"/plan\s+(\d+)", lower)
        days = int(match.group(1)) if match else 7
        return ask_ai(
            f"Create a practical plan for the next {days} days using the provided task and calendar context.",
            planning=True,
            days=days,
        )
    if lower == "/addevent":
        guided_add_event()
        return "Calendar event workflow finished."
    if lower == "/help":
        show_help()
        return "Help displayed."
    return None


# ------------------------- Agent (tool calling) ----------------------------
# The model can call these tools to read your tasks/calendar and to add events.
# Calendar writes always show a preview and ask for y/N confirmation first.

MAX_TOOL_ROUNDS = 6
MAX_AGENT_MESSAGES = 30
agent_history = []

TOOLS = [
    {"type": "function", "function": {
        "name": "get_calendar_events",
        "description": "List the user's upcoming Google Calendar events with start and end times.",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "How many days ahead to look (default 7)"}}}}},
    {"type": "function", "function": {
        "name": "get_tasks",
        "description": "List the user's pending tasks with IDs, deadlines, priorities and estimated minutes.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "add_task",
        "description": "Add a task to the user's task list.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "deadline": {"type": "string", "description": "YYYY-MM-DD, optional"},
            "priority": {"type": "string", "enum": ["low", "medium", "high"]},
            "duration_minutes": {"type": "integer"}},
            "required": ["title"]}}},
    {"type": "function", "function": {
        "name": "complete_task",
        "description": "Mark a task as completed, by its ID or a unique part of its title.",
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string"}}, "required": ["task"]}}},
    {"type": "function", "function": {
        "name": "schedule_events",
        "description": (
            "Add one or more events to the user's Google Calendar. The user is shown the list "
            "and must confirm before anything is saved. Call get_calendar_events first to avoid double-booking."
        ),
        "parameters": {"type": "object", "properties": {
            "events": {"type": "array", "items": {"type": "object", "properties": {
                "title": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "start_time": {"type": "string", "description": "24-hour HH:MM"},
                "duration_minutes": {"type": "integer"},
                "description": {"type": "string"}},
                "required": ["title", "date", "start_time", "duration_minutes"]}}},
            "required": ["events"]}}},
]


def _parse_event_dt(text):
    if not text or "T" not in text:
        return None  # all-day events don't block time slots
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=TIMEZONE)


def find_conflicts(start_dt, end_dt, existing):
    clashes = []
    for event in existing:
        s, f = _parse_event_dt(event["start"]), _parse_event_dt(event["end"])
        if s and f and s < end_dt and f > start_dt:
            clashes.append(event["summary"])
    return clashes


def tool_get_calendar_events(days=7):
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 7
    events = list_calendar_events(days)
    if events is None:
        return "Calendar unavailable. Do not assume it is empty."
    if not events:
        return f"No events in the next {days} day(s)."
    return "\n".join(f"- {e['start']} to {e['end']}: {e['summary']}" for e in events)


def tool_schedule_events(events):
    if calendar_service is None:
        return "Error: Google Calendar is not connected."
    if not isinstance(events, list) or not events:
        return "Error: no events were provided."

    parsed = []
    for item in events:
        try:
            title = str(item["title"]).strip()
            iso_date = parse_date(str(item["date"]))
            start_time = parse_time(str(item["start_time"]))
            minutes = max(5, min(int(item.get("duration_minutes", 60)), 1440))
        except (KeyError, TypeError, ValueError, AttributeError):
            return "Error: each event needs title, date, start_time and duration_minutes."
        if not title or not iso_date or not start_time:
            return f"Error: couldn't understand the date/time for {item}."
        start_dt = datetime.combine(date.fromisoformat(iso_date), start_time, tzinfo=TIMEZONE)
        if start_dt < now_local():
            return f"Error: '{title}' is in the past ({start_dt:%Y-%m-%d %H:%M})."
        parsed.append((title, start_dt, start_dt + timedelta(minutes=minutes),
                       str(item.get("description", ""))))

    horizon = max((s.date() - now_local().date()).days for _, s, _, _ in parsed) + 2
    existing = list_calendar_events(horizon) or []

    print("\nThe assistant wants to add these events:")
    for title, s, e, _ in parsed:
        line = f"  • {s.strftime('%a %d %b, %I:%M %p')}–{e.strftime('%I:%M %p')}  {title}"
        clashes = find_conflicts(s, e, existing)
        if clashes:
            line += f"   WARNING: overlaps: {', '.join(clashes)}"
        print(line)
    if input("Add to Google Calendar? [y/N]: ").strip().lower() not in {"y", "yes"}:
        return "The user declined. Nothing was added. Ask what they'd like changed."

    results = []
    for title, s, e, desc in parsed:
        ok, res = create_calendar_event(title, s, e, desc)
        results.append(f"created: {title}" if ok else f"FAILED: {title} ({res})")
    return "\n".join(results)


def run_tool(name, args):
    if not isinstance(args, dict):
        args = {}
    try:
        if name == "get_calendar_events":
            return tool_get_calendar_events(args.get("days", 7))
        if name == "get_tasks":
            return format_tasks(False)
        if name == "add_task":
            task, err = add_task(args.get("title") or "", args.get("deadline"),
                                 args.get("priority") or "medium",
                                 args.get("duration_minutes") or 60)
            return f"Error: {err}" if err else f"Added task [{task['id']}] {task['title']}"
        if name == "complete_task":
            task, err = complete_task(args.get("task") or "")
            return f"Error: {err}" if err else f"Completed task [{task['id']}] {task['title']}"
        if name == "schedule_events":
            return tool_schedule_events(args.get("events"))
        return f"Error: unknown tool '{name}'"
    except Exception as exc:
        return f"Error: {exc}"


def agent_system_prompt():
    today = now_local()
    days = "\n".join(
        f"  {(today + timedelta(days=i)).strftime('%A %Y-%m-%d')}" for i in range(8)
    )
    return (
        "You are a personal planning agent for the user. Timezone: Africa/Harare.\n"
        f"Right now it is {today.strftime('%A %Y-%m-%d %H:%M')}.\n"
        "Use this table to resolve words like 'tomorrow' or 'Thursday':\n"
        f"{days}\n\n"
        "Rules:\n"
        "- To plan a day, first call get_calendar_events and get_tasks, then build a realistic "
        "schedule with breaks and buffers around existing events, and call schedule_events.\n"
        "- If the user hasn't said when they're available, assume 09:00-17:00 and say so.\n"
        "- Never schedule in the past or over existing events.\n"
        "- Only say something was added when a tool result confirms it. If the user declined, don't retry.\n"
        "- Keep replies short and specific."
    )


def call_ollama_agent(messages):
    payload = {
        "model": MODEL,
        "messages": messages,
        "tools": TOOLS,
        "stream": False,
        "think": False,
        "options": {"temperature": 0.2, "num_predict": 900},
    }
    request = urllib.request.Request(
        OLLAMA_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            return json.loads(response.read().decode("utf-8")).get("message", {}), None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        return None, f"Ollama returned HTTP {exc.code}: {detail}"
    except urllib.error.URLError as exc:
        return None, f"Couldn't reach Ollama at {OLLAMA_URL}. Is it running? ({exc})"
    except Exception as exc:
        return None, f"Unexpected Ollama error: {exc}"


def agent_chat(user_message):
    agent_history.append({"role": "user", "content": user_message})

    for _ in range(MAX_TOOL_ROUNDS):
        recent = agent_history[-MAX_AGENT_MESSAGES:]
        while recent and recent[0]["role"] != "user":  # never start on an orphaned tool message
            recent = recent[1:]
        messages = [{"role": "system", "content": agent_system_prompt()}] + recent

        message, error = call_ollama_agent(messages)
        if error:
            return f"WARNING: {error}"

        content = strip_thinking(message.get("content", ""))
        calls = message.get("tool_calls") or []

        if not calls:
            agent_history.append({"role": "assistant", "content": content})
            return content or "I didn't get a usable answer. Try rephrasing."

        agent_history.append({"role": "assistant", "content": content, "tool_calls": calls})
        for call in calls:
            fn = call.get("function", {})
            name, args = fn.get("name", ""), fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            print(f"  … using tool: {name}")
            agent_history.append({"role": "tool", "tool_name": name,
                                  "content": run_tool(name, args)})

    return "I got stuck in a tool loop. Try a more specific request."


# ---------------------------------- Main ------------------------------------

def main():
    print("=" * 58)
    print(" PERSONAL AI PLANNER AND TASK MANAGER")
    print("=" * 58)
    print(f"Model: {MODEL}")
    print(f"Local time: {now_local().strftime('%A, %d %B %Y, %I:%M %p')} (Africa/Harare)")
    print(f"Task file: {TASKS_FILE}")
    if not TASKS_FILE.exists():
        save_tasks([])
        print("Created an empty tasks.json file.")

    connect_google_calendar(interactive=True)
    print("Type /help for commands. Type /exit to quit.\n")

    while True:
        try:
            user_message = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting planner.")
            break

        if not user_message:
            continue

        if user_message.lower() in {"/exit", "exit", "quit"}:
            print("Planner closed. Your saved tasks remain in tasks.json.")
            break

        command_result = handle_command(user_message)
        if command_result is not None:
            if command_result not in {"Finished adding task.", "Calendar event workflow finished.", "Help displayed.", "Calendar lookup finished."}:
                print(f"\nAssistant:\n{command_result}\n")
            continue

        # Deterministic task operations are handled before the language model.
        # Add-task intent must be checked before completion intent.
        added_result = natural_task_add(user_message)
        if added_result:
            print(f"\nAssistant:\n{added_result}\n")
            continue

        completed_result = natural_task_complete(user_message)
        if completed_result:
            print(f"\nAssistant:\n{completed_result}\n")
            continue

        print("\nAssistant is thinking...")
        answer = agent_chat(user_message)
        print(f"\nAssistant:\n{answer}\n")


if __name__ == "__main__":
    main()