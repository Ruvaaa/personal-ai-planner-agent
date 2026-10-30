import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
import gmail_service


class GmailApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(api.app, client=("127.0.0.1", 12345))

    def test_status_endpoint_returns_no_oauth_secrets(self):
        with patch.object(gmail_service, "connection_status", return_value={
            "configured": True, "connected": True, "account_email": "person@example.test",
            "last_sync": None, "sync_enabled": True,
        }):
            response = self.client.get("/api/gmail/status", headers={
                "origin": "http://127.0.0.1:1420", "host": "127.0.0.1:8787",
            })
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("access_token", response.text)
        self.assertNotIn("refresh_token", response.text)
        self.assertNotIn("client_secret", response.text)
        self.assertEqual(response.json()["account_email"], "person@example.test")

    def test_gmail_rejects_missing_origin_and_host_rebinding(self):
        missing_origin = self.client.get("/api/gmail/status", headers={"host": "127.0.0.1:8787"})
        self.assertEqual(missing_origin.status_code, 403)
        wrong_host = self.client.get("/api/gmail/status", headers={
            "origin": "http://127.0.0.1:1420", "host": "attacker.example:8787",
        })
        self.assertEqual(wrong_host.status_code, 403)

    def test_gmail_rejects_untrusted_browser_origin(self):
        response = self.client.get("/api/gmail/status", headers={
            "origin": "https://untrusted.example", "host": "127.0.0.1:8787",
        })
        self.assertEqual(response.status_code, 403)

    def test_message_listing_without_gmail_token_is_clear_503(self):
        with patch.object(gmail_service, "list_messages", side_effect=gmail_service.GmailNotConfigured(
            "Gmail is not connected. Connect Gmail in Settings."
        )):
            response = self.client.get("/api/gmail/messages", headers={
                "origin": "http://127.0.0.1:1420", "host": "127.0.0.1:8787",
            })
        self.assertEqual(response.status_code, 503)
        self.assertIn("Connect Gmail", response.json()["detail"])

    def test_inbox_category_is_forwarded_and_invalid_category_is_rejected(self):
        headers = {"origin": "http://127.0.0.1:1420", "host": "127.0.0.1:8787"}
        with patch.object(gmail_service, "list_messages", return_value={"messages": []}) as list_messages:
            response = self.client.get("/api/gmail/messages", params={"gmail_category": "updates"}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list_messages.call_args.kwargs["gmail_category"], "updates")

        invalid = self.client.get("/api/gmail/messages", params={"gmail_category": "unknown"}, headers=headers)
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("Unsupported Gmail inbox category", invalid.json()["detail"])

    def test_send_route_calls_backend_only_after_explicit_request(self):
        sent = {"id": "sent-id", "thread_id": "thread-id", "sent": True}
        with patch.object(gmail_service, "send_message", return_value=sent) as send:
            response = self.client.post("/api/gmail/send", json={
                "to": "person@example.test", "subject": "Hello", "body": "Reviewed body",
            }, headers={"origin": "http://127.0.0.1:1420", "host": "127.0.0.1:8787"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), sent)
        send.assert_called_once()

    def test_reply_draft_route_forwards_options_without_sending(self):
        draft = {"source_message_id": "message-id", "body": "Editable draft"}
        headers = {"origin": "http://127.0.0.1:1420", "host": "127.0.0.1:8787"}
        with patch.object(gmail_service, "draft_ai_reply", return_value=draft) as draft_reply:
            response = self.client.post("/api/gmail/messages/message-id/reply-draft", json={
                "tone": "friendly", "instructions": "Keep it brief", "mode": "reply_all",
            }, headers=headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), draft)
        draft_reply.assert_called_once_with("message-id", "friendly", "Keep it brief", "reply_all")

    def test_manual_reply_route_prepares_an_empty_reply_without_ai(self):
        draft = {"source_message_id": "message-id", "body": ""}
        headers = {"origin": "http://127.0.0.1:1420", "host": "127.0.0.1:8787"}
        with patch.object(gmail_service, "prepare_reply", return_value=draft) as prepare_reply, \
                patch.object(gmail_service, "draft_ai_reply") as draft_ai_reply:
            response = self.client.post("/api/gmail/messages/message-id/reply", json={
                "mode": "reply_all",
            }, headers=headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), draft)
        prepare_reply.assert_called_once_with("message-id", "reply_all")
        draft_ai_reply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
