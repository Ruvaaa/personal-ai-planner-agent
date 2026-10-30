import base64
import json
import ssl
import tempfile
import threading
import time
import unittest
from email import message_from_bytes
from pathlib import Path
from unittest.mock import MagicMock, patch

import gmail_service as gmail


class GmailServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.token_file = Path(self.temp_dir.name) / "gmail_token.json"
        self.cache_file = Path(self.temp_dir.name) / "mail-cache.sqlite3"
        self.token_patch = patch.object(gmail, "GMAIL_TOKEN_FILE", self.token_file)
        self.cache_patch = patch.object(gmail, "GMAIL_CACHE_FILE", self.cache_file)
        self.token_patch.start()
        self.cache_patch.start()
        gmail._SERVICE = None

    def tearDown(self):
        gmail._SERVICE = None
        self.cache_patch.stop()
        self.token_patch.stop()
        self.temp_dir.cleanup()

    def test_oauth_state_validation(self):
        self.assertTrue(gmail.validate_oauth_state("a-state", "a-state"))
        self.assertFalse(gmail.validate_oauth_state("a-state", "other"))
        self.assertFalse(gmail.validate_oauth_state("", ""))

    def test_missing_gmail_token_is_nonfatal_not_configured(self):
        with self.assertRaises(gmail.GmailNotConfigured):
            gmail._ensure_credentials()
        with patch.object(gmail.main, "CREDENTIALS_FILE", Path(self.temp_dir.name) / "missing.json"):
            status = gmail.connection_status()
        self.assertFalse(status["connected"])
        self.assertFalse(status["configured"])

    def test_stale_calendar_only_token_requires_gmail_reconsent(self):
        fake_credentials = MagicMock()
        fake_credentials.has_scopes.return_value = False
        self.token_file.write_text("{}", encoding="utf-8")
        with patch.object(gmail.Credentials, "from_authorized_user_file", return_value=fake_credentials):
            with self.assertRaisesRegex(gmail.GmailNotConfigured, "Reconnect Gmail"):
                gmail._ensure_credentials()

    def test_refresh_failure_is_sanitized(self):
        fake_credentials = MagicMock()
        fake_credentials.has_scopes.return_value = True
        fake_credentials.expired = True
        fake_credentials.refresh_token = "refresh-secret"
        fake_credentials.refresh.side_effect = RuntimeError("token-secret")
        self.token_file.write_text("{}", encoding="utf-8")
        with patch.object(gmail.Credentials, "from_authorized_user_file", return_value=fake_credentials):
            with self.assertRaises(gmail.GmailError) as error:
                gmail._ensure_credentials()
        self.assertNotIn("refresh-secret", str(error.exception))
        self.assertNotIn("token-secret", str(error.exception))

    def test_list_messages_uses_gmail_page_tokens(self):
        service = MagicMock()
        users = service.users.return_value
        users.messages.return_value.list.return_value.execute.return_value = {
            "messages": [{"id": "m1", "threadId": "t1"}], "nextPageToken": "next-page", "resultSizeEstimate": 2
        }
        users.messages.return_value.get.return_value.execute.return_value = {
            "id": "m1", "threadId": "t1", "labelIds": ["INBOX", "UNREAD"],
            "snippet": "A preview", "payload": {"headers": [
                {"name": "From", "value": "Sender <sender@example.test>"},
                {"name": "Subject", "value": "Important"},
            ]},
        }
        with patch.object(gmail, "gmail_client", return_value=service):
            page = gmail.list_messages(
                "inbox", page_token="previous", query="from:sender@example.test",
                page_size=30, gmail_category="social",
            )
        self.assertEqual(page["next_page_token"], "next-page")
        self.assertTrue(page["messages"][0]["unread"])
        self.assertEqual(page["messages"][0]["subject"], "Important")
        kwargs = users.messages.return_value.list.call_args.kwargs
        self.assertEqual(kwargs["pageToken"], "previous")
        self.assertEqual(kwargs["q"], "from:sender@example.test category:social")
        self.assertEqual(kwargs["labelIds"], ["INBOX"])

    def test_api_call_enables_bounded_google_client_retries(self):
        request = MagicMock()
        request.execute.return_value = {"ok": True}
        self.assertEqual(gmail._api_call(request), {"ok": True})
        request.execute.assert_called_once_with(num_retries=gmail.GMAIL_API_RETRIES)

    def test_create_draft_allows_writing_before_adding_a_recipient(self):
        service = MagicMock()
        response = {"id": "draft-id", "message": {"id": "message-id"}}
        with patch.object(gmail, "gmail_client", return_value=service), \
                patch.object(gmail, "_api_call", return_value=response):
            result = gmail.create_draft("", subject="A note", body="Text written by the user")

        self.assertEqual(result["draft_id"], "draft-id")
        request = service.users.return_value.drafts.return_value.create
        request.assert_called_once()
        raw = request.call_args.kwargs["body"]["message"]["raw"]
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        message = message_from_bytes(decoded)
        self.assertIsNone(message["To"])
        self.assertEqual(message["Subject"], "A note")
        self.assertIn("Text written by the user", message.get_payload(decode=True).decode())

    def test_prepare_reply_builds_editable_reply_without_calling_ai(self):
        message = {
            "from": "Sender <sender@example.test>",
            "to": "me@example.test",
            "cc": "",
            "subject": "Project update",
            "thread_id": "thread-1",
        }
        with patch.object(gmail, "get_message", return_value=message), \
                patch.object(gmail, "_state_get", return_value="me@example.test"), \
                patch.object(gmail.gemini_service, "generate_reply") as generate_reply:
            result = gmail.prepare_reply("message-1")

        self.assertEqual(result["to"], "sender@example.test")
        self.assertEqual(result["subject"], "Re: Project update")
        self.assertEqual(result["body"], "")
        self.assertEqual(result["thread_id"], "thread-1")
        self.assertEqual(result["source_message_id"], "message-1")
        generate_reply.assert_not_called()

    def test_send_still_requires_a_recipient(self):
        with self.assertRaisesRegex(ValueError, "At least one recipient"):
            gmail.send_message("", subject="A note", body="Text written by the user")

    def test_api_call_serializes_shared_google_transport_requests(self):
        state_lock = threading.Lock()
        active_requests = 0
        max_active_requests = 0

        def execute(*, num_retries):
            nonlocal active_requests, max_active_requests
            with state_lock:
                active_requests += 1
                max_active_requests = max(max_active_requests, active_requests)
            time.sleep(0.01)
            with state_lock:
                active_requests -= 1
            return {"ok": True}

        request = MagicMock()
        request.execute.side_effect = execute
        threads = [
            threading.Thread(target=gmail._api_call, args=(request,))
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(max_active_requests, 1)

    def test_api_call_reports_tls_failures_without_raw_exception_details(self):
        request = MagicMock()
        request.execute.side_effect = ssl.SSLError("private certificate detail")
        with self.assertRaisesRegex(gmail.GmailError, "secure connection failed after retries") as error:
            gmail._api_call(request)
        self.assertNotIn("private certificate detail", str(error.exception))

    def test_sync_failure_is_persisted_for_status(self):
        sync_error = gmail.GmailError("Gmail request timed out after retries.")
        state_values = {"history_id": "history-1", "sync_enabled": True}
        with patch.object(gmail, "connection_status", return_value={"connected": True}), \
             patch.object(gmail, "_state_get", side_effect=lambda key, fallback=None: state_values.get(key, fallback)), \
             patch.object(gmail, "_changed_message_ids", side_effect=sync_error), \
             patch.object(gmail, "_state_set") as set_state:
            with self.assertRaisesRegex(gmail.GmailError, "timed out after retries"):
                gmail.synchronize()
        set_state.assert_any_call("last_sync_error", None)
        set_state.assert_any_call("last_sync_error", str(sync_error))
        self.assertEqual(set_state.call_count, 2)

    def test_sync_skips_messages_deleted_before_metadata_fetch(self):
        state_values = {"history_id": "h1", "sync_enabled": True, "classification_enabled": False}
        service = MagicMock()
        raw_message = {"id": "available", "threadId": "thread-1", "labelIds": ["INBOX"]}
        with patch.object(gmail, "connection_status", return_value={
            "connected": True, "classification_enabled": False,
        }), patch.object(gmail, "_state_get",
                         side_effect=lambda key, fallback=None: state_values.get(key, fallback)), \
                patch.object(gmail, "_state_set",
                             side_effect=lambda key, value: state_values.__setitem__(key, value)), \
                patch.object(gmail, "_changed_message_ids", return_value=({"deleted", "available"}, "h2")), \
                patch.object(gmail, "gmail_client", return_value=service), \
                patch.object(gmail, "_api_call", side_effect=[
                    gmail.GmailError("Gmail API request failed (HTTP 404)."), raw_message,
                ]), patch.object(gmail, "_cache_metadata", return_value=True) as cache:
            result = gmail.synchronize()
        self.assertEqual(result["new_or_updated"], 1)
        self.assertEqual(state_values["history_id"], "h2")
        self.assertIsNone(state_values["last_sync_error"])
        cache.assert_called_once_with(raw_message)

    def test_gemini_classification_failure_does_not_abort_mail_sync(self):
        state_values = {"sync_enabled": True, "classification_enabled": True}
        raw_message = {"id": "m1", "threadId": "t1", "labelIds": ["INBOX"]}
        service = MagicMock()
        with patch.object(gmail, "connection_status", return_value={
            "connected": True, "classification_enabled": True,
        }), patch.object(gmail, "_state_get",
                         side_effect=lambda key, fallback=None: state_values.get(key, fallback)), \
                patch.object(gmail, "_state_set",
                             side_effect=lambda key, value: state_values.__setitem__(key, value)), \
                patch.object(gmail, "get_account_profile", return_value={"history_id": "h1"}), \
                patch.object(gmail, "_messages_list_page", return_value={"messages": [{"id": "m1"}]}), \
                patch.object(gmail, "gmail_client", return_value=service), \
                patch.object(gmail, "_api_call", return_value=raw_message), \
                patch.object(gmail, "_cache_metadata", return_value=True), \
                patch.object(gmail, "classify_message",
                             side_effect=gmail.gemini_service.GeminiError("Gemini unavailable")) as classify, \
                patch.object(gmail, "_set_classification_status") as set_classification:
            result = gmail.synchronize()

        self.assertEqual(result["new_or_updated"], 1)
        self.assertEqual(result["classified"], 0)
        self.assertIsNotNone(result["last_sync"])
        self.assertEqual(state_values["last_sync_error"], None)
        self.assertEqual(state_values["history_id"], "h1")
        classify.assert_called_once_with("m1")
        set_classification.assert_called_once_with("m1", "failed")

    def test_draft_folder_uses_drafts_list_not_message_labels(self):
        service = MagicMock()
        users = service.users.return_value
        users.drafts.return_value.list.return_value.execute.return_value = {"drafts": [], "nextPageToken": "p2"}
        with patch.object(gmail, "gmail_client", return_value=service):
            page = gmail.list_messages("drafts")
        self.assertEqual(page["messages"], [])
        self.assertEqual(page["next_page_token"], "p2")
        self.assertNotIn("labelIds", users.drafts.return_value.list.call_args.kwargs)

    def test_parse_mime_multipart_and_encoded_header(self):
        plain = base64.urlsafe_b64encode("Hello plain".encode()).decode().rstrip("=")
        rich = base64.urlsafe_b64encode('<p>Hello <b>HTML</b></p>'.encode()).decode().rstrip("=")
        result = gmail.parse_gmail_message({
            "id": "m1", "threadId": "t1", "labelIds": ["INBOX"], "snippet": "What&#39;s next?",
            "payload": {
                "mimeType": "multipart/alternative",
                "headers": [{"name": "Subject", "value": "=?utf-8?b?SGVsbG8=?="}],
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": plain}},
                    {"mimeType": "text/html", "body": {"data": rich}},
                ],
            },
        })
        self.assertEqual(result["subject"], "Hello")
        self.assertEqual(result["snippet"], "What's next?")
        self.assertEqual(result["body_text"], "Hello plain")
        self.assertIn("<b>HTML</b>", result["body_html"])

    def test_html_sanitizer_removes_scripts_and_unsafe_links(self):
        safe = gmail.sanitize_html('<script>alert(1)</script><img src=x onerror=alert(1)><a href="javascript:alert(2)" onclick="x()">read</a><p>safe</p>')
        self.assertNotIn("script", safe)
        self.assertNotIn("javascript:", safe)
        self.assertNotIn("onerror", safe)
        self.assertNotIn("onclick", safe)
        self.assertIn("<p>safe</p>", safe)
        self.assertIn("read", safe)

    def test_classification_schema_validates_categories_and_ambiguous_dates(self):
        value = gmail.validate_classification({
            "categories": ["academic", "action_required"], "summary": "Submit report",
            "explicit_deadlines": [{"text": "next Friday", "explicit": True}], "confidence": 0.8,
        })
        self.assertIsNone(value["explicit_deadlines"][0]["iso_date"])
        self.assertTrue(value["explicit_deadlines"][0]["needs_review"])
        with self.assertRaises(ValueError):
            gmail.validate_classification({"categories": ["made_up"], "confidence": 0.5})

    def test_classification_schema_treats_null_optional_lists_as_empty(self):
        value = gmail.validate_classification({
            "categories": ["general"],
            "requested_actions": None,
            "meeting_details": None,
            "questions": None,
        })
        self.assertEqual(value["requested_actions"], [])
        self.assertEqual(value["meeting_details"], [])
        self.assertEqual(value["questions"], [])
        with self.assertRaisesRegex(ValueError, "Requested actions must be an array"):
            gmail.validate_classification({
                "categories": ["general"],
                "requested_actions": "not an array",
            })

    def test_reply_draft_is_not_sent_and_preserves_thread(self):
        message = {
            "id": "m1", "thread_id": "t1", "subject": "Question", "from": "Sender <sender@example.test>",
            "to": "me@example.test", "cc": "", "date": "today", "body_text": "Can we meet?",
            "message_id_header": "<source@example.test>", "references": "<prior@example.test>",
        }
        service = MagicMock()
        service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
            "id": "m1", "threadId": "t1", "payload": {"headers": [
                {"name": "From", "value": message["from"]}, {"name": "To", "value": message["to"]},
                {"name": "Subject", "value": message["subject"]}, {"name": "Message-ID", "value": "<source@example.test>"},
                {"name": "References", "value": "<prior@example.test>"},
            ], "mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"Can we meet?").decode().rstrip("=")}},
        }
        service.users.return_value.threads.return_value.get.return_value.execute.return_value = {
            "id": "t1", "messages": [service.users.return_value.messages.return_value.get.return_value.execute.return_value]
        }
        with patch.object(gmail, "gmail_client", return_value=service), \
             patch.object(gmail.gemini_service, "generate_reply", return_value="I can meet Thursday.") as generate, \
             patch.object(gmail, "create_draft") as create_draft, \
             patch.object(gmail, "_state_get", side_effect=lambda key, fallback=None: "me@example.test" if key == "account_email" else fallback):
            draft = gmail.draft_ai_reply("m1", "friendly")
        generate.assert_called_once()
        create_draft.assert_not_called()
        self.assertEqual(draft["to"], "sender@example.test")
        self.assertEqual(draft["thread_id"], "t1")
        self.assertEqual(draft["body"], "I can meet Thursday.")

    def test_raw_reply_has_thread_headers_and_safe_recipient_validation(self):
        raw = gmail._raw_message("person@example.test", "", "Re: Topic", "Reply body", {
            "message_id_header": "<source@example.test>", "references": "<older@example.test>"
        })["raw"]
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        message = message_from_bytes(decoded)
        self.assertEqual(message["In-Reply-To"], "<source@example.test>")
        self.assertIn("<older@example.test>", message["References"])
        with self.assertRaises(ValueError):
            gmail._raw_message("person@example.test\r\nBcc: victim@example.test", "", "x", "y")

    def test_gmail_api_errors_are_sanitized(self):
        service = MagicMock()
        error = type("FakeHttpError", (Exception,), {"resp": type("Response", (), {"status": 429})()})()
        with patch.object(gmail, "gmail_client", return_value=service), \
             patch.object(gmail, "_api_call", side_effect=gmail.GmailError("Gmail is temporarily rate-limiting requests. Try again shortly.")):
            with self.assertRaisesRegex(gmail.GmailError, "rate-limiting"):
                gmail.list_labels()

    def test_duplicate_email_task_uses_existing_task_manager_once(self):
        task = {"id": 88, "title": "Submit report", "status": "pending", "source_email_id": "m2"}
        with patch.object(gmail.main, "add_task", return_value=(task, None)) as add_task, \
             patch.object(gmail.main, "find_task", return_value=task):
            first = gmail.create_task_from_email("m2", "Submit report", "2026-10-30")
            second = gmail.create_task_from_email("m2", "Submit report", "2026-10-30")
        self.assertTrue(first["created"])
        self.assertTrue(second["duplicate"])
        add_task.assert_called_once()

    def test_task_deadline_rejects_unresolved_natural_text(self):
        with self.assertRaisesRegex(ValueError, "needs review"):
            gmail.create_task_from_email("m3", "Follow up", "next Friday")

    def test_disconnect_removes_only_gmail_token_and_never_gmail_messages(self):
        self.token_file.write_text("token-data", encoding="utf-8")
        with patch.object(gmail, "clear_email_cache", return_value={"cleared": True, "gmail_messages_deleted": False}):
            result = gmail.disconnect_gmail(clear_cache=True)
        self.assertFalse(self.token_file.exists())
        self.assertFalse(result["gmail_messages_deleted"])


if __name__ == "__main__":
    unittest.main()
