"""Loopback-only HTTP API for the personal AI terminal application.

Task and calendar writes are delegated to functions in main.py. Run with:
    python api.py
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import uvicorn

import main
import gemini_service
from dotenv import load_dotenv

load_dotenv()

HOST = "127.0.0.1"
PORT = 8787
_pending_events: dict[str, dict] = {}

app = FastAPI(title="Personal AI Agent", version="1.0")

@app.on_event("startup")
def connect_calendar_on_startup():
    main.connect_google_calendar()


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:1420",
        "http://localhost:1420",
        "tauri://localhost",
        "http://tauri.localhost",
    ],
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.middleware("http")
async def loopback_only(request: Request, call_next):
    peer = request.client.host if request.client else ""
    if peer not in {"127.0.0.1", "::1", "testclient"}:
        return JSONResponse(status_code=403, content={"detail": "This API accepts loopback clients only."})
    return await call_next(request)


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    deadline: Optional[str] = None
    priority: str = "medium"
    duration_minutes: int = Field(default=60, ge=5, le=1440)


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    start: str
    duration_minutes: int = Field(default=60, ge=5, le=1440)
    description: str = Field(default="", max_length=2000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class TaskUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    deadline: Optional[str] = None
    priority: Optional[str] = None
    duration_minutes: Optional[int] = Field(default=None, ge=5, le=1440)


class CalendarEventUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    start: Optional[str] = None
    duration_minutes: Optional[int] = Field(default=None, ge=5, le=1440)
    description: Optional[str] = Field(default=None, max_length=2000)


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "calendar_connected": main.calendar_service is not None,
        "timezone": str(main.TIMEZONE),
        "gemini": gemini_service.configuration(),
    }


@app.post("/api/calendar/connect")
def calendar_connect():
    if main.connect_google_calendar():
        return {"connected": True}
    raise HTTPException(status_code=503, detail="Google Calendar is unavailable. Check local application configuration.")


chat_history: list[dict] = []


@app.post("/api/chat")
def chat(payload: ChatRequest):
    global chat_history
    message = payload.message.strip()
    action_result = None
    added = main.natural_task_add(message)
    if added:
        action_result = added
    if action_result is None:
        completed = main.natural_task_complete(message)
        if completed:
            action_result = completed

    task_context = "CURRENT PENDING TASKS:\n" + main.format_tasks(False)
    calendar_context = "CALENDAR: " + (main.tool_get_calendar_events(14) if hasattr(main, "tool_get_calendar_events") else "Calendar status unavailable")
    context = task_context + "\n" + calendar_context + ("\nBACKEND ACTION RESULT:\n" + action_result if action_result else "")
    try:
        reply = gemini_service.generate_reply(message, context, chat_history[-12:] + [{"role": "system", "content": context}])
    except gemini_service.GeminiError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    chat_history.append({"role": "user", "content": message})
    chat_history.append({"role": "assistant", "content": reply})
    chat_history = chat_history[-24:]
    return {"reply": reply, "action_taken": action_result is not None}


@app.get("/api/tasks")
def get_tasks(include_completed: bool = False):
    return {"tasks": [t for t in main.load_tasks() if include_completed or t.get("status") != "completed"]}


@app.post("/api/tasks", status_code=201)
def add_task(payload: TaskCreate):
    task, error = main.add_task(payload.title, payload.deadline, payload.priority, payload.duration_minutes)
    if error:
        raise HTTPException(status_code=400, detail=error)
    return task


@app.patch("/api/tasks/{task_id}")
def update_task(task_id: int, payload: TaskUpdate):
    task, error = main.update_task(task_id, payload.model_dump(exclude_none=True))
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
        raise HTTPException(status_code=503, detail="Google Calendar is not connected.")
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
        try:
            response = main.calendar_service.events().list(
                calendarId="primary", timeMin=start_dt.isoformat(), timeMax=end_dt.isoformat(),
                maxResults=250, singleEvents=True, orderBy="startTime"
            ).execute()
            events = []
            for item in response.get("items", []):
                start_info = item.get("start", {})
                start_text = start_info.get("dateTime") or start_info.get("date")
                if start_text:
                    events.append({
                        "id": item.get("id"), "summary": item.get("summary", "(Untitled event)"),
                        "start": start_text, "end": item.get("end", {}).get("dateTime") or item.get("end", {}).get("date"),
                        "description": item.get("description", "")
                    })
            return {"events": events}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Google Calendar could not be reached: {type(exc).__name__}")
    events = main.list_calendar_events(days)
    if events is None:
        raise HTTPException(status_code=502, detail="Google Calendar could not be reached.")
    return {"events": events}


@app.post("/api/events", status_code=201)
def create_event(payload: EventCreate):
    if main.calendar_service is None:
        raise HTTPException(status_code=503, detail="Google Calendar is not connected.")
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
        raise HTTPException(status_code=503, detail="Google Calendar is not connected.")
    try:
        start = datetime.fromisoformat(payload.start.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail="Start must be an ISO 8601 date-time.")
    if start.tzinfo is None:
        start = start.replace(tzinfo=main.TIMEZONE)
    if start < main.now_local():
        raise HTTPException(status_code=400, detail="The event cannot start in the past.")
    end = start + timedelta(minutes=payload.duration_minutes)
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
    pending = _pending_events.pop(pending_id, None)
    if pending is None:
        raise HTTPException(status_code=404, detail="Pending event not found.")
    if pending["start"] < main.now_local():
        raise HTTPException(status_code=409, detail="The proposed event is no longer in the future.")
    success, result = main.create_calendar_event(
        pending["title"], pending["start"], pending["end"], pending["description"]
    )
    if not success:
        raise HTTPException(status_code=502, detail=result)
    return {"created": True, "result": result}


def run():
    uvicorn.run(app, host=HOST, port=PORT, access_log=False, log_level="warning")


if __name__ == "__main__":
    run()
