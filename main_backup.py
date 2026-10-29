import json
import re
import urllib.request
import urllib.error

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# ============================================================
# CONFIGURATION
# ============================================================

MODEL = "qwen3:4b"
OLLAMA_URL = "http://localhost:11434/api/chat"

TIMEZONE = ZoneInfo("Africa/Harare")

BASE_DIR = Path(__file__).resolve().parent

CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"
TASKS_FILE = BASE_DIR / "tasks.json"

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events"
]

MAX_HISTORY_MESSAGES = 16
MAX_TOOL_ROUNDS = 6

calendar_service = None
pending_event = None

conversation = []


# ============================================================
# DATE AND TIME UTILITIES
# ============================================================

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def now_local():
    """Return the current time in Zimbabwe."""
    return datetime.now(TIMEZONE)


def parse_requested_date(text):
    """
    Resolve common date expressions in the user's original message.

    Returns:
        date: if the date can be resolved.
        None: if the message does not contain a supported expression.

    Raises:
        ValueError: if a date is ambiguous or invalid.
    """

    text = text.lower().strip()
    today = now_local().date()

    # Today and tomorrow.
    if re.search(r"\btoday\b", text):
        return today

    if re.search(r"\btomorrow\b", text):
        return today + timedelta(days=1)

    # "Next Monday", "next Friday", etc.
    match = re.search(
        r"\bnext\s+"
        r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        text,
    )

    if match:
        target_weekday = WEEKDAYS[match.group(1)]
        days_ahead = (target_weekday - today.weekday()) % 7

        # "Next Monday" means the next occurrence after today.
        if days_ahead == 0:
            days_ahead = 7

        return today + timedelta(days=days_ahead)

    # Explicit dates such as "19 October 2026".
    match = re.search(
        r"\b(\d{1,2})\s+"
        r"(january|february|march|april|may|june|july|"
        r"august|september|october|november|december)"
        r"(?:\s*,?\s*((?:19|20)\d{2}))?\b",
        text,
    )

    if match:
        day = int(match.group(1))
        month = MONTHS[match.group(2)]
        year_text = match.group(3)

        if year_text:
            year = int(year_text)
        else:
            year = today.year

        try:
            requested = date(year, month, day)
        except ValueError as error:
            raise ValueError(
                f"The requested date is invalid: {error}"
            ) from error

        # Do not silently roll a past date into another year.
        if not year_text and requested < today:
            raise ValueError(
                f"{requested.strftime('%d %B %Y')} has already passed. "
                "Please specify the intended year."
            )

        return requested

    # ISO date: 2026-10-19.
    match = re.search(
        r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b",
        text,
    )

    if match:
        try:
            return date.fromisoformat(match.group(0))
        except ValueError as error:
            raise ValueError(
                f"The requested date is invalid: {error}"
            ) from error

    return None


def format_datetime(value):
    """Format a calendar datetime for display."""

    parsed = datetime.fromisoformat(value)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TIMEZONE)

    return parsed.astimezone(TIMEZONE).strftime(
        "%A, %d %B %Y at %I:%M %p"
    )


def refresh_system_prompt():
    """Update the system prompt with the current date and time."""

    now = now_local()

    return f"""
You are a personal AI planning and task-management assistant.

CURRENT DATE AND TIME
Date: {now.strftime("%A, %d %B %Y")}
Time: {now.strftime("%I:%M %p")}
Timezone: Africa/Harare (UTC+02:00).

YOUR PURPOSE

Help the user manage commitments, tasks, projects, studies,
personal goals, and time.

You are a practical planning assistant, not merely a chatbot.

CORE CAPABILITIES

1. Review Google Calendar commitments.
2. Propose calendar events.
3. Manage a persistent list of tasks.
4. Break projects into actionable steps.
5. Prioritise work based on deadlines, importance, and effort.
6. Suggest realistic study sessions around existing commitments.
7. Identify overloaded schedules and conflicting priorities.
8. Help the user review progress and decide what to do next.

TOOLS

You can:
- List upcoming calendar events.
- Propose a calendar event for approval.
- Add tasks.
- List tasks.
- Mark tasks complete.

CALENDAR RULES

- Retrieve actual calendar events when planning around commitments.
- Never invent calendar events or claim a proposed event already exists.
- Use Africa/Harare for local times.
- Never silently invent a year.
- Use ISO 8601 datetime values with a +02:00 offset for tool calls.
- A proposed event is NOT created until the user approves it.
- If event creation is rejected, explain the error honestly.

DATE RULES

- Use the current date above as the reference point.
- For "today", "tomorrow", and "next Monday", the application
  can resolve dates using Python.
- Do not assume your own day-of-week calculations are infallible.
- If the user provides an explicit date, preserve that date.
- Ask for clarification if the intended date is ambiguous.
- Do not schedule an event in the past.

TASK-MANAGEMENT RULES

- Give tasks clear, actionable titles.
- When possible, capture a deadline and estimated duration.
- Never invent a deadline the user did not provide.
- Break large projects into smaller tasks when requested.
- Prioritise urgent, important, and dependency-blocking tasks.
- Distinguish suggestions from tasks actually saved in the task list.
- Do not mark tasks complete unless the appropriate tool succeeds.
- Do not claim a task was saved unless the tool confirms it.

PLANNING RULES

When asked to plan a day, week, study session, or project:

1. Understand the goal and deadline.
2. Retrieve calendar events if existing commitments matter.
3. Retrieve outstanding tasks if task priorities matter.
4. Consider available time, task duration, and deadlines.
5. Recommend a realistic plan with specific actions.
6. Explain important trade-offs briefly.
7. Clearly distinguish proposed schedule blocks from confirmed events.

Do not merely list calendar events when the user asks for a plan.

COMMUNICATION STYLE

- Be concise, organised, and practical.
- Use headings and bullet points when useful.
- Ask only necessary clarification questions.
- Never pretend to have performed an action.
- Never expose credentials, access tokens, or secrets.
"""


# ============================================================
# GOOGLE CALENDAR CONNECTION
# ============================================================

def connect_google_calendar():
    """Authenticate with Google Calendar."""

    credentials = None

    if TOKEN_FILE.exists():
        credentials = Credentials.from_authorized_user_file(
            str(TOKEN_FILE),
            SCOPES,
        )

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())

        TOKEN_FILE.write_text(
            credentials.to_json(),
            encoding="utf-8",
        )

    if not credentials or not credentials.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_FILE),
            SCOPES,
        )

        credentials = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(
            credentials.to_json(),
            encoding="utf-8",
        )

    return build(
        "calendar",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )


# ============================================================
# CALENDAR MANAGEMENT
# ============================================================

def list_calendar_events(days=14):
    """Retrieve upcoming events from the primary calendar."""

    days = max(1, min(int(days), 90))

    now = now_local()
    end = now + timedelta(days=days)

    response = calendar_service.events().list(
        calendarId="primary",
        timeMin=now.isoformat(),
        timeMax=end.isoformat(),
        maxResults=100,
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    events = []

    for item in response.get("items", []):
        start = item.get("start", {})
        finish = item.get("end", {})

        events.append({
            "id": item.get("id"),
            "title": item.get("summary", "Untitled event"),
            "start": start.get("dateTime", start.get("date")),
            "end": finish.get("dateTime", finish.get("date")),
            "description": item.get("description", ""),
            "location": item.get("location", ""),
            "all_day": (
                "date" in start and "dateTime" not in start
            ),
        })

    return {
        "success": True,
        "count": len(events),
        "days": days,
        "events": events,
    }


def propose_calendar_event(
    title,
    start,
    end,
    description="",
    location="",
    user_message="",
):
    """
    Validate an event and request approval.

    This function does not create the event.
    """

    global pending_event

    if pending_event is not None:
        return {
            "success": False,
            "error": (
                "Another event is awaiting confirmation. "
                "Ask the user to approve or cancel that event first."
            ),
        }

    if not title or not title.strip():
        return {
            "success": False,
            "error": "The event title cannot be empty.",
        }

    try:
        start_time = datetime.fromisoformat(start)
        end_time = datetime.fromisoformat(end)

        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=TIMEZONE)

        if end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=TIMEZONE)

        start_time = start_time.astimezone(TIMEZONE)
        end_time = end_time.astimezone(TIMEZONE)

    except (ValueError, TypeError) as error:
        return {
            "success": False,
            "error": f"Invalid date or time: {error}",
        }

    if end_time <= start_time:
        return {
            "success": False,
            "error": "The event end must be after its start.",
        }

    if start_time < now_local():
        return {
            "success": False,
            "error": (
                "The proposed event starts in the past. "
                "Please check the intended date and time."
            ),
        }

    # Compare the model's proposed date against dates found in
    # the user's original request.
    try:
        requested_date = parse_requested_date(user_message)
    except ValueError as error:
        return {
            "success": False,
            "error": str(error),
        }

    if (
        requested_date is not None
        and start_time.date() != requested_date
    ):
        return {
            "success": False,
            "error": (
                "The proposed event date does not match the date "
                f"requested by the user. Requested: {requested_date}. "
                f"Proposed: {start_time.date()}. Do not create the "
                "event. Correct the date or ask the user to clarify."
            ),
        }

    pending_event = {
        "title": title.strip(),
        "start": start_time.isoformat(),
        "end": end_time.isoformat(),
        "description": description,
        "location": location,
    }

    return {
        "success": True,
        "needs_confirmation": True,
        "message": (
            "The event has been prepared but NOT created. "
            "Present the proposed details to the user and ask them "
            "to type YES to create it or NO to cancel."
        ),
        "proposed_event": {
            "title": title.strip(),
            "date": start_time.strftime("%A, %d %B %Y"),
            "start_time": start_time.strftime("%I:%M %p"),
            "end_time": end_time.strftime("%I:%M %p"),
            "timezone": "Africa/Harare",
            "location": location,
        },
    }


def create_approved_event():
    """Create the event only after explicit user approval."""

    global pending_event

    if pending_event is None:
        return {
            "success": False,
            "error": "There is no event awaiting approval.",
        }

    event_data = pending_event

    # Check the time again at the point of creation.
    start_time = datetime.fromisoformat(event_data["start"])
    end_time = datetime.fromisoformat(event_data["end"])

    if start_time < now_local():
        pending_event = None

        return {
            "success": False,
            "error": (
                "The event start time has passed. "
                "Please propose a new date or time."
            ),
        }

    body = {
        "summary": event_data["title"],
        "start": {
            "dateTime": event_data["start"],
            "timeZone": "Africa/Harare",
        },
        "end": {
            "dateTime": event_data["end"],
            "timeZone": "Africa/Harare",
        },
    }

    if event_data["description"]:
        body["description"] = event_data["description"]

    if event_data["location"]:
        body["location"] = event_data["location"]

    try:
        created = calendar_service.events().insert(
            calendarId="primary",
            body=body,
        ).execute()

        pending_event = None

        return {
            "success": True,
            "message": "Event created successfully.",
            "event": {
                "id": created.get("id"),
                "title": created.get("summary"),
                "start": created.get("start", {}).get("dateTime"),
                "end": created.get("end", {}).get("dateTime"),
                "link": created.get("htmlLink"),
            },
        }

    except HttpError as error:
        # Retain the pending event so the user can retry if appropriate.
        return {
            "success": False,
            "error": f"Google Calendar API error: {error}",
        }


# ============================================================
# LOCAL TASK MANAGEMENT
# ============================================================

def load_tasks():
    """Load tasks from the local JSON file."""

    if not TASKS_FILE.exists():
        return []

    try:
        data = json.loads(
            TASKS_FILE.read_text(encoding="utf-8")
        )

        if isinstance(data, list):
            return data

        return []

    except (json.JSONDecodeError, OSError):
        print(
            "Warning: tasks.json could not be read. "
            "No tasks were loaded."
        )
        return []


def save_tasks(tasks):
    """Persist tasks locally."""

    TASKS_FILE.write_text(
        json.dumps(tasks, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def add_task(title, deadline="", priority="medium", duration_minutes=60):
    """Save a new task."""

    title = title.strip()

    if not title:
        return {
            "success": False,
            "error": "A task title is required.",
        }

    allowed_priorities = {"low", "medium", "high"}

    priority = priority.lower().strip()

    if priority not in allowed_priorities:
        priority = "medium"

    try:
        duration_minutes = int(duration_minutes)
    except (ValueError, TypeError):
        duration_minutes = 60

    if duration_minutes < 1 or duration_minutes > 1440:
        return {
            "success": False,
            "error": (
                "Task duration must be between 1 and 1440 minutes."
            ),
        }

    if deadline:
        try:
            date.fromisoformat(deadline)
        except ValueError:
            return {
                "success": False,
                "error": (
                    "Deadline must use YYYY-MM-DD format."
                ),
            }

    tasks = load_tasks()

    next_id = max(
        (task.get("id", 0) for task in tasks),
        default=0,
    ) + 1

    task = {
        "id": next_id,
        "title": title,
        "deadline": deadline or None,
        "priority": priority,
        "duration_minutes": duration_minutes,
        "status": "pending",
        "created_at": now_local().isoformat(),
        "completed_at": None,
    }

    tasks.append(task)
    save_tasks(tasks)

    return {
        "success": True,
        "message": "Task saved.",
        "task": task,
    }


def list_tasks(status="pending"):
    """List locally saved tasks."""

    tasks = load_tasks()

    if status != "all":
        tasks = [
            task for task in tasks
            if task.get("status") == status
        ]

    priority_order = {
        "high": 0,
        "medium": 1,
        "low": 2,
    }

    tasks.sort(
        key=lambda task: (
            task.get("deadline") or "9999-12-31",
            priority_order.get(task.get("priority"), 1),
            task.get("id", 0),
        )
    )

    return {
        "success": True,
        "count": len(tasks),
        "tasks": tasks,
    }


def complete_task(task_id):
    """Mark an existing task as complete."""

    try:
        task_id = int(task_id)
    except (ValueError, TypeError):
        return {
            "success": False,
            "error": "Task ID must be a number.",
        }

    tasks = load_tasks()

    for task in tasks:
        if task.get("id") == task_id:
            if task.get("status") == "completed":
                return {
                    "success": True,
                    "message": "Task was already completed.",
                    "task": task,
                }

            task["status"] = "completed"
            task["completed_at"] = now_local().isoformat()

            save_tasks(tasks)

            return {
                "success": True,
                "message": "Task marked as complete.",
                "task": task,
            }

    return {
        "success": False,
        "error": f"No task found with ID {task_id}.",
    }


# ============================================================
# TOOL DEFINITIONS
# ============================================================

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_calendar_events",
            "description": (
                "Retrieve actual upcoming Google Calendar events. "
                "Use when reviewing commitments or planning around them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Days ahead, between 1 and 90.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_calendar_event",
            "description": (
                "Prepare a calendar event for user approval. "
                "This does NOT create the event. Always use the current "
                "date and the Africa/Harare timezone. Start and end "
                "must be ISO 8601 datetimes with timezone offsets. "
                "Never invent a year."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start": {
                        "type": "string",
                        "description": "ISO 8601 start datetime.",
                    },
                    "end": {
                        "type": "string",
                        "description": "ISO 8601 end datetime.",
                    },
                    "description": {"type": "string"},
                    "location": {"type": "string"},
                },
                "required": ["title", "start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_task",
            "description": (
                "Save a task to the user's persistent local task list. "
                "Only set a deadline if the user provided one or it "
                "was clearly established in the conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "deadline": {
                        "type": "string",
                        "description": "Optional deadline in YYYY-MM-DD format.",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "description": "Estimated duration in minutes.",
                    },
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": (
                "Retrieve saved tasks. Use this before planning work "
                "around outstanding tasks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["pending", "completed", "all"],
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": "Mark a saved task as completed using its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                },
                "required": ["task_id"],
            },
        },
    },
]


# ============================================================
# TOOL EXECUTION
# ============================================================

def execute_tool(name, arguments, user_message):
    """Dispatch a tool call to the corresponding Python function."""

    try:
        if name == "list_calendar_events":
            return list_calendar_events(
                arguments.get("days", 14)
            )

        if name == "propose_calendar_event":
            return propose_calendar_event(
                title=arguments["title"],
                start=arguments["start"],
                end=arguments["end"],
                description=arguments.get("description", ""),
                location=arguments.get("location", ""),
                user_message=user_message,
            )

        if name == "add_task":
            return add_task(
                title=arguments["title"],
                deadline=arguments.get("deadline", ""),
                priority=arguments.get("priority", "medium"),
                duration_minutes=arguments.get(
                    "duration_minutes", 60
                ),
            )

        if name == "list_tasks":
            return list_tasks(
                arguments.get("status", "pending")
            )

        if name == "complete_task":
            return complete_task(arguments["task_id"])

        return {
            "success": False,
            "error": f"Unknown tool: {name}",
        }

    except (KeyError, TypeError, ValueError) as error:
        return {
            "success": False,
            "error": f"Invalid tool arguments: {error}",
        }

    except HttpError as error:
        return {
            "success": False,
            "error": f"Google Calendar API error: {error}",
        }

    except OSError as error:
        return {
            "success": False,
            "error": f"Local file error: {error}",
        }


# ============================================================
# LOCAL AI
# ============================================================

def ask_ai(user_message):
    """Send a message to Ollama and process tool calls."""

    conversation.append({
        "role": "user",
        "content": user_message,
    })

    while len(conversation) > MAX_HISTORY_MESSAGES:
        conversation.pop(0)

    messages = [
        {
            "role": "system",
            "content": refresh_system_prompt(),
        },
        *conversation,
    ]

    for _ in range(MAX_TOOL_ROUNDS):
        payload = {
            "model": MODEL,
            "messages": messages,
            "tools": TOOLS,
            "stream": False,
            "options": {
                "temperature": 0.1,
                "num_predict": 512,
            },
        }

        request = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=300,
            ) as response:
                result = json.loads(
                    response.read().decode("utf-8")
                )

        except urllib.error.URLError as error:
            return (
                f"Could not connect to Ollama: {error}\n"
                "Check that Ollama is running."
            )

        except json.JSONDecodeError:
            return "Ollama returned an invalid response."

        assistant_message = result.get("message", {})
        messages.append(assistant_message)

        tool_calls = assistant_message.get("tool_calls", [])

        if not tool_calls:
            answer = assistant_message.get(
                "content",
                "I couldn't generate a response.",
            )

            conversation.append({
                "role": "assistant",
                "content": answer,
            })

            while len(conversation) > MAX_HISTORY_MESSAGES:
                conversation.pop(0)

            return answer

        for tool_call in tool_calls:
            function = tool_call.get("function", {})
            name = function.get("name")
            arguments = function.get("arguments", {})

            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}

            print(f"\n[Using tool: {name}]")

            tool_result = execute_tool(
                name,
                arguments,
                user_message,
            )

            messages.append({
                "role": "tool",
                "tool_name": name,
                "content": json.dumps(
                    tool_result,
                    ensure_ascii=False,
                ),
            })

    return (
        "I reached the tool-call limit. "
        "Try breaking the request into smaller steps."
    )


# ============================================================
# COMMAND HANDLING
# ============================================================

def handle_confirmation(user_message):
    """
    Handle approval or cancellation of a pending event.

    Returns None if no event is awaiting approval.
    """

    global pending_event

    if pending_event is None:
        return None

    answer = user_message.strip().upper()

    if answer == "YES":
        result = create_approved_event()

        if result["success"]:
            event = result["event"]

            return (
                "✅ Event created successfully!\n"
                f"Title: {event['title']}\n"
                f"Start: {format_datetime(event['start'])}\n"
                f"End: {format_datetime(event['end'])}\n"
                f"Google Calendar: {event['link']}"
            )

        return (
            "❌ The event could not be created.\n"
            f"Reason: {result.get('error', 'Unknown error')}"
        )

    if answer == "NO":
        pending_event = None
        return "Event cancelled. Nothing was added to Google Calendar."

    return (
        "An event is awaiting your decision.\n"
        "Review the proposed details above, then type YES to create "
        "it or NO to cancel it."
    )


# ============================================================
# MAIN APPLICATION
# ============================================================

def main():
    global calendar_service

    print("=" * 55)
    print("       PERSONAL AI PLANNER AND TASK MANAGER")
    print("=" * 55)
    print(f"Model: {MODEL}")
    print("AI: Local Ollama")
    print("Calendar: Google Calendar")
    print("Tasks: Local JSON storage")
    print("Timezone: Africa/Harare")
    print()
    print("Commands:")
    print("  /tasks       List outstanding tasks")
    print("  /alltasks    List all tasks")
    print("  /events      Show events for the next 14 days")
    print("  /help        Show commands")
    print("  /exit        Quit")
    print("=" * 55)

    try:
        calendar_service = connect_google_calendar()
        print("\n✅ Google Calendar connected.")

    except Exception as error:
        print(f"\n❌ Google Calendar connection failed: {error}")
        print(
            "Check credentials.json, token.json, and your "
            "Google Calendar API setup."
        )
        return

    # Create the task file if it does not already exist.
    if not TASKS_FILE.exists():
        save_tasks([])

    print(f"✅ Task storage ready: {TASKS_FILE.name}")
    print("\nAssistant ready. What would you like to work on?")

    while True:
        try:
            user_message = input("\nYou: ").strip()

            if not user_message:
                continue

            if user_message.lower() in {"/exit", "/quit"}:
                print("\nGoodbye.")
                break

            if user_message.lower() == "/help":
                print(
                    "\nTry asking:\n"
                    "- Help me plan my week.\n"
                    "- What should I work on today?\n"
                    "- Add a task to finish my image processing project.\n"
                    "- Show my outstanding tasks.\n"
                    "- Break my project into manageable tasks.\n"
                    "- Plan a study session around my calendar.\n"
                    "- Schedule a study session for tomorrow."
                )
                continue

            if user_message.lower() == "/tasks":
                result = list_tasks("pending")
                print(f"\nOutstanding tasks: {result['count']}")

                for task in result["tasks"]:
                    deadline = task.get("deadline") or "No deadline"

                    print(
                        f"  [{task['id']}] {task['title']} | "
                        f"{task['priority']} priority | "
                        f"{deadline} | "
                        f"{task['duration_minutes']} min"
                    )

                if not result["tasks"]:
                    print("  No outstanding tasks.")

                continue

            if user_message.lower() == "/alltasks":
                result = list_tasks("all")
                print(f"\nAll tasks: {result['count']}")

                for task in result["tasks"]:
                    deadline = task.get("deadline") or "No deadline"

                    print(
                        f"  [{task['id']}] {task['title']} | "
                        f"{task['status']} | "
                        f"{deadline}"
                    )

                if not result["tasks"]:
                    print("  No tasks saved yet.")

                continue

            if user_message.lower() == "/events":
                result = list_calendar_events(14)
                print("\nUpcoming calendar events:")

                if not result["events"]:
                    print("  No upcoming events.")
                else:
                    for event in result["events"]:
                        if event["all_day"]:
                            print(
                                f"  {event['title']}: {event['start']}"
                            )
                        else:
                            print(
                                f"  {event['title']}: "
                                f"{format_datetime(event['start'])}"
                            )

                continue

            # Process an event approval before sending anything
            # to the language model.
            confirmation_result = handle_confirmation(user_message)

            if confirmation_result is not None:
                print(f"\nAssistant: {confirmation_result}")
                continue

            answer = ask_ai(user_message)

            if answer:
                print(f"\nAssistant: {answer}")

        except KeyboardInterrupt:
            print("\n\nGoodbye.")
            break

        except Exception as error:
            print(f"\nAn unexpected error occurred: {error}")


if __name__ == "__main__":
    main()