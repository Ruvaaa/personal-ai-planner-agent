"""Secure WhatsApp Cloud API integration for the local planner backend."""

import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import urllib.error
import urllib.request
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

import gemini_service
import main
import productivity_service

load_dotenv()
logger = logging.getLogger(__name__)
WEBHOOK_PATH = "/api/whatsapp/webhook"
LEGACY_WEBHOOK_PATH = "/webhooks/whatsapp"
MAX_WEBHOOK_BYTES = 3 * 1024 * 1024
MAX_MESSAGE_CHARS = 4000


class WhatsAppError(RuntimeError):
    pass


class InvalidWebhookPayload(ValueError):
    pass


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def enabled() -> bool:
    return _env("WHATSAPP_ENABLED").lower() in {"1", "true", "yes", "on"}


def normalize_phone_number(value: str) -> str:
    value = str(value or "").strip()
    if not value or not re.fullmatch(r"[+0-9() .-]+", value):
        return ""
    digits = re.sub(r"\D", "", value)
    return digits if 7 <= len(digits) <= 15 else ""


def _allowed_numbers() -> set[str]:
    raw = _env("WHATSAPP_ALLOWED_PHONE_NUMBERS")
    return {number for item in raw.split(",") if (number := normalize_phone_number(item))}


def is_authorized_sender(sender: str) -> bool:
    normalized = normalize_phone_number(sender)
    return bool(normalized and normalized in _allowed_numbers())


def configuration_status() -> dict:
    public_url = _env("WHATSAPP_PUBLIC_WEBHOOK_URL")
    if public_url.endswith("/"):
        public_url = public_url[:-1]
    if public_url and not public_url.endswith(WEBHOOK_PATH):
        public_url = public_url + WEBHOOK_PATH
    required = {
        "access_token": bool(_env("WHATSAPP_ACCESS_TOKEN")),
        "phone_number_id": bool(_env("WHATSAPP_PHONE_NUMBER_ID")),
        "verify_token": bool(_env("WHATSAPP_VERIFY_TOKEN")),
        "app_secret": bool(_env("WHATSAPP_APP_SECRET")),
        "ngrok_proxy_token": bool(_env("WHATSAPP_NGROK_PROXY_TOKEN")),
        "allowed_phone_numbers": bool(_allowed_numbers()),
    }
    credentials_present = all(required.values())
    public_url_ready = public_url.startswith("https://")
    gemini_ready = gemini_service.configuration()["configured"]
    missing = [name for name, present in required.items() if not present]
    if not public_url_ready:
        missing.append("public_https_webhook_url")
    if not gemini_ready:
        missing.append("gemini_api_key")
    return {
        "enabled": enabled(),
        "configuration_present": credentials_present,
        "webhook_endpoint": WEBHOOK_PATH,
        "public_url_configured": public_url_ready,
        "webhook_url": public_url if public_url_ready else None,
        "gemini_configured": gemini_ready,
        "ready": enabled() and credentials_present and public_url_ready and gemini_ready,
        "missing": missing,
    }


def verify_signature(raw_body: bytes, signature_header: str | None, app_secret: str | None = None) -> bool:
    secret = (app_secret or _env("WHATSAPP_APP_SECRET")).encode("utf-8")
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    supplied = signature_header[len("sha256="):]
    if not re.fullmatch(r"[0-9a-fA-F]{64}", supplied):
        return False
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, supplied.lower())


def verify_challenge(mode: str | None, verify_token: str | None, challenge: str | None) -> str | None:
    configured = _env("WHATSAPP_VERIFY_TOKEN")
    if (enabled() and configured and mode == "subscribe" and challenge is not None
            and verify_token is not None
            and hmac.compare_digest(verify_token, configured)):
        return challenge
    return None


def parse_webhook_payload(payload: object) -> list[dict[str, str]]:
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        raise InvalidWebhookPayload("Unsupported webhook object.")
    entries = payload.get("entry")
    if not isinstance(entries, list):
        raise InvalidWebhookPayload("Webhook entry must be an array.")

    incoming: list[dict[str, str]] = []
    expected_phone_id = _env("WHATSAPP_PHONE_NUMBER_ID")
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("changes"), list):
            raise InvalidWebhookPayload("Webhook entry is malformed.")
        for change in entry["changes"]:
            if not isinstance(change, dict):
                raise InvalidWebhookPayload("Webhook change is malformed.")
            if change.get("field") != "messages":
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                raise InvalidWebhookPayload("Message webhook value is malformed.")
            messages = value.get("messages", [])
            if not isinstance(messages, list):
                raise InvalidWebhookPayload("Webhook messages must be an array.")
            if not messages:
                continue
            metadata = value.get("metadata")
            if not isinstance(metadata, dict) or not isinstance(metadata.get("phone_number_id"), str):
                raise InvalidWebhookPayload("Message webhook metadata is missing.")
            if not expected_phone_id or metadata["phone_number_id"] != expected_phone_id:
                raise InvalidWebhookPayload("Webhook was addressed to another phone number.")
            for message in messages:
                if not isinstance(message, dict):
                    raise InvalidWebhookPayload("Webhook message is malformed.")
                sender = normalize_phone_number(message.get("from", ""))
                message_id = message.get("id")
                if not sender or not isinstance(message_id, str) or not 1 <= len(message_id) <= 256:
                    raise InvalidWebhookPayload("Webhook message identity is invalid.")
                if message.get("type") != "text":
                    continue
                text_object = message.get("text")
                text = text_object.get("body") if isinstance(text_object, dict) else None
                if not isinstance(text, str) or not text.strip() or len(text) > MAX_MESSAGE_CHARS:
                    raise InvalidWebhookPayload("Webhook text message is invalid.")
                timestamp = message.get("timestamp")
                if timestamp is not None and (not isinstance(timestamp, str) or not timestamp.isdigit()):
                    raise InvalidWebhookPayload("Webhook message timestamp is invalid.")
                incoming.append({"sender": sender, "message_id": message_id, "text": text.strip(),
                                 "timestamp": timestamp or ""})
    return incoming


def _state_db_path() -> Path:
    override = _env("WHATSAPP_STATE_DB")
    if override:
        return Path(override)
    if os.name == "nt":
        state_root = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        state_root = Path(os.getenv("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state_root / "Personal AI Agent" / "whatsapp_state.sqlite3"


def _connect_db() -> sqlite3.Connection:
    path = _state_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS processed_messages (message_id TEXT PRIMARY KEY, received_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS pending_sessions (sender TEXT PRIMARY KEY, state_json TEXT NOT NULL)"
    )
    return connection


def claim_message(message_id: str) -> bool:
    with closing(_connect_db()) as connection:
        with connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO processed_messages(message_id, received_at) VALUES (?, ?)",
                (message_id, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            return cursor.rowcount == 1


def _get_pending(sender: str) -> dict | None:
    with closing(_connect_db()) as connection:
        row = connection.execute(
            "SELECT state_json FROM pending_sessions WHERE sender = ?", (sender,)
        ).fetchone()
    if not row:
        return None
    try:
        value = json.loads(row[0])
        if not isinstance(value, dict):
            return None
        created_at = datetime.fromisoformat(value.get("created_at", ""))
        if datetime.now(timezone.utc) - created_at.astimezone(timezone.utc) > timedelta(hours=24):
            _set_pending(sender, None)
            return None
        return value
    except (TypeError, json.JSONDecodeError):
        return None
    except ValueError:
        _set_pending(sender, None)
        return None


def _set_pending(sender: str, state: dict | None) -> None:
    with closing(_connect_db()) as connection:
        with connection:
            if state is None:
                connection.execute("DELETE FROM pending_sessions WHERE sender = ?", (sender,))
            else:
                state.setdefault("created_at", datetime.now(timezone.utc).isoformat())
                connection.execute(
                    "INSERT INTO pending_sessions(sender, state_json) VALUES (?, ?) "
                    "ON CONFLICT(sender) DO UPDATE SET state_json = excluded.state_json",
                    (sender, json.dumps(state)),
                )


def _send_text_message(recipient: str, text: str) -> str:
    token = _env("WHATSAPP_ACCESS_TOKEN")
    phone_id = _env("WHATSAPP_PHONE_NUMBER_ID")
    version = _env("WHATSAPP_GRAPH_API_VERSION") or "v26.0"
    if not token or not phone_id:
        raise WhatsAppError("WhatsApp sending is not configured.")
    body = json.dumps({
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "text",
        "text": {"preview_url": False, "body": text[:4000]},
    }).encode("utf-8")
    request = urllib.request.Request(
        f"https://graph.facebook.com/{version}/{phone_id}/messages",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise WhatsAppError(f"WhatsApp send failed ({type(exc).__name__}).") from exc
    except Exception as exc:
        raise WhatsAppError(f"WhatsApp send failed ({type(exc).__name__}).") from exc
    messages = result.get("messages") if isinstance(result, dict) else None
    if not isinstance(messages, list) or not messages or not isinstance(messages[0], dict):
        raise WhatsAppError("WhatsApp did not confirm message acceptance.")
    message_id = messages[0].get("id")
    if not isinstance(message_id, str) or not message_id:
        raise WhatsAppError("WhatsApp did not return a message identifier.")
    return message_id


def _normalize_tokens(value: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", value.lower())
    normalized = set()
    for word in words:
        if word.endswith("ies") and len(word) > 4:
            word = word[:-3] + "y"
        elif word.endswith("ing") and len(word) > 5:
            word = word[:-3]
        elif word.endswith("s") and len(word) > 4:
            word = word[:-1]
        normalized.add(word)
    return normalized


def _match_tasks(reference: str, include_completed: bool = False) -> tuple[list[dict], list[dict]]:
    target = _normalize_tokens(reference)
    tasks = [task for task in main.load_tasks()
             if include_completed or task.get("status") != "completed"]
    ranked = []
    for task in tasks:
        title_tokens = _normalize_tokens(str(task.get("title", "")))
        if not target or not title_tokens:
            continue
        overlap = len(target & title_tokens)
        score = overlap / max(1, len(title_tokens))
        if title_tokens <= target or target <= title_tokens:
            score = 1.0
        if score >= 0.6:
            ranked.append((score, task))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if not ranked:
        return [], []
    best_score = ranked[0][0]
    matches = [task for score, task in ranked if score == best_score]
    return matches, tasks


def _deadline_matches(text: str) -> list[tuple[int, int, str]]:
    date_words = (
        r"(?:day after tomorrow|tomorrow|today|next\s+week|next\s+"
        r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
        r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
        r"(?:\d{1,2}\s+)?(?:january|february|march|april|may|june|july|august|"
        r"september|october|november|december)(?:\s+\d{4})?)"
    )
    pattern = re.compile(r"\b(?:(?:by|on|to|due(?:\s+on)?)\s+)?(" + date_words + r")\b", re.I)
    return [(match.start(), match.end(), match.group(1)) for match in pattern.finditer(text)]


def _parse_natural_date(value: str) -> str | None:
    value = value.strip().lower()
    today = main.now_local().date()
    if value == "next week":
        return None
    next_weekday = value.startswith("next ")
    weekday = value.removeprefix("next ")
    weekdays = {name.lower(): index for index, name in enumerate(
        ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    )}
    if weekday in weekdays:
        days_ahead = (weekdays[weekday] - today.weekday()) % 7
        if next_weekday or days_ahead == 0:
            days_ahead += 7
        return (today + timedelta(days=days_ahead)).isoformat()
    return main.parse_date(value)


def _extract_deadline(text: str) -> tuple[str | None, list[tuple[int, int, str]], bool]:
    matches = _deadline_matches(text)
    if not matches:
        return None, [], False
    dates = [_parse_natural_date(item[2]) for item in matches]
    if any(value is None for value in dates) or len(set(dates)) > 1:
        return None, matches, True
    return dates[0], matches, False


def _remove_spans(text: str, spans: list[tuple[int, int, str]]) -> str:
    for start, end, _ in reversed(spans):
        text = text[:start] + " " + text[end:]
    return re.sub(r"\s+", " ", text).strip(" .,:;-\t")


def _create_task(text: str) -> str | None:
    prefix = re.match(
        r"^\s*(?:please\s+)?(?:remind\s+me\s+to|remember\s+to|"
        r"(?:add|create|make|track)\s+(?:me\s+)?(?:a\s+)?(?:new\s+)?"
        r"(?:task|todo|to-do)(?:\s+to)?\s*)",
        text,
        re.I,
    )
    if not prefix:
        return None
    deadline, matches, ambiguous = _extract_deadline(text)
    if ambiguous:
        return "Which deadline did you mean? Please send one specific date."
    title = _remove_spans(text[prefix.end():], [
        (start - prefix.end(), end - prefix.end(), token)
        for start, end, token in matches if start >= prefix.end()
    ])
    title = re.sub(r"^(?:to|that\s+i\s+need\s+to)\s+", "", title, flags=re.I)
    title = re.sub(r"\b(?:high|medium|low)\s+priority\b", "", title, flags=re.I).strip(" .,:;-\t")
    if len(title) < 3:
        return "What should I call the task?"
    priority = "high" if re.search(r"\b(?:high priority|urgent|important)\b", text, re.I) else (
        "low" if re.search(r"\blow priority\b", text, re.I) else "medium"
    )
    duration = 60
    duration_match = re.search(r"\b(\d{1,4})\s*(minutes?|mins?|hours?|hrs?)\b", text, re.I)
    if duration_match:
        duration = int(duration_match.group(1)) * (60 if duration_match.group(2).lower().startswith(("hour", "hr")) else 1)
    task, error = main.add_task(title, deadline, priority, duration)
    if error or not task:
        return "I couldn't save that task. Please check the title and deadline and try again."
    deadline_text = f" Deadline: {task['deadline']}." if task.get("deadline") else ""
    return f"Task added: {task['title']}.{deadline_text}"


def _list_tasks(completed: bool) -> str:
    tasks = main.load_tasks()
    tasks = [task for task in tasks if (task.get("status") == "completed") == completed]
    if not tasks:
        return "You have no completed tasks." if completed else "You have no outstanding tasks."
    tasks.sort(key=lambda task: (task.get("deadline") or "9999-12-31", task.get("id", 0)))
    label = "Completed" if completed else "To do"
    lines = [f"{label}: "]
    for task in tasks[:12]:
        due = f" (due {task['deadline']})" if task.get("deadline") else ""
        lines.append(f"- {task.get('title', 'Untitled')}{due}; {task.get('priority', 'medium')} priority")
    if len(tasks) > 12:
        lines.append(f"And {len(tasks) - 12} more.")
    return "\n".join(lines)


def _complete_task(text: str) -> str | None:
    match = re.search(
        r"\b(?:i\s+)?(?:have\s+)?(?:finished|completed|complete|finish|done\s+with)\s+(.+)$",
        text,
        re.I,
    )
    if not match:
        return None
    reference = re.sub(r"\b(?:studying|working\s+on)\s+", "", match.group(1), flags=re.I)
    reference = re.sub(r"\b(?:task|todo|to-do)\b", "", reference, flags=re.I).strip(" .,!?")
    matches, _ = _match_tasks(reference)
    if len(matches) > 1:
        choices = "; ".join(f"{task['title']} (#{task['id']})" for task in matches[:5])
        return f"I found a few matches. Which one did you finish? {choices}"
    if not matches:
        return "I couldn't find a matching outstanding task. What is its title?"
    task, error = main.complete_task(str(matches[0]["id"]))
    if error or not task:
        return "I couldn't mark that task complete. Please try again."
    return f"Marked complete: {task['title']}."


def _update_task_deadline(text: str) -> str | None:
    move = re.search(r"\b(?:move|reschedule|push)\s+(?:my\s+)?(.+?)\s+(?:to|until|for)\s+(.+)$", text, re.I)
    if not move:
        return None
    reference = re.sub(r"^my\s+", "", move.group(1), flags=re.I).strip(" .,!?")
    deadline, _, ambiguous = _extract_deadline(move.group(2))
    if ambiguous or not deadline:
        return "Which specific date should I move it to?"
    matches, _ = _match_tasks(reference)
    if len(matches) > 1:
        choices = "; ".join(f"{task['title']} (#{task['id']})" for task in matches[:5])
        return f"Which task should I update? {choices}"
    if not matches:
        return "I couldn't find a matching outstanding task. What is its title?"
    task, error = main.update_task(matches[0]["id"], {"deadline": deadline})
    if error or not task:
        return "I couldn't update that task. Please try again."
    return f"Moved {task['title']} to {task['deadline']}."


def _update_task_priority(text: str) -> str | None:
    update = re.search(
        r"\b(?:set|change|update)\s+(?:the\s+)?priority\s+(?:of\s+)?(?:my\s+)?(.+?)\s+"
        r"to\s+(low|medium|high)\b",
        text,
        re.I,
    )
    if not update:
        return None
    reference = update.group(1).strip(" .,!?")
    priority = update.group(2).lower()
    matches, _ = _match_tasks(reference)
    if len(matches) > 1:
        choices = "; ".join(f"{task['title']} (#{task['id']})" for task in matches[:5])
        return f"Which task should I update? {choices}"
    if not matches:
        return "I couldn't find a matching outstanding task. What is its title?"
    task, error = main.update_task(matches[0]["id"], {"priority": priority})
    if error or not task:
        return "I couldn't update that task. Please try again."
    return f"Set {task['title']} to {task['priority']} priority."


def _schedule_details(text: str) -> dict | None:
    if not re.search(r"\b(?:schedule|book|calendar|study session|meeting)\b", text, re.I):
        return None
    duration = 60
    amount_words = {"one": 1, "two": 2, "three": 3, "four": 4}
    duration_match = re.search(r"\b(\d+|one|two|three|four)[ -]?(hours?|hrs?|minutes?|mins?)\b", text, re.I)
    if duration_match:
        amount = amount_words.get(duration_match.group(1).lower(), None)
        if amount is None:
            amount = int(duration_match.group(1))
        duration = amount * (60 if duration_match.group(2).lower().startswith(("hour", "hr")) else 1)
    deadline, spans, ambiguous = _extract_deadline(text)
    time_match = re.search(r"\b(?:at\s*)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)|[01]?\d:[0-5]\d)\b", text, re.I)
    start_time = main.parse_time(time_match.group(1)) if time_match else None
    title = text
    title = re.sub(r"^\s*(?:please\s+)?(?:schedule|book)\s+(?:a\s+)?", "", title, flags=re.I)
    title = _remove_spans(title, spans)
    if time_match:
        title = re.sub(re.escape(time_match.group(0)), "", title, flags=re.I)
    if duration_match:
        title = re.sub(re.escape(duration_match.group(0)), "", title, flags=re.I)
    title = re.sub(r"\b(?:study|work)\s+session\b", "study session", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip(" .,:;-\t") or "Study session"
    return {"title": title, "duration": duration, "date": deadline,
            "time": start_time.isoformat() if start_time else None, "ambiguous": ambiguous}


def _handle_pending_schedule(sender: str, text: str) -> str | None:
    state = _get_pending(sender)
    if not state or state.get("kind") != "schedule":
        return None
    if re.fullmatch(r"\s*(?:cancel|never mind|nevermind)\s*[.!]?\s*", text, re.I):
        _set_pending(sender, None)
        return "Okay, I cancelled that calendar request."
    if not state.get("date"):
        date_value, _, ambiguous = _extract_deadline(text)
        if ambiguous:
            return "Please send one specific date for the event."
        if date_value:
            state["date"] = date_value
    if not state.get("time"):
        time_match = re.search(r"\b(?:at\s*)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)|[01]?\d:[0-5]\d)\b", text, re.I)
        parsed_time = main.parse_time(time_match.group(1)) if time_match else None
        if parsed_time:
            state["time"] = parsed_time.isoformat()
    if not state.get("date"):
        _set_pending(sender, state)
        return "What date should I use?"
    if not state.get("time"):
        _set_pending(sender, state)
        return "What time should the event start?"
    if main.calendar_service is None:
        return "Google Calendar is unavailable right now, so I couldn't create the event."
    start = datetime.combine(date.fromisoformat(state["date"]), time.fromisoformat(state["time"]), tzinfo=main.TIMEZONE)
    if start < main.now_local():
        _set_pending(sender, None)
        return "That start time has passed. Send a future date and time to try again."
    success, result = main.create_calendar_event(
        state["title"], start, start + timedelta(minutes=state["duration"]), ""
    )
    if not success:
        _set_pending(sender, None)
        return "Google Calendar couldn't create that event. Nothing was added."
    _set_pending(sender, None)
    return f"Scheduled {state['title']} for {start.strftime('%A, %d %B at %I:%M %p')} (Africa/Harare)."


def _schedule_event(sender: str, text: str) -> str | None:
    details = _schedule_details(text)
    if details is None:
        return None
    if details["ambiguous"]:
        return "Which date should I use? Please send one specific date."
    state = {"kind": "schedule", "title": details["title"], "duration": details["duration"],
             "date": details["date"], "time": details["time"]}
    _set_pending(sender, state)
    return _handle_pending_schedule(sender, "")


def _assistant_reply(text: str) -> str:
    try:
        context = main.build_planning_context(7)
        import api
        return api.generate_assistant_reply(
            text,
            "Use this verified task and calendar snapshot as factual context:\n" + context,
            [],
        )
    except gemini_service.GeminiError:
        pending = [task for task in main.load_tasks() if task.get("status") != "completed"]
        pending.sort(key=lambda task: (task.get("deadline") or "9999-12-31", task.get("id", 0)))
        if pending:
            tasks = "; ".join(task.get("title", "Untitled") for task in pending[:5])
            return f"I can't reach Gemini right now. Your next outstanding tasks are: {tasks}."
        return "I can't reach Gemini right now. Your task and calendar data are unchanged."


def process_incoming_message(sender: str, message_id: str, text: str, timestamp: str = "") -> str:
    if not enabled() or normalize_phone_number(sender) not in _allowed_numbers():
        return ""
    received_at = None
    if timestamp:
        try:
            received_at = datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            received_at = None
    inbox_item = productivity_service.create_inbox_item(text, "whatsapp", message_id, received_at=received_at)
    reply = _handle_pending_schedule(sender, text)
    if reply is None:
        reply = productivity_service._project_chat_command(text)
    if reply is None:
        reply = _create_task(text)
    if reply is None and re.search(r"\b(?:what|show|list|check|view).{0,30}\b(?:completed|finished|done)\b|\bcompleted tasks\b", text, re.I):
        reply = _list_tasks(True)
    if reply is None and re.search(r"\b(?:what|show|list|check|view).{0,30}\b(?:tasks?|to-dos?|finish|outstanding)\b|\bwhat do i still need to finish\b", text, re.I):
        reply = _list_tasks(False)
    if reply is None:
        reply = _complete_task(text)
    if reply is None:
        reply = _update_task_deadline(text)
    if reply is None:
        reply = _update_task_priority(text)
    if reply is None and re.search(r"\b(?:calendar|events|schedule)\b", text, re.I) and not re.search(r"\b(?:schedule|book)\b", text, re.I):
        events = main.list_calendar_events(14)
        if events is None:
            reply = "Google Calendar is unavailable right now."
        elif not events:
            reply = "There are no upcoming calendar events in the next two weeks."
        else:
            reply = "Upcoming events:\n" + "\n".join(
                f"- {event.get('display_start', event.get('start'))}: {event.get('summary', 'Untitled')}"
                for event in events[:8]
            )
    if reply is None:
        reply = _schedule_event(sender, text)
    if reply is None:
        reply = _assistant_reply(text)
    if reply and not reply.startswith(("What ", "Which ", "I couldn't", "Google Calendar is unavailable")):
        productivity_service.set_inbox_status(inbox_item["id"], "processed")
    try:
        _send_text_message(sender, reply)
    except WhatsAppError as exc:
        logger.warning("WhatsApp reply send failed for message %s (%s)", message_id, type(exc).__name__)
    return reply


def enqueue_verified_messages(payload: object, background_tasks) -> int:
    messages = parse_webhook_payload(payload)
    accepted = 0
    allowlist = _allowed_numbers()
    for message in messages:
        if message["sender"] not in allowlist:
            continue
        if claim_message(message["message_id"]):
            background_tasks.add_task(
                process_incoming_message,
                message["sender"], message["message_id"], message["text"], message.get("timestamp", ""),
            )
            accepted += 1
    return accepted
