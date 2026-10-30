import hashlib
import hmac
import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
import whatsapp_service as service


class WhatsAppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.productivity_state_patch = patch.object(
            service.productivity_service, "STATE_FILE", Path(self.temp_dir.name) / "productivity.json"
        )
        self.productivity_state_patch.start()
        self.env = patch.dict(os.environ, {
            "WHATSAPP_ENABLED": "true",
            "WHATSAPP_ACCESS_TOKEN": "test-access-secret",
            "WHATSAPP_PHONE_NUMBER_ID": "123456789012345",
            "WHATSAPP_VERIFY_TOKEN": "test-verify-secret",
            "WHATSAPP_APP_SECRET": "test-app-secret",
            "WHATSAPP_NGROK_PROXY_TOKEN": "test-ngrok-proxy-secret",
            "WHATSAPP_ALLOWED_PHONE_NUMBERS": "+263 77 123 4567",
            "WHATSAPP_STATE_DB": os.path.join(self.temp_dir.name, "webhook-state.sqlite3"),
            "WHATSAPP_PUBLIC_WEBHOOK_URL": "https://planner.example.test",
            "GEMINI_API_KEY": "test-gemini-secret",
        })
        self.env.start()
        self.client = TestClient(api.app)

    def tearDown(self):
        self.env.stop()
        self.productivity_state_patch.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def payload(sender="263771234567", message_id="wamid.test.1", text="Hello"):
        return {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"field": "messages", "value": {
                "metadata": {"phone_number_id": "123456789012345"},
                "messages": [{"from": sender, "id": message_id, "type": "text",
                              "text": {"body": text}}],
            }}]}],
        }

    def signed_post(self, payload, signature_secret="test-app-secret"):
        body = json.dumps(payload).encode()
        signature = hmac.new(signature_secret.encode(), body, hashlib.sha256).hexdigest()
        return self.client.post(service.WEBHOOK_PATH, content=body,
                                headers={"X-Hub-Signature-256": "sha256=" + signature,
                                         "Content-Type": "application/json"})

    def test_webhook_verification_returns_documented_challenge(self):
        response = self.client.get(service.WEBHOOK_PATH, params={
            "hub.mode": "subscribe", "hub.verify_token": "test-verify-secret",
            "hub.challenge": "challenge-123",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "challenge-123")

    def test_webhook_verification_rejects_bad_token_and_disabled_mode(self):
        response = self.client.get(service.WEBHOOK_PATH, params={
            "hub.mode": "subscribe", "hub.verify_token": "wrong",
            "hub.challenge": "challenge-123",
        })
        self.assertEqual(response.status_code, 403)
        with patch.dict(os.environ, {"WHATSAPP_ENABLED": "false"}):
            disabled = self.client.get(service.WEBHOOK_PATH, params={
                "hub.mode": "subscribe", "hub.verify_token": "test-verify-secret",
                "hub.challenge": "challenge-123",
            })
        self.assertEqual(disabled.status_code, 403)

    def test_signature_accepts_valid_and_rejects_invalid(self):
        body = b'{"signed":true}'
        signature = hmac.new(b"test-app-secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(service.verify_signature(body, "sha256=" + signature))
        self.assertFalse(service.verify_signature(body, "sha256=" + "0" * 64))
        self.assertFalse(service.verify_signature(body, ""))

    def test_invalid_signature_is_rejected(self):
        response = self.client.post(service.WEBHOOK_PATH, content=b"{}",
                                    headers={"X-Hub-Signature-256": "sha256=" + "0" * 64})
        self.assertEqual(response.status_code, 401)

    def test_unauthorized_sender_is_ignored(self):
        with patch.object(service, "process_incoming_message") as process:
            response = self.signed_post(self.payload(sender="263779999999"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["queued"], 0)
        process.assert_not_called()

    def test_malformed_payload_is_rejected(self):
        response = self.signed_post({"object": "whatsapp_business_account", "entry": "bad"})
        self.assertEqual(response.status_code, 400)

    def test_delivery_status_notification_is_acknowledged_but_not_processed(self):
        status_payload = {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"field": "messages", "value": {
                "metadata": {"phone_number_id": "123456789012345"},
                "statuses": [{"id": "wamid.out", "status": "delivered", "recipient_id": "263771234567"}],
            }}]}],
        }
        with patch.object(service, "process_incoming_message") as process:
            response = self.signed_post(status_payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["queued"], 0)
        process.assert_not_called()

    def test_requested_webhook_path_and_legacy_alias_both_verify(self):
        params = {"hub.mode": "subscribe", "hub.verify_token": "test-verify-secret", "hub.challenge": "ok"}
        current = self.client.get("/api/whatsapp/webhook", params=params)
        legacy = self.client.get("/webhooks/whatsapp", params=params)
        self.assertEqual(current.status_code, 200)
        self.assertEqual(legacy.status_code, 200)

    def test_reverse_proxy_cannot_publish_the_rest_of_the_local_api(self):
        proxied_client = TestClient(api.app, client=("127.0.0.1", 12345))
        proxied_headers = {
            "x-forwarded-for": "198.51.100.24",
            "x-forwarded-host": "planner.ngrok.app",
            "x-planner-ngrok-auth": "test-ngrok-proxy-secret",
        }
        private_api = proxied_client.get("/api/health", headers=proxied_headers)
        challenge = proxied_client.get("/api/whatsapp/webhook", params={
            "hub.mode": "subscribe", "hub.verify_token": "test-verify-secret", "hub.challenge": "ngrok-ok",
        }, headers=proxied_headers)
        status_api = proxied_client.get("/api/whatsapp/status", headers=proxied_headers)
        self.assertEqual(private_api.status_code, 404)
        self.assertEqual(challenge.status_code, 200)
        self.assertEqual(challenge.text, "ngrok-ok")
        self.assertEqual(status_api.status_code, 404)

    def test_forwarded_headers_without_ngrok_edge_marker_are_rejected(self):
        proxied_client = TestClient(api.app, client=("127.0.0.1", 12345))
        response = proxied_client.get("/api/whatsapp/webhook", params={
            "hub.mode": "subscribe", "hub.verify_token": "test-verify-secret", "hub.challenge": "nope",
        }, headers={"x-forwarded-for": "198.51.100.24", "x-forwarded-host": "planner.ngrok.app"})
        self.assertEqual(response.status_code, 403)

    def test_direct_local_webhook_verification_does_not_need_proxy_marker(self):
        response = self.client.get("/api/whatsapp/webhook", params={
            "hub.mode": "subscribe", "hub.verify_token": "test-verify-secret", "hub.challenge": "local-ok",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "local-ok")

    def test_task_creation_uses_shared_task_manager(self):
        expected_deadline = (service.main.now_local().date() + timedelta(days=1)).isoformat()
        created = {"id": 88, "title": "Finish Image Processing", "deadline": expected_deadline,
                   "priority": "medium", "duration_minutes": 60, "status": "pending"}
        with patch.object(service.main, "add_task", return_value=(created, None)) as add_task, \
             patch.object(service, "_send_text_message", return_value="wamid.out.1"):
            reply = service.process_incoming_message(
                "263771234567", "wamid.in.create", "Remind me to Finish Image Processing tomorrow."
            )
        add_task.assert_called_once_with("Finish Image Processing", expected_deadline, "medium", 60)
        self.assertIn("Task added", reply)
        self.assertIn(expected_deadline, reply)

    def test_task_retrieval_uses_shared_task_store(self):
        with patch.object(service.main, "load_tasks", return_value=[
            {"id": 3, "title": "Study databases", "deadline": "2026-11-02", "priority": "high", "status": "pending"}
        ]), patch.object(service, "_send_text_message", return_value="wamid.out.2"):
            reply = service.process_incoming_message(
                "263771234567", "wamid.in.list", "What do I still need to finish?"
            )
        self.assertIn("Study databases", reply)

    def test_completed_task_retrieval_uses_shared_task_store(self):
        with patch.object(service.main, "load_tasks", return_value=[
            {"id": 9, "title": "Read systems chapter", "status": "completed"}
        ]), patch.object(service, "_send_text_message", return_value="wamid.out.completed"):
            reply = service.process_incoming_message(
                "263771234567", "wamid.in.completed", "Show my completed tasks."
            )
        self.assertIn("Read systems chapter", reply)

    def test_task_completion_uses_shared_task_manager(self):
        task = {"id": 4, "title": "Database Systems", "status": "pending"}
        with patch.object(service.main, "load_tasks", return_value=[task]), \
             patch.object(service.main, "complete_task", return_value=(task, None)) as complete, \
             patch.object(service, "_send_text_message", return_value="wamid.out.3"):
            reply = service.process_incoming_message(
                "263771234567", "wamid.in.done", "I finished studying Database Systems."
            )
        complete.assert_called_once_with("4")
        self.assertIn("Marked complete", reply)

    def test_priority_update_uses_shared_task_manager(self):
        task = {"id": 5, "title": "Image Processing", "status": "pending", "priority": "high"}
        with patch.object(service.main, "load_tasks", return_value=[task]), \
             patch.object(service.main, "update_task", return_value=(task, None)) as update, \
             patch.object(service, "_send_text_message", return_value="wamid.out.priority"):
            reply = service.process_incoming_message(
                "263771234567", "wamid.in.priority", "Set the priority of my Image Processing to high."
            )
        update.assert_called_once_with(5, {"priority": "high"})
        self.assertIn("high priority", reply)

    def test_deadline_update_uses_shared_task_manager(self):
        task = {"id": 6, "title": "Image Processing assignment", "status": "pending",
                "deadline": "2026-10-30"}
        with patch.object(service.main, "load_tasks", return_value=[task]), \
             patch.object(service.main, "update_task", return_value=(task, None)) as update, \
             patch.object(service, "_send_text_message", return_value="wamid.out.deadline"):
            reply = service.process_incoming_message(
                "263771234567", "wamid.in.deadline", "Move my Image Processing assignment to Friday."
            )
        update.assert_called_once()
        self.assertEqual(update.call_args.args[0], 6)
        self.assertIn("deadline", update.call_args.args[1])
        self.assertIn("Moved Image Processing", reply)

    def test_ambiguous_task_reference_asks_before_modifying(self):
        tasks = [
            {"id": 1, "title": "Study Database Systems", "status": "pending"},
            {"id": 2, "title": "Review Database Systems", "status": "pending"},
        ]
        with patch.object(service.main, "load_tasks", return_value=tasks), \
             patch.object(service.main, "complete_task") as complete, \
             patch.object(service, "_send_text_message", return_value="wamid.out.4"):
            reply = service.process_incoming_message(
                "263771234567", "wamid.in.ambiguous", "I finished Database Systems."
            )
        complete.assert_not_called()
        self.assertIn("Which one", reply)

    def test_missing_deadline_clarification_does_not_create_task(self):
        with patch.object(service.main, "add_task") as add_task, \
             patch.object(service, "_send_text_message", return_value="wamid.out.5"):
            reply = service.process_incoming_message(
                "263771234567", "wamid.in.date", "Remind me to submit my assignment next week."
            )
        add_task.assert_not_called()
        self.assertIn("Which deadline", reply)

    def test_calendar_request_without_start_time_asks_and_does_not_create(self):
        with patch.object(service.main, "calendar_service", object()), \
             patch.object(service.main, "create_calendar_event") as create_event, \
             patch.object(service, "_send_text_message", return_value="wamid.out.6"):
            reply = service.process_incoming_message(
                "263771234567", "wamid.in.calendar", "Schedule a two-hour study session tomorrow."
            )
        create_event.assert_not_called()
        self.assertIn("What time", reply)

    def test_calendar_failure_never_claims_event_was_created(self):
        with patch.object(service.main, "calendar_service", object()), \
             patch.object(service.main, "create_calendar_event", return_value=(False, "Calendar unavailable")), \
             patch.object(service, "_send_text_message", return_value="wamid.out.calendar-failed"):
            reply = service.process_incoming_message(
                "263771234567", "wamid.in.calendar-failed",
                "Schedule study session tomorrow at 9am."
            )
        self.assertIn("couldn't create", reply)
        self.assertNotIn("Scheduled study", reply)

    def test_calendar_success_is_reported_only_after_backend_confirmation(self):
        with patch.object(service.main, "calendar_service", object()), \
             patch.object(service.main, "create_calendar_event", return_value=(True, "https://calendar.test/event")) as create_event, \
             patch.object(service, "_send_text_message", return_value="wamid.out.calendar-ok"):
            reply = service.process_incoming_message(
                "263771234567", "wamid.in.calendar-ok", "Schedule study session tomorrow at 9am."
            )
        create_event.assert_called_once()
        self.assertIn("Scheduled study session", reply)

    def test_duplicate_webhook_delivery_is_queued_once(self):
        payload = self.payload(message_id="wamid.duplicate")
        with patch.object(service, "process_incoming_message") as process:
            first = self.signed_post(payload)
            second = self.signed_post(payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["queued"], 1)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["queued"], 0)
        process.assert_called_once()

    def test_signed_webhook_queues_authorized_message(self):
        with patch.object(service, "process_incoming_message") as process:
            response = self.signed_post(self.payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["queued"], 1)
        process.assert_called_once()

    def test_incoming_message_timestamp_is_extracted(self):
        payload = self.payload()
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["timestamp"] = "1793300000"
        parsed = service.parse_webhook_payload(payload)
        self.assertEqual(parsed[0]["timestamp"], "1793300000")

    def test_whatsapp_ai_fallback_reuses_shared_chat_reply_generator(self):
        with patch.object(service, "_send_text_message", return_value="wamid.reply"), \
             patch("api.generate_assistant_reply", return_value="Shared chat reply") as generate:
            reply = service.process_incoming_message(
                "263771234567", "wamid.in.generic", "Can you help me think through my week?"
            )
        self.assertEqual(reply, "Shared chat reply")
        generate.assert_called_once()

    def test_disabled_integration_rejects_post(self):
        with patch.dict(os.environ, {"WHATSAPP_ENABLED": "false"}):
            response = self.client.post(service.WEBHOOK_PATH, content=b"{}")
        self.assertEqual(response.status_code, 503)

    def test_failed_external_send_is_reported_as_error(self):
        with patch.object(service.urllib.request, "urlopen", side_effect=OSError("offline")):
            with self.assertRaises(service.WhatsAppError):
                service._send_text_message("263771234567", "hello")

    def test_frontend_status_never_contains_secrets(self):
        status = api.whatsapp_status()
        serialized = json.dumps(status)
        self.assertTrue(status["configuration_present"])
        self.assertTrue(status["ready"])
        for secret in ("test-access-secret", "test-verify-secret", "test-app-secret", "test-gemini-secret"):
            self.assertNotIn(secret, serialized)

    def test_non_loopback_clients_cannot_reach_planner_routes(self):
        remote_client = TestClient(api.app, client=("203.0.113.10", 12345))
        self.assertEqual(remote_client.get("/api/tasks").status_code, 403)
        self.assertEqual(remote_client.get(service.WEBHOOK_PATH).status_code, 403)


if __name__ == "__main__":
    unittest.main()
