"""Local-first inbox, projects, notes, planning, and review services."""

import json
import hashlib
import re
import threading
from functools import wraps
from datetime import date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

import main

STATE_FILE = Path(main.BASE_DIR) / "productivity.json"
_state_lock = threading.RLock()


def _serialized(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with _state_lock:
            return function(*args, **kwargs)
    return wrapped


def _now() -> datetime:
    return main.now_local()


def _id() -> str:
    return uuid4().hex


def _empty_state() -> dict:
    return {
        "schema_version": 1,
        "inbox": [],
        "projects": [],
        "milestones": [],
        "notes": [],
        "proposals": [],
        "dismissed_insights": [],
        "settings": {
            "work_start": "09:00",
            "work_end": "17:00",
            "working_days": [0, 1, 2, 3, 4],
            "break_minutes": 10,
            "break_after_minutes": 120,
            "buffer_minutes": 10,
            "reminders_enabled": True,
            "remind_overdue": True,
            "remind_due_today": True,
            "reminder_minutes_before": 30,
            "widget_enabled": False,
            "widget_always_on_top": False,
        },
    }


def load_state() -> dict:
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_state()
    except (OSError, json.JSONDecodeError):
        return _empty_state()
    if not isinstance(raw, dict):
        return _empty_state()
    state = _empty_state()
    state.update({key: value for key, value in raw.items() if key in state})
    for key in ("inbox", "projects", "milestones", "notes", "proposals"):
        if not isinstance(state.get(key), list):
            state[key] = []
        state[key] = [item for item in state[key] if isinstance(item, dict)]
    if not isinstance(state.get("dismissed_insights"), list):
        state["dismissed_insights"] = []
    if not isinstance(state.get("settings"), dict):
        state["settings"] = _empty_state()["settings"]
    else:
        state["settings"] = {**_empty_state()["settings"], **state["settings"]}
    return state


def save_state(state: dict) -> None:
    with _state_lock:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = STATE_FILE.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(STATE_FILE)


def _timestamp(value: str | None = None) -> str:
    return value or _now().isoformat(timespec="seconds")


def _clean_text(value: object, name: str, limit: int = 4000, required: bool = True) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{name} is required.")
    if len(text) > limit:
        raise ValueError(f"{name} must be {limit} characters or fewer.")
    return text


def _find_record(collection: str, record_id: str) -> dict | None:
    return next((item for item in load_state()[collection] if item.get("id") == record_id), None)


def _replace_record(collection: str, record_id: str, updates: dict) -> dict:
    state = load_state()
    record = next((item for item in state[collection] if item.get("id") == record_id), None)
    if record is None:
        raise KeyError(f"{collection.rstrip('s').capitalize()} not found.")
    record.update(updates)
    record["updated_at"] = _timestamp()
    save_state(state)
    return record


# Inbox

@_serialized
def create_inbox_item(text: str, source: str = "manual", source_id: str | None = None,
                      notes: str = "", received_at: str | None = None) -> dict:
    text = _clean_text(text, "Original text")
    allowed_sources = {"manual", "desktop_chat", "whatsapp", "quick_capture"}
    if source not in allowed_sources:
        raise ValueError("Unsupported inbox source.")
    with _state_lock:
        state = load_state()
        if source_id:
            existing = next((item for item in state["inbox"]
                             if item.get("source") == source and item.get("source_id") == source_id), None)
            if existing:
                return existing
        item = {
            "id": _id(), "original_text": text, "created_at": _timestamp(received_at),
            "source": source, "source_id": source_id, "status": "open",
            "notes": _clean_text(notes, "Notes", required=False), "suggestion": None,
            "processed_at": None,
        }
        state["inbox"].append(item)
        save_state(state)
        return item


def list_inbox(status: str | None = None) -> list[dict]:
    items = load_state()["inbox"]
    return [item for item in items if status is None or item.get("status") == status]


@_serialized
def update_inbox_item(item_id: str, updates: dict) -> dict:
    allowed = {"original_text", "notes", "suggestion"}
    cleaned = {key: value for key, value in updates.items() if key in allowed}
    for key in ("original_text", "notes"):
        if key in cleaned:
            cleaned[key] = _clean_text(cleaned[key], key.replace("_", " ").capitalize(), required=key == "original_text")
    return _replace_record("inbox", item_id, cleaned)


@_serialized
def set_inbox_status(item_id: str, status: str) -> dict:
    if status not in {"open", "processed", "archived", "dismissed"}:
        raise ValueError("Invalid inbox status.")
    updates = {"status": status}
    if status == "processed":
        updates["processed_at"] = _timestamp()
    return _replace_record("inbox", item_id, updates)


@_serialized
def convert_inbox_item(item_id: str, record_type: str, details: dict | None = None) -> dict:
    details = details or {}
    item = _find_record("inbox", item_id)
    if item is None:
        raise KeyError("Inbox item not found.")
    if item.get("status") == "processed":
        raise ValueError("This inbox item has already been processed.")
    text = item["original_text"]
    if record_type in {"task", "reminder"}:
        title = _clean_text(details.get("title") or text, "Task title", 300)
        task, error = main.add_task(
            title, details.get("deadline"), details.get("priority", "medium"),
            details.get("duration_minutes", 60), details.get("project_id"),
        )
        if error or not task:
            raise ValueError(error or "Task could not be saved.")
        result = {"type": "task", "record": task}
    elif record_type == "note":
        result = {"type": "note", "record": create_note(
            details.get("title") or text[:120], details.get("content") or text,
            details.get("project_id"), details.get("task_id"), details.get("tags", []),
        )}
    elif record_type == "project":
        result = {"type": "project", "record": create_project(
            details.get("name") or text[:120], details.get("description") or text,
            details.get("target_deadline"), details.get("priority", "medium"),
        )}
    elif record_type == "calendar_event":
        start_text = details.get("start")
        if not start_text:
            raise ValueError("A specific start date and time is required before creating a calendar event.")
        start = _parse_datetime(start_text)
        if start < _now():
            raise ValueError("The calendar event cannot start in the past.")
        success, event = main.create_calendar_event(
            _clean_text(details.get("title") or text, "Event title", 300),
            start,
            start + timedelta(minutes=int(details.get("duration_minutes", 60))),
            _clean_text(details.get("description", ""), "Description", 2000, required=False),
        )
        if not success:
            raise ValueError("Google Calendar could not confirm the event. Nothing was marked as processed.")
        result = {"type": "calendar_event", "record": event}
    else:
        raise ValueError("Unsupported conversion type.")
    set_inbox_status(item_id, "processed")
    return result


# Projects and milestones

@_serialized
def create_project(name: str, description: str = "", target_deadline: str | None = None,
                   priority: str = "medium", start_date: str | None = None) -> dict:
    name = _clean_text(name, "Project name", 200)
    priority = priority.lower()
    if priority not in {"low", "medium", "high"}:
        raise ValueError("Priority must be low, medium, or high.")
    deadline = main.parse_date(target_deadline) if target_deadline else None
    if target_deadline and not deadline:
        raise ValueError("Invalid target deadline.")
    started = main.parse_date(start_date) if start_date else _now().date().isoformat()
    if not started:
        raise ValueError("Invalid start date.")
    project = {
        "id": _id(), "name": name, "description": _clean_text(description, "Description", required=False),
        "status": "active", "start_date": started, "target_deadline": deadline,
        "priority": priority, "notes": "", "created_at": _timestamp(), "updated_at": _timestamp(),
    }
    state = load_state()
    state["projects"].append(project)
    save_state(state)
    return project


@_serialized
def update_project(project_id: str, updates: dict) -> dict:
    cleaned = {}
    for field in ("name", "description", "notes"):
        if field in updates:
            cleaned[field] = _clean_text(updates[field], field.capitalize(), 4000 if field != "name" else 200,
                                         required=field == "name")
    if "status" in updates:
        if updates["status"] not in {"active", "on_hold", "completed", "archived"}:
            raise ValueError("Invalid project status.")
        cleaned["status"] = updates["status"]
    if "priority" in updates:
        if updates["priority"] not in {"low", "medium", "high"}:
            raise ValueError("Priority must be low, medium, or high.")
        cleaned["priority"] = updates["priority"]
    for field in ("start_date", "target_deadline"):
        if field in updates:
            value = main.parse_date(updates[field]) if updates[field] else None
            if updates[field] and not value:
                raise ValueError(f"Invalid {field.replace('_', ' ')}.")
            cleaned[field] = value
    return _replace_record("projects", project_id, cleaned)


def list_projects() -> list[dict]:
    state = load_state()
    tasks = main.load_tasks()
    result = []
    today = _now().date()
    for project in state["projects"]:
        associated = [task for task in tasks if task.get("project_id") == project["id"]]
        milestones = [item for item in state["milestones"] if item.get("project_id") == project["id"]]
        completed = sum(task.get("status") == "completed" for task in associated)
        completed += sum(item.get("completed") for item in milestones)
        total = len(associated) + len(milestones)
        enriched = dict(project)
        enriched.update({
            "task_count": len(associated),
            "completed_task_count": sum(task.get("status") == "completed" for task in associated),
            "outstanding_task_count": sum(task.get("status") != "completed" for task in associated),
            "overdue_task_count": sum(
                1 for task in associated
                if task.get("status") != "completed"
                and task.get("deadline")
                and task["deadline"] < today.isoformat()
            ),
            "estimated_remaining_minutes": sum(int(task.get("duration_minutes", 60)) for task in associated
                                                if task.get("status") != "completed"),
            "milestones": milestones,
            "upcoming_milestones": [item for item in milestones if not item.get("completed")
                                    and (not item.get("target_date") or item["target_date"] >= today.isoformat())],
            "progress_percent": round(100 * completed / total) if total else 0,
            "at_risk": bool(project.get("target_deadline") and
                            (date.fromisoformat(project["target_deadline"]) - today).days <= 7 and
                            total > completed and (total - completed) / max(total, 1) >= 0.5),
        })
        result.append(enriched)
    return result


@_serialized
def create_milestone(project_id: str, title: str, description: str = "", target_date: str | None = None) -> dict:
    if _find_record("projects", project_id) is None:
        raise KeyError("Project not found.")
    target = main.parse_date(target_date) if target_date else None
    if target_date and not target:
        raise ValueError("Invalid milestone date.")
    milestone = {
        "id": _id(), "project_id": project_id, "title": _clean_text(title, "Milestone title", 200),
        "description": _clean_text(description, "Description", required=False), "target_date": target,
        "completed": False, "completed_at": None, "created_at": _timestamp(), "updated_at": _timestamp(),
    }
    state = load_state()
    state["milestones"].append(milestone)
    save_state(state)
    return milestone


@_serialized
def update_milestone(milestone_id: str, updates: dict) -> dict:
    cleaned = {}
    if "title" in updates:
        cleaned["title"] = _clean_text(updates["title"], "Milestone title", 200)
    if "description" in updates:
        cleaned["description"] = _clean_text(updates["description"], "Description", required=False)
    if "target_date" in updates:
        value = main.parse_date(updates["target_date"]) if updates["target_date"] else None
        if updates["target_date"] and not value:
            raise ValueError("Invalid milestone date.")
        cleaned["target_date"] = value
    if "completed" in updates:
        if not isinstance(updates["completed"], bool):
            raise ValueError("completed must be a boolean.")
        cleaned["completed"] = updates["completed"]
        cleaned["completed_at"] = _timestamp() if updates["completed"] else None
    return _replace_record("milestones", milestone_id, cleaned)


@_serialized
def delete_milestone(milestone_id: str) -> dict:
    state = load_state()
    milestone = next((item for item in state["milestones"] if item.get("id") == milestone_id), None)
    if milestone is None:
        raise KeyError("Milestone not found.")
    state["milestones"].remove(milestone)
    save_state(state)
    return milestone


def link_task_to_project(task_id: int, project_id: str | None) -> dict:
    if project_id and _find_record("projects", project_id) is None:
        raise KeyError("Project not found.")
    task, error = main.update_task(task_id, {"project_id": project_id})
    if error or not task:
        raise KeyError(error or "Task not found.")
    return task


# Notes

@_serialized
def create_note(title: str, content: str, project_id: str | None = None, task_id: int | None = None,
                tags: list[str] | None = None) -> dict:
    title = _clean_text(title, "Note title", 200)
    content = _clean_text(content, "Note content", 20000)
    if project_id and _find_record("projects", project_id) is None:
        raise KeyError("Project not found.")
    if task_id is not None and not any(int(task.get("id", -1)) == task_id for task in main.load_tasks()):
        raise KeyError("Task not found.")
    note = {
        "id": _id(), "title": title, "content": content,
        "created_at": _timestamp(), "updated_at": _timestamp(),
        "project_id": project_id, "task_id": task_id,
        "tags": [_clean_text(tag, "Tag", 40) for tag in (tags or [])[:20]],
    }
    state = load_state()
    state["notes"].append(note)
    save_state(state)
    return note


@_serialized
def update_note(note_id: str, updates: dict) -> dict:
    cleaned = {}
    for field, limit in (("title", 200), ("content", 20000)):
        if field in updates:
            cleaned[field] = _clean_text(updates[field], field.capitalize(), limit)
    if "project_id" in updates:
        project_id = updates["project_id"] or None
        if project_id and _find_record("projects", project_id) is None:
            raise KeyError("Project not found.")
        cleaned["project_id"] = project_id
    if "task_id" in updates:
        task_id = updates["task_id"]
        if task_id is not None and not any(int(task.get("id", -1)) == int(task_id) for task in main.load_tasks()):
            raise KeyError("Task not found.")
        cleaned["task_id"] = task_id
    if "tags" in updates:
        cleaned["tags"] = [_clean_text(tag, "Tag", 40) for tag in updates["tags"][:20]]
    return _replace_record("notes", note_id, cleaned)


@_serialized
def delete_note(note_id: str) -> dict:
    state = load_state()
    note = next((item for item in state["notes"] if item.get("id") == note_id), None)
    if note is None:
        raise KeyError("Note not found.")
    state["notes"].remove(note)
    save_state(state)
    return note


def search_notes(query: str) -> list[dict]:
    query = _clean_text(query, "Search query", 300).casefold()
    words = set(re.findall(r"[a-z0-9]+", query))
    ranked = []
    for note in load_state()["notes"]:
        haystack = " ".join([note.get("title", ""), note.get("content", ""), *note.get("tags", [])]).casefold()
        overlap = len(words & set(re.findall(r"[a-z0-9]+", haystack)))
        if query in haystack or overlap:
            ranked.append((overlap + (len(words) if query in haystack else 0), note))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [note for _, note in ranked[:20]]


# Deterministic daily planner and adaptive scheduling

@_serialized
def update_planning_settings(updates: dict) -> dict:
    state = load_state()
    settings = state["settings"]
    for field in ("work_start", "work_end"):
        if field in updates:
            try:
                time.fromisoformat(updates[field])
            except (TypeError, ValueError):
                raise ValueError(f"{field.replace('_', ' ').capitalize()} must use HH:MM.")
            settings[field] = updates[field]
    if time.fromisoformat(settings["work_start"]) >= time.fromisoformat(settings["work_end"]):
        raise ValueError("Working-hours end must be after start.")
    if "working_days" in updates:
        days = updates["working_days"]
        if not isinstance(days, list) or not days or any(not isinstance(day, int) or day not in range(7) for day in days):
            raise ValueError("Working days must contain weekday numbers from 0 to 6.")
        settings["working_days"] = sorted(set(days))
    for field in ("break_minutes", "break_after_minutes", "buffer_minutes"):
        if field in updates:
            try:
                value = int(updates[field])
            except (TypeError, ValueError):
                raise ValueError(f"{field.replace('_', ' ').capitalize()} must be a number.")
            if not 0 <= value <= 120:
                raise ValueError(f"{field.replace('_', ' ').capitalize()} must be between 0 and 120.")
            settings[field] = value
    for field in ("reminders_enabled", "remind_overdue", "remind_due_today",
                  "widget_enabled", "widget_always_on_top"):
        if field in updates:
            if not isinstance(updates[field], bool):
                raise ValueError(f"{field.replace('_', ' ').capitalize()} must be a boolean.")
            settings[field] = updates[field]
    if "reminder_minutes_before" in updates:
        try:
            minutes = int(updates["reminder_minutes_before"])
        except (TypeError, ValueError):
            raise ValueError("Reminder lead time must be a number of minutes.")
        if not 0 <= minutes <= 1440:
            raise ValueError("Reminder lead time must be between 0 and 1440 minutes.")
        settings["reminder_minutes_before"] = minutes
    save_state(state)
    return settings


def _calendar_events_for_day(target_day: date) -> list[dict] | None:
    if main.calendar_service is None:
        return None
    start = datetime.combine(target_day, time.min, tzinfo=main.TIMEZONE)
    end = start + timedelta(days=1)
    try:
        response = main.calendar_service.events().list(
            calendarId="primary", timeMin=start.isoformat(), timeMax=end.isoformat(),
            maxResults=250, singleEvents=True, orderBy="startTime",
        ).execute()
        events = []
        for item in response.get("items", []):
            start_value = item.get("start", {}).get("dateTime") or item.get("start", {}).get("date")
            end_value = item.get("end", {}).get("dateTime") or item.get("end", {}).get("date")
            if start_value:
                events.append({
                    "id": item.get("id"), "summary": item.get("summary", "(Untitled event)"),
                    "start": start_value, "end": end_value,
                    "display_start": start_value,
                    "description": item.get("description", ""),
                    "confirmed_calendar_event": True,
                })
        return events
    except Exception:
        return None


def daily_overview(day_text: str | None = None) -> dict:
    target_text = main.parse_date(day_text) if day_text else _now().date().isoformat()
    if not target_text:
        raise ValueError("Select a valid calendar date.")
    target = date.fromisoformat(target_text)
    tasks = main.load_tasks()
    tasks_on_day = [task for task in tasks if task.get("deadline") == target_text]
    outstanding_on_day = [task for task in tasks_on_day if task.get("status") != "completed"]
    completed_on_day = []
    for task in tasks:
        if task.get("status") != "completed" or not task.get("completed_at"):
            continue
        try:
            if _parse_datetime(task["completed_at"]).date() == target:
                completed_on_day.append(task)
        except (TypeError, ValueError):
            continue
    overdue = [task for task in tasks if task.get("status") != "completed"
               and task.get("deadline") and task["deadline"] < target_text]
    high_priority = [task for task in tasks if task.get("status") != "completed"
                     and task.get("priority") == "high"]
    events = _calendar_events_for_day(target)
    state = load_state()
    windows = _free_windows(target, state["settings"], events or []) if events is not None else []
    available_minutes = sum(int((end - start).total_seconds() // 60) for start, end in windows)
    latest_proposal = next((proposal for proposal in reversed(state["proposals"])
                            if proposal.get("date") == target_text and proposal.get("status") != "rejected"), None)
    sorted_tasks = sorted(outstanding_on_day + [task for task in high_priority if task not in outstanding_on_day],
                          key=lambda task: (
                              0 if task.get("priority") == "high" else 1,
                              task.get("deadline") or "9999-12-31",
                              str(task.get("title", "")).casefold(),
                          ))
    if sorted_tasks:
        next_action = sorted_tasks[0].get("title", "Review your next task")
    elif overdue:
        next_action = overdue[0].get("title", "Review overdue tasks")
    else:
        next_action = "Review today's calendar and choose a priority."
    reminders = []
    settings = state["settings"]
    if settings.get("reminders_enabled", True):
        if settings.get("remind_overdue", True):
            reminders.extend({"kind": "overdue", "task_id": task.get("id"), "title": task.get("title"),
                              "deadline": task.get("deadline")} for task in overdue)
        if settings.get("remind_due_today", True):
            reminders.extend({"kind": "due_today", "task_id": task.get("id"), "title": task.get("title"),
                              "deadline": task.get("deadline")} for task in outstanding_on_day)
    denominator = len(set(task.get("id") for task in tasks_on_day + completed_on_day))
    completed_count = len({task.get("id") for task in completed_on_day})
    return {
        "date": target_text,
        "timezone": str(main.TIMEZONE),
        "calendar_available": events is not None,
        "calendar_events": events or [],
        "calendar_tasks": tasks_on_day,
        "due_today_tasks": outstanding_on_day,
        "completed_today_tasks": completed_on_day,
        "overdue_tasks": overdue,
        "high_priority_tasks": high_priority,
        "available_minutes": available_minutes if events is not None else None,
        "availability_note": None if events is not None else "Calendar is unavailable; available time cannot be estimated.",
        "daily_proposal": latest_proposal,
        "next_recommended_action": next_action,
        "completion_percent": round(100 * completed_count / denominator) if denominator else None,
        "completion_count": completed_count,
        "completion_total": denominator,
        "reminders": reminders,
        "reminders_enabled": settings.get("reminders_enabled", True),
    }


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=main.TIMEZONE)
    return parsed.astimezone(main.TIMEZONE)


def _event_interval(event: dict, target_day: date) -> tuple[datetime, datetime] | None:
    try:
        start_value, end_value = event.get("start"), event.get("end")
        if not start_value or not end_value:
            return None
        if "T" not in start_value:
            event_day = date.fromisoformat(start_value[:10])
            end_day = date.fromisoformat(end_value[:10])
            if event_day <= target_day < end_day:
                start = datetime.combine(target_day, time.min, tzinfo=main.TIMEZONE)
                return start, start + timedelta(days=1)
            return None
        start, end = _parse_datetime(start_value), _parse_datetime(end_value)
        day_start = datetime.combine(target_day, time.min, tzinfo=main.TIMEZONE)
        day_end = day_start + timedelta(days=1)
        if start < day_end and end > day_start:
            return max(start, day_start), min(end, day_end)
    except (TypeError, ValueError):
        return None
    return None


def _free_windows(target_day: date, settings: dict, events: list[dict]) -> list[tuple[datetime, datetime]]:
    if target_day.weekday() not in settings["working_days"]:
        return []
    start = datetime.combine(target_day, time.fromisoformat(settings["work_start"]), tzinfo=main.TIMEZONE)
    end = datetime.combine(target_day, time.fromisoformat(settings["work_end"]), tzinfo=main.TIMEZONE)
    if target_day == _now().date():
        start = max(start, _now())
    occupied = sorted((interval for event in events if (interval := _event_interval(event, target_day))), key=lambda pair: pair[0])
    windows = []
    cursor = start
    for fixed_start, fixed_end in occupied:
        fixed_start, fixed_end = max(start, fixed_start), min(end, fixed_end)
        if fixed_start > cursor:
            windows.append((cursor, fixed_start))
        cursor = max(cursor, fixed_end)
    if cursor < end:
        windows.append((cursor, end))
    return [(left, right) for left, right in windows if right > left]


@_serialized
def generate_daily_plan(day_text: str | None = None, persist: bool = True) -> dict:
    target = main.parse_date(day_text) if day_text else _now().date().isoformat()
    if not target:
        raise ValueError("Date must be a specific valid date.")
    target_day = date.fromisoformat(target)
    if target_day < _now().date():
        raise ValueError("A daily plan cannot be generated for a past date.")
    state = load_state()
    settings = state["settings"]
    day_offset = (target_day - _now().date()).days
    events_result = main.list_calendar_events(min(60, max(1, day_offset + 2)))
    events_available = events_result is not None
    events = events_result or []
    fixed_today = [event for event in events if _event_interval(event, target_day)]
    windows = _free_windows(target_day, settings, fixed_today) if events_available else []
    available_minutes = sum(int((end - start).total_seconds() // 60) for start, end in windows)

    all_tasks = main.load_tasks()
    fingerprint_data = {
        "date": target,
        "settings": settings,
        "tasks": [{key: task.get(key) for key in ("id", "title", "deadline", "priority", "duration_minutes", "status", "depends_on", "project_id")}
                  for task in all_tasks],
        "events": [{key: event.get(key) for key in ("id", "summary", "start", "end")} for event in fixed_today],
        "events_available": events_available,
    }
    input_signature = hashlib.sha256(json.dumps(fingerprint_data, sort_keys=True, default=str).encode()).hexdigest()
    if persist:
        existing = next((item for item in reversed(state["proposals"])
                         if item.get("date") == target and item.get("input_signature") == input_signature
                         and item.get("status") != "rejected"), None)
        if existing:
            return existing
    completed_ids = {str(task.get("id")) for task in all_tasks if task.get("status") == "completed"}
    tasks = [task for task in all_tasks if task.get("status") != "completed"]
    tasks.sort(key=lambda task: (
        0 if task.get("deadline") and task["deadline"] < target else 1,
        0 if task.get("deadline") == target else 1,
        {"high": 0, "medium": 1, "low": 2}.get(task.get("priority", "medium"), 1),
        task.get("deadline") or "9999-12-31",
        str(task.get("title", "")).casefold(),
    ))
    blocks = []
    unscheduled = []
    cursor_index = 0
    cursor = windows[0][0] if windows else None
    work_since_break = 0
    for task in tasks:
        deadline = task.get("deadline")
        if not events_available:
            unscheduled.append({"task_id": task.get("id"), "title": task.get("title"),
                                "reason": "calendar availability cannot be verified"})
            continue
        if deadline and deadline < target:
            unscheduled.append({"task_id": task.get("id"), "title": task.get("title"), "reason": "deadline has passed"})
            continue
        dependencies = task.get("depends_on", [])
        incomplete_dependencies = [str(dep) for dep in dependencies if str(dep) not in completed_ids]
        if incomplete_dependencies:
            unscheduled.append({"task_id": task.get("id"), "title": task.get("title"), "reason": "waiting for prerequisite tasks"})
            continue
        duration = max(5, int(task.get("duration_minutes", 60)))
        placed = False
        while cursor is not None and cursor_index < len(windows):
            window_start, window_end = windows[cursor_index]
            cursor = max(cursor, window_start)
            if cursor >= window_end:
                cursor_index += 1
                cursor = windows[cursor_index][0] if cursor_index < len(windows) else None
                continue
            if settings["break_minutes"] and work_since_break >= settings["break_after_minutes"]:
                break_end = cursor + timedelta(minutes=settings["break_minutes"])
                if break_end > window_end:
                    cursor_index += 1
                    cursor = windows[cursor_index][0] if cursor_index < len(windows) else None
                    continue
                blocks.append({"id": _id(), "kind": "break", "title": "Break", "start": cursor.isoformat(),
                               "end": break_end.isoformat(), "duration_minutes": settings["break_minutes"],
                               "confirmed_calendar_event": False})
                cursor = break_end
                work_since_break = 0
            end = cursor + timedelta(minutes=duration)
            if end <= window_end:
                blocks.append({"id": _id(), "kind": "task", "task_id": task.get("id"),
                               "title": task.get("title", "Untitled"), "start": cursor.isoformat(),
                               "end": end.isoformat(), "duration_minutes": duration,
                               "priority": task.get("priority", "medium"), "status": "proposed",
                               "confirmed_calendar_event": False, "calendar_committed": False})
                remaining = end + timedelta(minutes=settings["buffer_minutes"])
                if remaining <= window_end:
                    cursor = remaining
                else:
                    cursor_index += 1
                    cursor = windows[cursor_index][0] if cursor_index < len(windows) else None
                work_since_break += duration
                placed = True
                break
            cursor_index += 1
            cursor = windows[cursor_index][0] if cursor_index < len(windows) else None
        if not placed:
            unscheduled.append({"task_id": task.get("id"), "title": task.get("title"),
                                "reason": "not enough working time remains today"})

    scheduled_minutes = sum(block["duration_minutes"] for block in blocks if block["kind"] == "task")
    overdue = [task for task in all_tasks if task.get("status") != "completed" and task.get("deadline") and task["deadline"] < target]
    due_today = [task for task in all_tasks if task.get("status") != "completed" and task.get("deadline") == target]
    high_priority = [task for task in all_tasks if task.get("status") != "completed" and task.get("priority") == "high"]
    proposal = {
        "id": _id(), "date": target, "created_at": _timestamp(), "status": "proposed",
        "input_signature": input_signature,
        "approved_at": None, "calendar_committed_at": None,
        "blocks": blocks, "unscheduled": unscheduled,
        "fixed_events": fixed_today, "events_available": events_available,
        "available_minutes": available_minutes, "scheduled_minutes": scheduled_minutes,
        "overloaded": bool(unscheduled), "overdue_tasks": overdue, "due_today_tasks": due_today,
        "high_priority_tasks": high_priority,
        "conflicts": [] if events_available else ["Calendar availability could not be verified; fixed commitments may be missing."],
        "working_hours": {"start": settings["work_start"], "end": settings["work_end"]},
    }
    if persist:
        state["proposals"].append(proposal)
        state["proposals"] = state["proposals"][-50:]
        save_state(state)
    return proposal


def list_proposals() -> list[dict]:
    return sorted(load_state()["proposals"], key=lambda item: item.get("created_at", ""), reverse=True)


@_serialized
def approve_proposal(proposal_id: str) -> dict:
    proposal = _find_record("proposals", proposal_id)
    if proposal is None:
        raise KeyError("Schedule proposal not found.")
    if proposal.get("status") == "rejected":
        raise ValueError("This schedule proposal was rejected.")
    return _replace_record("proposals", proposal_id, {"status": "approved", "approved_at": _timestamp()})


@_serialized
def reject_proposal(proposal_id: str) -> dict:
    return _replace_record("proposals", proposal_id, {"status": "rejected"})


@_serialized
def complete_proposed_task(proposal_id: str, block_id: str) -> dict:
    state = load_state()
    proposal = next((item for item in state["proposals"] if item.get("id") == proposal_id), None)
    block = next((item for item in proposal.get("blocks", []) if item.get("id") == block_id), None) if proposal else None
    if not block or block.get("kind") != "task":
        raise KeyError("Proposed task block not found.")
    task, error = main.complete_task(str(block["task_id"]))
    if error:
        raise ValueError(error)
    block["status"] = "completed"
    block["completed_at"] = _timestamp()
    save_state(state)
    return task


@_serialized
def reschedule_proposed_task(proposal_id: str, block_id: str, start_text: str) -> dict:
    state = load_state()
    proposal = next((item for item in state["proposals"] if item.get("id") == proposal_id), None)
    block = next((item for item in proposal.get("blocks", []) if item.get("id") == block_id), None) if proposal else None
    if not block or block.get("kind") != "task":
        raise KeyError("Proposed task block not found.")
    task = next((item for item in main.load_tasks() if str(item.get("id")) == str(block.get("task_id"))), None)
    if not task or task.get("status") == "completed":
        raise ValueError("Completed or missing tasks cannot be rescheduled.")
    start = _parse_datetime(start_text)
    end = start + timedelta(minutes=int(block["duration_minutes"]))
    target_day = date.fromisoformat(proposal["date"])
    if start.date() != target_day:
        raise ValueError("This proposal only supports rescheduling within its selected day.")
    if task.get("deadline") and task["deadline"] < target_day.isoformat():
        raise ValueError("The task deadline has already passed.")
    settings = load_state()["settings"]
    events = [event for event in (main.list_calendar_events(60) or []) if _event_interval(event, target_day)]
    windows = _free_windows(target_day, settings, events)
    if not any(start >= left and end <= right for left, right in windows):
        raise ValueError("The proposed time is outside working hours or conflicts with a calendar event.")
    for other in proposal["blocks"]:
        if other.get("kind") != "task" or other.get("id") == block_id:
            continue
        other_start, other_end = _parse_datetime(other["start"]), _parse_datetime(other["end"])
        if start < other_end and end > other_start:
            raise ValueError("The new time overlaps another proposed task.")
    block["start"], block["end"] = start.isoformat(), end.isoformat()
    proposal["status"] = "proposed"
    proposal["approved_at"] = None
    save_state(state)
    return proposal


@_serialized
def commit_proposal_to_calendar(proposal_id: str) -> dict:
    state = load_state()
    proposal = next((item for item in state["proposals"] if item.get("id") == proposal_id), None)
    if proposal is None:
        raise KeyError("Schedule proposal not found.")
    if proposal.get("status") != "approved":
        raise ValueError("Approve the proposed schedule before creating calendar events.")
    target_day = date.fromisoformat(proposal["date"])
    current_events = main.list_calendar_events(min(60, max(1, (target_day - _now().date()).days + 2)))
    if current_events is None:
        raise ValueError("Calendar availability is unavailable; no proposed events were created.")
    fixed_events = [event for event in current_events if _event_interval(event, target_day)]
    available_windows = _free_windows(target_day, load_state()["settings"], fixed_events)
    tasks_by_id = {str(task.get("id")): task for task in main.load_tasks()}
    for block in proposal["blocks"]:
        if block.get("kind") != "task" or block.get("calendar_committed") or block.get("status") == "completed":
            continue
        task = tasks_by_id.get(str(block.get("task_id")))
        if not task:
            raise ValueError(f"Task '{block.get('title', 'Unknown')}' no longer exists; regenerate the plan.")
        if task.get("status") == "completed":
            block["status"] = "completed"
            block["completed_at"] = _timestamp()
            continue
        if task.get("deadline") and task["deadline"] < proposal["date"]:
            raise ValueError(f"Task '{task.get('title')}' has an expired deadline; regenerate the plan.")
        block_start, block_end = _parse_datetime(block["start"]), _parse_datetime(block["end"])
        if block_end <= _now():
            raise ValueError(
                f"Approved block '{block.get('title', 'Untitled')}' has already ended. "
                "Move it to a future free slot, approve the revised time, then commit again; no calendar events were created."
            )
        if block_start < _now():
            raise ValueError(
                f"Approved block '{block.get('title', 'Untitled')}' has already started. "
                "Move it to a future free slot, approve the revised time, then commit again; no calendar events were created."
            )
        if not any(block_start >= start and block_end <= end for start, end in available_windows):
            raise ValueError(
                f"Approved block '{block.get('title', 'Untitled')}' conflicts with a calendar event or work hours. "
                "Move it to an available slot, approve the revised time, then commit again; no calendar events were created."
            )
    results = []
    for block in proposal["blocks"]:
        if block.get("kind") != "task" or block.get("status") == "completed" or block.get("calendar_committed"):
            continue
        start, end = _parse_datetime(block["start"]), _parse_datetime(block["end"])
        task_id = block.get("task_id")
        success, result = main.create_calendar_event(
            block["title"],
            start,
            end,
            f"Approved planner block for task {task_id}.",
            idempotency_key=f"schedule-proposal:{proposal_id}:{block['id']}",
        )
        results.append({"block_id": block["id"], "created": bool(success),
                        "message": "Calendar accepted the event." if success else "Calendar did not confirm the event."})
        if not success:
            save_state(state)
            raise ValueError(
                f"Google Calendar could not confirm block '{block.get('title', 'Untitled')}' "
                f"({result}). Successfully created blocks were saved; retry to continue with the remaining blocks."
            )
        block["calendar_committed"] = True
        block["confirmed_calendar_event"] = True
        save_state(state)
    proposal["calendar_committed_at"] = _timestamp() if all(
        item.get("calendar_committed") or item.get("status") == "completed"
        for item in proposal["blocks"] if item.get("kind") == "task"
    ) else None
    if proposal["calendar_committed_at"]:
        proposal["status"] = "committed"
    save_state(state)
    return {"proposal": proposal, "results": results}


def adaptive_recommendations(day_text: str | None = None) -> list[dict]:
    proposal = generate_daily_plan(day_text)
    recommendations = []
    for item in proposal["unscheduled"]:
        recommendations.append({
            "kind": "replan", "title": f"{item['title']} is not scheduled",
            "reason": item["reason"], "task_id": item["task_id"],
            "recommendation": "Review the deadline and priority, then generate a revised plan for another working day.",
        })
    return recommendations


# Weekly review and deterministic workload insights

def weekly_review(week_text: str | None = None) -> dict:
    selected = main.parse_date(week_text) if week_text else _now().date().isoformat()
    if not selected:
        raise ValueError("Select a valid date within the week to review.")
    selected_day = date.fromisoformat(selected)
    week_start = selected_day - timedelta(days=selected_day.weekday())
    week_end = week_start + timedelta(days=6)
    tasks = main.load_tasks()
    completed = []
    historical_unknown = 0
    for task in tasks:
        if task.get("status") == "completed":
            completed_at = task.get("completed_at")
            if not completed_at:
                historical_unknown += 1
                continue
            try:
                timestamp = _parse_datetime(completed_at).date()
            except ValueError:
                historical_unknown += 1
                continue
            if week_start <= timestamp <= week_end:
                completed.append(task)
    outstanding = [task for task in tasks if task.get("status") != "completed"]
    overdue = [task for task in outstanding if task.get("deadline") and task["deadline"] < _now().date().isoformat()]
    carry_forward = [task for task in outstanding if task.get("deadline") and
                     week_start.isoformat() <= task["deadline"] <= week_end.isoformat()]
    upcoming_deadlines = [task for task in outstanding if task.get("deadline") and
                          _now().date().isoformat() <= task["deadline"] <= (week_end + timedelta(days=7)).isoformat()]
    completed_minutes = sum(int(task.get("duration_minutes", 0)) for task in completed)
    due_in_week = [task for task in outstanding if task.get("deadline")
                   and week_start.isoformat() <= task["deadline"] <= week_end.isoformat()]
    weekly_scope_ids = {str(task.get("id")) for task in completed + due_in_week}
    completion_percent = round(100 * len(completed) / len(weekly_scope_ids)) if weekly_scope_ids else None
    projects = list_projects()
    project_progress = [{"id": project["id"], "name": project["name"],
                         "progress_percent": project["progress_percent"]} for project in projects]
    return {
        "week_start": week_start.isoformat(), "week_end": week_end.isoformat(),
        "completed_tasks": completed, "outstanding_tasks": outstanding,
        "overdue_tasks": overdue, "carried_forward_tasks": carry_forward,
        "upcoming_deadlines": upcoming_deadlines, "projects": project_progress,
        "completed_estimated_minutes": completed_minutes,
        "completion_percent": completion_percent,
        "completion_count": len(completed),
        "completion_total": len(weekly_scope_ids),
        "milestones_completed": [item for item in load_state()["milestones"] if item.get("completed")
                                  and item.get("completed_at", "")[:10] >= week_start.isoformat()
                                  and item.get("completed_at", "")[:10] <= week_end.isoformat()],
        "milestones_approaching": [item for item in load_state()["milestones"] if not item.get("completed")
                                   and item.get("target_date") and week_start.isoformat() <= item["target_date"] <= week_end.isoformat()],
        "history_limited": historical_unknown > 0 or not completed,
        "historical_completion_dates_missing": historical_unknown,
        "interpretation": "Recorded completion timestamps and current task state only; older task-state snapshots are unavailable.",
    }


def _next_insight_calendar_slot(task: dict, target_day: date, calendar_events: list[dict],
                                settings: dict, scheduled_tasks: list[dict],
                                proposed_blocks: list[dict]) -> dict | None:
    duration = max(5, int(task.get("duration_minutes", 60)))
    deadline = date.fromisoformat(task["deadline"]) if task.get("deadline") else None
    today = _now().date()
    for day_offset in range(60):
        candidate_day = max(target_day, today) + timedelta(days=day_offset)
        if deadline and candidate_day > deadline:
            return None
        occupied = list(calendar_events)
        for scheduled_task in scheduled_tasks:
            if str(scheduled_task.get("id")) == str(task.get("id")):
                continue
            scheduled_at = scheduled_task.get("scheduled_at")
            if not scheduled_at or scheduled_task.get("status") == "completed":
                continue
            try:
                scheduled_start = _parse_datetime(scheduled_at)
                scheduled_end = scheduled_start + timedelta(
                    minutes=max(5, int(scheduled_task.get("duration_minutes", 60)))
                )
            except (TypeError, ValueError):
                continue
            occupied.append({"start": scheduled_start.isoformat(), "end": scheduled_end.isoformat()})
        occupied.extend(proposed_blocks)
        windows = _free_windows(candidate_day, settings, occupied)
        for window_start, window_end in windows:
            start = max(window_start, _now()) if candidate_day == today else window_start
            end = start + timedelta(minutes=duration)
            if end <= window_end:
                return {"start": start.isoformat(), "end": end.isoformat(), "duration_minutes": duration}
    return None


def workload_insights(day_text: str | None = None) -> list[dict]:
    target = main.parse_date(day_text) if day_text else _now().date().isoformat()
    if not target:
        raise ValueError("Select a valid date.")
    tasks = main.load_tasks()
    pending = [task for task in tasks if task.get("status") != "completed"]
    insights = []
    overdue = [task for task in pending if task.get("deadline") and task["deadline"] < target]
    for task in overdue:
        insights.append({"id": f"overdue-{task['id']}", "kind": "overdue", "title": f"{task['title']} is overdue",
                         "records": [task], "why": "Its stored deadline is before the selected date.",
                         "recommendation": "Choose a new deadline explicitly or mark it complete if finished."})
    deadlines = {}
    for task in pending:
        if task.get("deadline"):
            deadlines.setdefault(task["deadline"], []).append(task)
    for due, group in deadlines.items():
        if len(group) > 1:
            insights.append({"id": f"deadline-{due}", "kind": "deadline_cluster",
                             "title": f"{len(group)} tasks share a deadline on {due}", "records": group,
                             "why": "Several outstanding commitments are due on the same day.",
                             "recommendation": "Review priorities and available work time before that date."})
    if target >= _now().date().isoformat():
        try:
            plan = generate_daily_plan(target, persist=False)
            task_by_id = {str(task.get("id")): task for task in pending}
            calendar_events = main.list_calendar_events(60) if plan["events_available"] else None
            for missed in plan["unscheduled"]:
                if missed.get("reason") == "not enough working time remains today":
                    task = task_by_id.get(str(missed["task_id"]))
                    if not task or task.get("scheduled_at"):
                        continue
                    slot = _next_insight_calendar_slot(
                        task,
                        date.fromisoformat(target),
                        calendar_events or [],
                        load_state()["settings"],
                        pending,
                        [block for block in plan["blocks"] if block.get("kind") == "task"],
                    ) if calendar_events is not None else None
                    insights.append({"id": f"capacity-{target}-{missed['task_id']}", "kind": "capacity",
                                     "title": f"{missed['title']} does not fit the available workday",
                                     "records": [missed],
                                     "why": f"The plan has {plan['available_minutes']} available minutes and prioritizes earlier deadlines/high priority.",
                                     "recommendation": "Move this task to the next available work slot.",
                                     "suggested_slot": slot})
        except ValueError:
            pass
    dismissed = set(load_state().get("dismissed_insights", []))
    return [item for item in insights if item["id"] not in dismissed]


@_serialized
def dismiss_insight(insight_id: str) -> dict:
    state = load_state()
    dismissed = state.setdefault("dismissed_insights", [])
    if insight_id not in dismissed:
        dismissed.append(insight_id)
    save_state(state)
    return {"id": insight_id, "dismissed": True}


@_serialized
def accept_workload_insight(insight_id: str, review_date: str, expected_start: str) -> dict:
    selected_date = main.parse_date(review_date)
    if not selected_date:
        raise ValueError("Select a valid insight review date.")
    insight = next(
        (item for item in workload_insights(selected_date) if item["id"] == insight_id),
        None,
    )
    if not insight or insight.get("kind") != "capacity":
        raise KeyError("This calendar action is no longer available. Refresh workload insights.")
    slot = insight.get("suggested_slot")
    if not slot:
        raise ValueError("No conflict-free working slot is available for this task.")
    try:
        expected = _parse_datetime(expected_start)
        suggested = _parse_datetime(slot["start"])
    except (TypeError, ValueError) as exc:
        raise ValueError("The suggested calendar time is invalid. Refresh workload insights.") from exc
    if expected != suggested:
        raise ValueError("Calendar availability changed. Refresh the insight to review the updated time.")

    task = insight["records"][0]
    task_id = task["task_id"]
    title = task["title"]
    scheduled, error = main.update_task(task_id, {"scheduled_at": slot["start"]})
    if error or not scheduled:
        raise ValueError(error or "The task could not be scheduled.")
    end = _parse_datetime(slot["end"])
    success, result = main.create_calendar_event(
        title,
        suggested,
        end,
        f"Scheduled from the accepted workload insight for task {task_id}.",
        idempotency_key=f"workload-insight:{insight_id}",
    )
    if not success:
        _, rollback_error = main.update_task(task_id, {"scheduled_at": None})
        if rollback_error:
            raise ValueError(
                f"Calendar did not confirm the event ({result}), and the task schedule could not be rolled back ({rollback_error})."
            )
        raise ValueError(f"Google Calendar could not confirm the event ({result}).")

    dismiss_insight(insight_id)
    return {
        "accepted": True,
        "task": scheduled,
        "event": result,
    }


def extract_capture(text: str) -> dict:
    text = _clean_text(text, "Capture text", 4000)
    lower = text.lower()
    duration = 60
    duration_match = re.search(r"\b(\d{1,4})\s*(minutes?|mins?|hours?|hrs?)\b", lower)
    if duration_match:
        amount = int(duration_match.group(1))
        duration = amount * 60 if duration_match.group(2).startswith(("hour", "hr")) else amount
    date_match = re.search(
        r"\b(day after tomorrow|tomorrow|today|20\d{2}-\d{2}-\d{2}|"
        r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|next\s+week|next\s+"
        r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
        r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"(?:\d{1,2}\s+)?(?:january|february|march|april|may|june|july|august|"
        r"september|october|november|december)(?:\s+\d{4})?)\b",
        text,
        re.I,
    )
    deadline = None
    if date_match:
        value = date_match.group(1)
        if value.lower() in {"tomorrow", "today", "day after tomorrow"}:
            deadline = main.parse_date(value)
        elif value and value.lower().removeprefix("next ") in {
            "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
        }:
            normalized = value.lower()
            next_weekday = normalized.startswith("next ")
            normalized = normalized.removeprefix("next ")
            days = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                    "friday": 4, "saturday": 5, "sunday": 6}
            today = _now().date()
            delta = (days[normalized] - today.weekday()) % 7 or 7
            if next_weekday and delta <= 7:
                delta += 7
            deadline = (today + timedelta(days=delta)).isoformat()
        else:
            deadline = main.parse_date(value) if value else None
    priority = "high" if re.search(r"\b(?:urgent|important|high priority)\b", lower) else "medium"
    title = text
    title = re.sub(r"^\s*(?:please\s+)?(?:i need to|remember to|remind me to|study|submit|finish|research|look into)\s*", "", title, flags=re.I)
    if duration_match:
        title = title.replace(duration_match.group(0), "")
    if date_match and deadline:
        title = title.replace(date_match.group(0), "")
    title = re.sub(r"\b(?:urgent|important|high priority)\b", "", title, flags=re.I)
    title = re.sub(r"\b(?:for|by|on|due)\s*$", "", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip(" .,:;-\t")
    return {
        "original_text": text,
        "suggested_type": "task" if re.search(r"\b(?:need to|remember to|remind me|study|submit|finish)\b", lower) else "inbox",
        "title": title or text,
        "deadline": deadline,
        "duration_minutes": duration,
        "priority": priority,
        "requires_review": not bool(title),
    }


def _project_matches(reference: str) -> list[dict]:
    target = set(re.findall(r"[a-z0-9]+", reference.casefold()))
    matches = []
    for project in list_projects():
        words = set(re.findall(r"[a-z0-9]+", project["name"].casefold()))
        score = len(target & words) / max(1, len(words))
        if words and (words <= target or target <= words or score >= 0.6):
            matches.append(project)
    return matches


def _project_chat_command(text: str) -> str | None:
    create_match = re.search(
        r"\b(?:create|start|set up)\s+(?:a\s+)?project\s+(?:for|called|named)?\s+(.+)$",
        text,
        re.I,
    )
    if create_match:
        name = re.sub(r"^(?:my|the)\s+", "", create_match.group(1), flags=re.I).strip(" .,!?")
        if len(name) < 3:
            return "What should I name the project?"
        project = create_project(name)
        return f"Project created: {project['name']}."

    milestone_match = re.search(
        r"\badd\s+(?:a\s+)?milestone\s+(?:for\s+)?(.+?)\s+to\s+(?:the\s+)?project\s+(.+)$",
        text,
        re.I,
    )
    if milestone_match:
        title, project_reference = milestone_match.group(1).strip(" .,!"), milestone_match.group(2).strip(" .,!?")
        projects = _project_matches(project_reference)
        if len(projects) > 1:
            return "Which project did you mean? " + "; ".join(project["name"] for project in projects[:5])
        if not projects:
            return "I couldn't find that project. Please give me its project name."
        milestone = create_milestone(projects[0]["id"], title)
        return f"Milestone added to {projects[0]['name']}: {milestone['title']}."

    if re.search(r"\b(?:what|which|show|list).{0,30}\b(?:tasks? remain|remaining tasks?|tasks? left)\b", text, re.I):
        named = re.search(r"\b(?:in|for)\s+(?:the\s+)?project\s+(.+)$", text, re.I)
        if not named:
            return "Which project should I check?"
        projects = _project_matches(named.group(1).strip(" .,!"))
        if len(projects) != 1:
            return "I couldn't identify one project. Please send its exact name."
        project = projects[0]
        tasks = [task for task in main.load_tasks()
                 if task.get("project_id") == project["id"] and task.get("status") != "completed"]
        if not tasks:
            return f"No outstanding tasks are recorded for {project['name']}."
        return f"Outstanding tasks in {project['name']}: " + "; ".join(task["title"] for task in tasks)
    return None


def desktop_chat_context(message: str) -> tuple[str | None, str | None]:
    """Return factual project and note context for Gemini to interpret."""
    context_lines = []
    notes = search_notes(message) if len(message.strip()) >= 3 else []
    if notes:
        context_lines.append("SAVED NOTES MATCHING THE USER'S MESSAGE:")
        context_lines.extend(f"- {note['title']}: {note['content']}" for note in notes[:5])
    projects = list_projects()
    matched_projects = [project for project in projects if any(
        word in message.casefold() for word in re.findall(r"[a-z0-9]+", project["name"].casefold()) if len(word) > 3
    )]
    if matched_projects:
        context_lines.append("MATCHING PROJECT RECORDS:")
        for project in matched_projects[:5]:
            context_lines.append(
                f"- {project['name']} | status {project['status']} | target {project.get('target_deadline') or 'not set'} "
                f"| progress {project['progress_percent']}% | remaining tasks {project['outstanding_task_count']}"
            )
            project_tasks = [
                task for task in main.load_tasks()
                if task.get("project_id") == project["id"] and task.get("status") != "completed"
            ]
            context_lines.extend(f"  Outstanding task: {task['title']}" for task in project_tasks[:10])
    return None, "\n".join(context_lines) if context_lines else None


def capture_desktop_message(text: str) -> dict | None:
    preview = extract_capture(text)
    if preview["suggested_type"] != "task":
        return None
    return create_inbox_item(text, "desktop_chat")
