import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
import gemini_service
import productivity_service


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(api.app)

    def test_health_is_safe(self):
        response = self.client.get('/api/health')
        self.assertEqual(response.status_code, 200)
        body = response.text.lower()
        self.assertNotIn('client_secret', body)
        self.assertEqual(response.json()['timezone'], 'Africa/Harare')
        self.assertIn('credentials_present', response.json()['calendar'])
        self.assertEqual(
            response.json()['calendar']['credentials_location'],
            str(api.main.CREDENTIALS_FILE),
        )

    def test_calendar_startup_does_not_launch_oauth(self):
        with tempfile.TemporaryDirectory() as directory:
            missing_credentials = Path(directory) / "credentials.json"
            missing_token = Path(directory) / "token.json"
            with patch.object(api.main, "CREDENTIALS_FILE", missing_credentials), \
                    patch.object(api.main, "TOKEN_FILE", missing_token), \
                    patch.object(api.main.InstalledAppFlow, "from_client_secrets_file") as oauth_flow:
                self.assertFalse(api.main.connect_google_calendar(interactive=False))
            oauth_flow.assert_not_called()
            self.assertIsNone(api.main.calendar_service)

    def test_calendar_refresh_persists_the_reauthorized_token(self):
        class FakeCredentials:
            token = "old-token"
            expired = True
            refresh_token = "refresh-token"
            valid = True

            def has_scopes(self, _scopes):
                return True

            def refresh(self, _request):
                self.token = "refreshed-token"
                self.expired = False

            def to_json(self):
                return json.dumps({"token": self.token})

        with tempfile.TemporaryDirectory() as directory:
            credentials_file = Path(directory) / "credentials.json"
            token_file = Path(directory) / "token.json"
            credentials_file.write_text("{}", encoding="utf-8")
            token_file.write_text("{}", encoding="utf-8")
            fake_credentials = FakeCredentials()
            fake_events = SimpleNamespace(
                list=lambda **_kwargs: SimpleNamespace(execute=lambda: {"items": []})
            )
            fake_service = SimpleNamespace(events=lambda: fake_events)
            with patch.object(api.main, "CREDENTIALS_FILE", credentials_file), \
                    patch.object(api.main, "TOKEN_FILE", token_file), \
                    patch.object(api.main.Credentials, "from_authorized_user_file", return_value=fake_credentials), \
                    patch.object(api.main, "build", return_value=fake_service), \
                    patch.object(api.main, "calendar_service", None), \
                    patch.object(api.main, "calendar_credentials", None), \
                    patch.object(api.main, "calendar_connection_error", None), \
                    patch.object(api.main, "_calendar_saved_token", None):
                self.assertTrue(api.main.connect_google_calendar(interactive=False))

            self.assertEqual(json.loads(token_file.read_text(encoding="utf-8")), {
                "token": "refreshed-token",
            })

    def test_tasks_read_through_python(self):
        task = {'id': 1, 'title': 'API test', 'status': 'pending'}
        with patch.object(api.main, 'load_tasks', return_value=[task]):
            response = self.client.get('/api/tasks')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['tasks'], [task])

    def test_task_write_delegates_to_python(self):
        task = {'id': 4, 'title': 'Write test', 'status': 'pending'}
        with patch.object(api.main, 'add_task', return_value=(task, None)) as add_task:
            response = self.client.post('/api/tasks', json={'title': 'Write test'})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), task)
        add_task.assert_called_once()

    def test_scheduled_task_timestamp_is_separate_from_deadline_and_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            task_file = Path(directory) / "tasks.json"
            with patch.object(api.main, "TASKS_FILE", task_file):
                created = self.client.post("/api/tasks", json={
                    "title": "Scheduled work",
                    "deadline": "2026-11-06",
                    "scheduled_at": "2026-11-04T09:30",
                    "description": "Work session",
                })
                self.assertEqual(created.status_code, 201)
                task = created.json()
                self.assertEqual(task["deadline"], "2026-11-06")
                self.assertEqual(task["scheduled_at"], "2026-11-04T09:30:00+02:00")
                self.assertNotEqual(task["scheduled_at"][:10], task["deadline"])

                task_id = task["id"]
                changed = self.client.patch(f"/api/tasks/{task_id}", json={
                    "scheduled_at": "2026-11-05T10:15",
                })
                self.assertEqual(changed.status_code, 200)
                self.assertEqual(changed.json()["deadline"], "2026-11-06")
                self.assertEqual(changed.json()["scheduled_at"], "2026-11-05T10:15:00+02:00")
                cleared = self.client.patch(f"/api/tasks/{task_id}", json={"scheduled_at": None})
                self.assertEqual(cleared.status_code, 200)
                self.assertIsNone(cleared.json()["scheduled_at"])
                self.assertEqual(json.loads(task_file.read_text(encoding="utf-8"))[0]["deadline"], "2026-11-06")

    def test_invalid_scheduled_task_timestamp_is_rejected(self):
        response = self.client.post("/api/tasks", json={
            "title": "Invalid schedule",
            "scheduled_at": "not-a-date",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("ISO 8601", response.json()["detail"])

    def test_calendar_range_returns_event_details_and_fetches_all_pages(self):
        requests = []
        pages = [
            {
                "items": [{
                    "id": "event-1",
                    "summary": "Planning meeting",
                    "start": {"dateTime": "2026-11-04T09:00:00+02:00"},
                    "end": {"dateTime": "2026-11-04T10:00:00+02:00"},
                    "description": "Review the roadmap",
                    "location": "Conference room",
                    "htmlLink": "https://calendar.google.com/event-1",
                }],
                "nextPageToken": "next-page",
            },
            {
                "items": [{
                    "id": "event-2",
                    "summary": "Holiday",
                    "start": {"date": "2026-11-05"},
                    "end": {"date": "2026-11-06"},
                }],
            },
        ]

        class FakeEvents:
            def list(self, **kwargs):
                requests.append(kwargs)
                return SimpleNamespace(execute=lambda: pages[len(requests) - 1])

        with patch.object(api.main, "calendar_service", SimpleNamespace(events=lambda: FakeEvents())):
            response = self.client.get("/api/events", params={
                "start": "2026-11-01T00:00:00+02:00",
                "end": "2026-12-13T00:00:00+02:00",
            })

        self.assertEqual(response.status_code, 200)
        events = response.json()["events"]
        self.assertEqual([event["id"] for event in events], ["event-1", "event-2"])
        self.assertEqual(events[0]["location"], "Conference room")
        self.assertEqual(events[0]["htmlLink"], "https://calendar.google.com/event-1")
        self.assertEqual(events[1]["start"], "2026-11-05")
        self.assertEqual(requests[1]["pageToken"], "next-page")

    def test_calendar_create_uses_stable_google_event_id_and_returns_confirmed_event(self):
        start = api.main.now_local() + timedelta(days=2)
        end = start + timedelta(hours=1)
        created = {
            "id": "0123456789abcdefghijklmnopqrstuv0123456789abcdefghijkl",
            "summary": "Calendar verification",
            "description": "Test",
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
            "htmlLink": "https://calendar.google.com/calendar/event?eid=verified",
        }
        insert_bodies = []

        class FakeRequest:
            def __init__(self, value):
                self.value = value

            def execute(self):
                if isinstance(self.value, Exception):
                    raise self.value
                return self.value

        class Conflict(Exception):
            resp = SimpleNamespace(status=409)

        class FakeEvents:
            def insert(self, **kwargs):
                insert_bodies.append(kwargs["body"])
                return FakeRequest(created if len(insert_bodies) == 1 else Conflict())

            def get(self, **_kwargs):
                return FakeRequest(created)

        service = SimpleNamespace(events=lambda: FakeEvents())
        with patch.object(api.main, "calendar_service", service):
            first = api.main.create_calendar_event(
                "Calendar verification", start, end, "Test", idempotency_key="test-confirmation-1"
            )
            retry = api.main.create_calendar_event(
                "Calendar verification", start, end, "Test", idempotency_key="test-confirmation-1"
            )

        self.assertTrue(first[0])
        self.assertTrue(retry[0])
        self.assertEqual(first[1]["id"], created["id"])
        self.assertEqual(first[1]["htmlLink"], created["htmlLink"])
        self.assertEqual(insert_bodies[0]["id"], insert_bodies[1]["id"])
        self.assertGreaterEqual(len(insert_bodies[0]["id"]), 5)
        self.assertTrue(all(character in "0123456789abcdefghijklmnopqrstuv" for character in insert_bodies[0]["id"]))
        self.assertEqual(insert_bodies[0]["start"]["timeZone"], "Africa/Harare")
        self.assertEqual(insert_bodies[0]["end"]["timeZone"], "Africa/Harare")

    def test_calendar_create_does_not_claim_success_without_verifiable_google_event(self):
        start = api.main.now_local() + timedelta(days=2)
        end = start + timedelta(hours=1)

        class FakeRequest:
            def execute(self):
                return {"summary": "Missing event ID"}

        class FakeEvents:
            def insert(self, **_kwargs):
                return FakeRequest()

            def get(self, **_kwargs):
                return FakeRequest()

        with patch.object(api.main, "calendar_service", SimpleNamespace(events=lambda: FakeEvents())):
            success, result = api.main.create_calendar_event("Missing ID", start, end)

        self.assertFalse(success)
        self.assertIn("verifiable event", result)

    def test_calendar_idempotency_conflict_with_different_event_is_not_success(self):
        start = api.main.now_local() + timedelta(days=2)
        end = start + timedelta(hours=1)

        class FakeRequest:
            def __init__(self, value):
                self.value = value

            def execute(self):
                if isinstance(self.value, Exception):
                    raise self.value
                return self.value

        class Conflict(Exception):
            resp = SimpleNamespace(status=409)

        different_event = {
            "id": "existing-event",
            "summary": "Different event",
            "description": "",
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }
        fake_events = SimpleNamespace(
            insert=lambda **_kwargs: FakeRequest(Conflict()),
            get=lambda **_kwargs: FakeRequest(different_event),
        )
        with patch.object(api.main, "calendar_service", SimpleNamespace(events=lambda: fake_events)):
            success, result = api.main.create_calendar_event(
                "Requested event", start, end, idempotency_key="same-request"
            )

        self.assertFalse(success)
        self.assertIn("different event details", result)

    def test_frontend_cors_preflight_allows_used_mutation_methods(self):
        for path, method in (("/api/planner/settings", "PUT"), ("/api/tasks/1", "DELETE")):
            with self.subTest(method=method):
                response = self.client.options(path, headers={
                    "Origin": "http://127.0.0.1:1420",
                    "Access-Control-Request-Method": method,
                    "Access-Control-Request-Headers": "content-type",
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers.get("access-control-allow-origin"), "http://127.0.0.1:1420")
                self.assertIn(method, response.headers.get("access-control-allow-methods", ""))

    def test_task_crud_and_completion_persist_through_api(self):
        with tempfile.TemporaryDirectory() as directory:
            task_file = Path(directory) / "tasks.json"
            with patch.object(api.main, "TASKS_FILE", task_file):
                created = self.client.post("/api/tasks", json={"title": "Persist this task"})
                self.assertEqual(created.status_code, 201)
                task_id = created.json()["id"]

                updated = self.client.patch(f"/api/tasks/{task_id}", json={"priority": "high"})
                self.assertEqual(updated.status_code, 200)
                self.assertEqual(updated.json()["priority"], "high")

                completed = self.client.patch(f"/api/tasks/{task_id}/complete")
                self.assertEqual(completed.status_code, 200)
                self.assertEqual(completed.json()["status"], "completed")
                self.assertTrue(task_file.is_file())

                self.assertEqual(self.client.get("/api/tasks?include_completed=true").json()["tasks"][0]["status"], "completed")
                deleted = self.client.delete(f"/api/tasks/{task_id}")
                self.assertEqual(deleted.status_code, 200)
                self.assertEqual(self.client.get("/api/tasks?include_completed=true").json()["tasks"], [])

    def test_planner_settings_put_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(productivity_service, "STATE_FILE", Path(directory) / "productivity.json"):
                response = self.client.put("/api/planner/settings", json={"work_start": "08:30", "work_end": "16:30"})
                self.assertEqual(response.status_code, 200)
                settings = self.client.get("/api/planner/settings")
                self.assertEqual(settings.status_code, 200)
                self.assertEqual(settings.json()["work_start"], "08:30")
                self.assertEqual(settings.json()["work_end"], "16:30")

    def test_chat_does_not_run_interactive_cli_agent_when_gemini_is_unconfigured(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('GEMINI_API_KEY', None)
            with patch.object(api.main, 'natural_task_add', return_value=None), \
                 patch.object(api.main, 'natural_task_complete', return_value=None), \
                 patch.object(api.main, 'agent_chat') as agent_chat:
                response = self.client.post('/api/chat', json={'message': 'Hey, how are you?'})
        self.assertEqual(response.status_code, 503)
        self.assertIn('GEMINI_API_KEY', response.json()['detail'])
        agent_chat.assert_not_called()

    def test_missing_gemini_key_returns_clear_error(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('GEMINI_API_KEY', None)
            with patch.object(api.main, 'natural_task_add', return_value=None), \
                 patch.object(api.main, 'natural_task_complete', return_value=None):
                response = self.client.post('/api/chat', json={'message': 'Hello'})
        self.assertEqual(response.status_code, 503)
        self.assertIn('GEMINI_API_KEY', response.json()['detail'])

    def test_chat_generates_reviewable_daily_plan_action(self):
        proposal = {
            "id": "plan-123", "date": "2027-01-05",
            "blocks": [{"kind": "task", "title": "Finish report"}],
            "scheduled_minutes": 60,
        }
        with patch.object(api, "chat_history", []), \
                patch.object(api.productivity_service, "desktop_chat_context", return_value=(None, None)), \
                patch.object(api.main, "natural_task_add", return_value=None), \
                patch.object(api.main, "natural_task_complete", return_value=None), \
                patch.object(api.main, "format_tasks", return_value=""), \
                patch.object(api.main, "tool_get_calendar_events", return_value=""), \
                patch.object(gemini_service, "configuration", return_value={"configured": True}), \
                patch.object(gemini_service, "generate_agent_command", return_value={
                    "reply": "I will plan your day.", "action": {"type": "generate_plan", "date": "2027-01-05"},
                }), \
                patch.object(api.productivity_service, "generate_daily_plan", return_value=proposal) as generate_plan:
            response = self.client.post("/api/chat", json={"message": "Plan my day on January 5"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["action"], {
            "type": "review_plan", "proposal_id": "plan-123", "date": "2027-01-05",
        })
        self.assertIn("nothing has been added to Google Calendar", response.json()["reply"])
        generate_plan.assert_called_once_with("2027-01-05")

    def test_chat_event_proposal_waits_for_explicit_confirmation(self):
        future = api.main.now_local().replace(hour=9, minute=0, second=0, microsecond=0)
        if future <= api.main.now_local():
            future += timedelta(days=1)
        with patch.object(api, "chat_history", []), \
                patch.object(api.productivity_service, "desktop_chat_context", return_value=(None, None)), \
                patch.object(api.main, "natural_task_add", return_value=None), \
                patch.object(api.main, "natural_task_complete", return_value=None), \
                patch.object(api.main, "format_tasks", return_value=""), \
                patch.object(api.main, "tool_get_calendar_events", return_value=""), \
                patch.object(gemini_service, "generate_agent_command", return_value={
                    "reply": "Preparing the event.", "action": {
                        "type": "propose_event", "title": "Project meeting",
                        "date": future.date().isoformat(), "start_time": future.strftime("%H:%M"),
                        "duration_minutes": 30,
                    },
                }) as generate_command, \
                patch.object(api.main, "calendar_service", object()), \
                patch.object(api, "get_events", return_value={"events": []}), \
                patch.object(api.main, "find_conflicts", return_value=[]), \
                patch.object(api.main, "create_calendar_event") as create_event:
            response = self.client.post("/api/chat", json={"message": "Add a project meeting"})
            pending_id = response.json().get("action", {}).get("pending_id")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(pending_id)
            create_event.assert_not_called()
            with patch.object(api.main, "now_local", return_value=future - timedelta(days=1)), \
                    patch.object(api.main, "create_calendar_event", return_value=(True, {"id": "created"})) as confirmed:
                confirmation = self.client.post(f"/api/events/{pending_id}/confirm")
        self.assertEqual(confirmation.status_code, 200)
        confirmed.assert_called_once()
        self.assertEqual(
            confirmed.call_args.kwargs["idempotency_key"],
            f"chat-proposal:{pending_id}",
        )
        self.assertTrue(confirmation.json()["created"])
        self.assertEqual(confirmation.json()["event"]["id"], "created")

    def test_event_chat_parser_understands_date_time_and_clarification_history(self):
        original_request = "Can you add an event on Monday 2 November 2026 for a test at 2pm"
        direct = api._parse_explicit_event_action(original_request, [])
        self.assertEqual(direct, {
            "type": "propose_event",
            "title": "test",
            "date": "2026-11-02",
            "start_time": "14:00",
            "duration_minutes": 60,
        })
        history = [
            {"role": "user", "content": original_request},
            {"role": "assistant", "content": "Please provide the event title, date, and start time so I can prepare it."},
        ]
        clarified = api._parse_explicit_event_action(
            "The date is Monday 2 November, start time is 2-m and event title is Research methods test",
            history,
        )
        self.assertEqual(clarified, {
            "type": "propose_event",
            "title": "Research methods test",
            "date": "2026-11-02",
            "start_time": "14:00",
            "duration_minutes": 60,
        })

    def test_event_chat_request_with_called_title_uses_gemini_before_confirmation(self):
        api.chat_history = []
        with patch.object(api.main, "calendar_service", object()), \
                patch.object(api.main, "tool_get_calendar_events", return_value="No upcoming events."), \
                patch.object(api.main, "format_tasks", return_value=""), \
                patch.object(api.productivity_service, "desktop_chat_context", return_value=(None, None)), \
                patch.object(api, "get_events", return_value={"events": []}), \
                patch.object(api.main, "find_conflicts", return_value=[]), \
                patch.object(api.gemini_service, "generate_agent_command", return_value={
                    "reply": "I found a suitable time.", "action": {
                        "type": "propose_event", "title": "QA chat verification",
                        "date": "2030-11-02", "start_time": "10:00", "duration_minutes": 30,
                    },
                }) as generate_command, \
                patch.object(api.main, "create_calendar_event") as create_event:
            response = self.client.post("/api/chat", json={
                "message": (
                    "Please schedule a test event called QA chat verification "
                    "for November 2, 2030 at 10:00 AM for 30 minutes."
                ),
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["action"]["type"], "confirm_event")
        self.assertEqual(response.json()["action"]["event"]["title"], "QA chat verification")
        generate_command.assert_called_once()
        create_event.assert_not_called()
        api._pending_events.pop(response.json()["action"]["pending_id"], None)
        api.chat_history = []

    def test_chat_uses_gemini_for_task_actions_instead_of_local_regex_shortcuts(self):
        task = {"id": 47, "title": "Study deep learning", "status": "pending"}
        with patch.object(api, "chat_history", []), \
                patch.object(api.productivity_service, "desktop_chat_context", return_value=(None, None)), \
                patch.object(api.main, "format_tasks", return_value=""), \
                patch.object(api.main, "tool_get_calendar_events", return_value="Calendar available."), \
                patch.object(api.main, "natural_task_add") as natural_add, \
                patch.object(api.main, "natural_task_complete") as natural_complete, \
                patch.object(api.main, "add_task", return_value=(task, None)) as add_task, \
                patch.object(gemini_service, "generate_agent_command", return_value={
                    "reply": "I will add that task.", "action": {
                        "type": "add_task", "title": "Study deep learning", "duration_minutes": 60,
                    },
                }) as generate_command:
            response = self.client.post("/api/chat", json={"message": "Add a task to study deep learning"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("Added task [47]", response.json()["reply"])
        generate_command.assert_called_once()
        natural_add.assert_not_called()
        natural_complete.assert_not_called()
        add_task.assert_called_once()

    def test_gemini_event_action_finds_a_free_afternoon_slot_before_asking_approval(self):
        current = datetime(2026, 10, 30, 10, 0, tzinfo=api.main.TIMEZONE)
        blocked_event = {
            "summary": "Meeting",
            "start": "2026-11-02T13:00:00+02:00",
            "end": "2026-11-02T14:00:00+02:00",
        }
        scheduled_task = {
            "id": 20,
            "title": "Existing focus block",
            "status": "pending",
            "scheduled_at": "2026-11-02T12:00:00+02:00",
            "duration_minutes": 30,
        }
        with patch.object(api, "chat_history", []), \
                patch.object(api.productivity_service, "desktop_chat_context", return_value=(None, None)), \
                patch.object(api.main, "format_tasks", return_value=""), \
                patch.object(api.main, "tool_get_calendar_events", return_value="Calendar available."), \
                patch.object(api.main, "now_local", return_value=current), \
                patch.object(api.main, "load_tasks", return_value=[scheduled_task]), \
                patch.object(api.main, "calendar_service", object()), \
                patch.object(api, "get_events", return_value={"events": [blocked_event]}), \
                patch.object(api.productivity_service, "load_state", return_value={
                    "settings": {
                        "work_start": "09:00", "work_end": "17:00", "working_days": [0, 1, 2, 3, 4],
                    },
                }), \
                patch.object(gemini_service, "generate_agent_command", return_value={
                    "reply": "I found a free afternoon slot.", "action": {
                        "type": "propose_event",
                        "title": "Deep learning",
                        "date": "2026-11-02",
                        "duration_minutes": 120,
                        "time_preference": "afternoon",
                    },
                }) as generate_command, \
                patch.object(api.main, "create_calendar_event") as create_event:
            response = self.client.post("/api/chat", json={
                "message": "Schedule two hours for deep learning tomorrow afternoon",
            })

        self.assertEqual(response.status_code, 200)
        action = response.json()["action"]
        self.assertEqual(action["type"], "confirm_event")
        self.assertEqual(action["event"]["start"], "2026-11-02T14:00:00+02:00")
        self.assertEqual(action["event"]["end"], "2026-11-02T16:00:00+02:00")
        generate_command.assert_called_once()
        create_event.assert_not_called()
        api._pending_events.pop(action["pending_id"], None)
        api.chat_history = []

    def test_notes_can_be_created_listed_updated_and_deleted_through_api(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(productivity_service, "STATE_FILE", Path(directory) / "productivity.json"):
            created = self.client.post("/api/notes", json={
                "title": "API note verification", "content": "Note body for persistence verification.",
            })
            self.assertEqual(created.status_code, 201)
            note_id = created.json()["id"]
            notes = self.client.get("/api/notes")
            self.assertEqual(notes.status_code, 200)
            self.assertEqual(notes.json()["notes"][0]["title"], "API note verification")
            updated = self.client.patch(f"/api/notes/{note_id}", json={"content": "Updated note body."})
            self.assertEqual(updated.status_code, 200)
            self.assertEqual(updated.json()["content"], "Updated note body.")
            deleted = self.client.delete(f"/api/notes/{note_id}")
            self.assertEqual(deleted.status_code, 200)
            self.assertEqual(self.client.get("/api/notes").json()["notes"], [])

    def test_accept_workload_insight_route_passes_reviewed_slot(self):
        expected = {"accepted": True, "event": {"title": "Study", "start": "2026-10-30T09:00:00+02:00"}}
        with patch.object(productivity_service, "accept_workload_insight", return_value=expected) as accept:
            response = self.client.post(
                "/api/insights/capacity-2026-10-29-2/accept",
                json={"review_date": "2026-10-29", "expected_start": "2026-10-30T09:00:00+02:00"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        accept.assert_called_once_with(
            "capacity-2026-10-29-2", "2026-10-29", "2026-10-30T09:00:00+02:00"
        )

    def test_gemini_default_model_uses_verified_model(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('GEMINI_MODEL', None)
            self.assertEqual(gemini_service.configuration()['model'], 'gemini-3.8-flash')

    def test_gemini_model_can_be_changed_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(gemini_service, "DATA_DIR", Path(directory)), \
                patch.dict(os.environ, {"GEMINI_MODEL": "gemini-3.8-flash"}, clear=False):
            response = self.client.put(
                "/api/gemini/model",
                json={"model": "gemini-3.1-flash-lite"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["model"], "gemini-3.1-flash-lite")
            self.assertEqual(gemini_service._resolve_model_name(), "gemini-3.1-flash-lite")
            saved = json.loads((Path(directory) / gemini_service.MODEL_SETTINGS_FILE).read_text(encoding="utf-8"))
            self.assertEqual(saved, {"model": "gemini-3.1-flash-lite"})

    def test_gemini_model_rejects_unknown_model(self):
        response = self.client.put("/api/gemini/model", json={"model": "unknown-model"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Choose one of the available Gemini models.")

    def test_gemini_filters_internal_thought_blocks(self):
        with patch.dict(os.environ, {
            'GEMINI_API_KEY': 'test-key',
            'GEMINI_MODEL': 'gemini-2.5-flash',
        }, clear=False):
            fake_client = SimpleNamespace()
            fake_response = SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
                SimpleNamespace(thought=True, text='internal reasoning'),
                SimpleNamespace(thought=False, text='Final answer only.')
            ]))])
            requests = []
            fake_client.models = SimpleNamespace(
                generate_content=lambda **kwargs: (requests.append(kwargs), fake_response)[1]
            )
            with patch.object(gemini_service.genai, 'Client', return_value=fake_client):
                answer = gemini_service.generate_reply('What should I do today?', 'verified task data', [])
        self.assertEqual(answer, 'Final answer only.')
        self.assertEqual(requests[0]['model'], 'gemini-2.5-flash')
        sent_text = '\n'.join(
            part.text
            for content in requests[0]['contents']
            for part in content.parts
        )
        self.assertIn('verified task data', sent_text)
        self.assertIn('What should I do today?', sent_text)

    def test_gemini_agent_command_requests_and_parses_structured_output(self):
        response_data = {
            "reply": "I drafted the plan.",
            "action": {"type": "generate_plan", "date": "2027-01-05"},
        }
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=False):
            fake_client = SimpleNamespace()
            fake_response = SimpleNamespace(text=json.dumps(response_data))
            requests = []
            fake_client.models = SimpleNamespace(
                generate_content=lambda **kwargs: (requests.append(kwargs), fake_response)[1]
            )
            with patch.object(gemini_service.genai, "Client", return_value=fake_client):
                result = gemini_service.generate_agent_command("Plan my day", "task context", [])
        self.assertEqual(result, response_data)
        self.assertEqual(requests[0]["config"].response_mime_type, "application/json")
        self.assertIsNotNone(requests[0]["config"].response_schema)
        self.assertIn("date", gemini_service.AGENT_RESPONSE_SCHEMA["properties"]["action"]["required"])
        self.assertIn("never omit the date for propose_event", requests[0]["config"].system_instruction)
        sent_text = "\n".join(
            part.text
            for content in requests[0]["contents"]
            for part in content.parts
        )
        self.assertIn("task context", sent_text)
        self.assertIn("Plan my day", sent_text)

    def test_gemini_provider_errors_are_actionable_and_redacted(self):
        invalid_key = gemini_service._provider_error_message(SimpleNamespace(code=403, message='secret-key leak'))
        missing_model = gemini_service._provider_error_message(SimpleNamespace(code=404, message='private details'))
        quota = gemini_service._provider_error_message(SimpleNamespace(code=429, message='billing details'))
        invalid_request = gemini_service._provider_error_message(SimpleNamespace(
            code=400, response_json={"error": {"message": "Invalid generation configuration"}}
        ))
        self.assertIn('HTTP 403', invalid_key)
        self.assertIn('HTTP 404', missing_model)
        self.assertIn('HTTP 429', quota)
        self.assertIn('HTTP 400', invalid_request)
        self.assertIn('GEMINI_MODEL', missing_model)
        self.assertIn('quota', quota.lower())
        for error in (invalid_key, missing_model, quota):
            self.assertNotIn('secret-key', error)
            self.assertNotIn('private details', error)
            self.assertNotIn('billing details', error)

    def test_gemini_provider_message_redacts_credentials_and_reports_model_from_environment(self):
        test_key = "AIza" + "A" * 35
        with patch.dict(os.environ, {"GEMINI_API_KEY": test_key, "GEMINI_MODEL": "gemini-2.5-flash"}):
            failure = gemini_service._provider_error_message(SimpleNamespace(
                code=403,
                response_json={"error": {"message": f"Invalid API key: {test_key}"}},
            ))
            self.assertEqual(gemini_service.configuration()["model"], "gemini-2.5-flash")
        self.assertIn("HTTP 403", failure)
        self.assertNotIn(test_key, failure)
        self.assertNotIn("A" * 35, failure)

    def test_gemini_diagnostics_requires_configuration_without_exposing_secrets(self):
        with patch.object(gemini_service, "configuration", return_value={
            "configured": False, "model": "gemini-3.8-flash", "sdk_version": "test",
        }):
            response = self.client.post("/api/gemini/diagnostics")
        self.assertEqual(response.status_code, 503)
        self.assertIn("GEMINI_API_KEY", response.json()["detail"])
        self.assertNotIn("AIza", response.text)

    def test_server_is_bound_to_loopback(self):
        self.assertEqual(api.HOST, '127.0.0.1')


if __name__ == '__main__':
    unittest.main()
