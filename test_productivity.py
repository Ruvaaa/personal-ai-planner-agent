import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import productivity_service as service


TZ = ZoneInfo("Africa/Harare")


class ProductivityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / "productivity.json"
        self.file_patch = patch.object(service, "STATE_FILE", self.state_path)
        self.file_patch.start()
        self.now_patch = patch.object(service.main, "now_local", return_value=datetime(2026, 10, 29, 8, 0, tzinfo=TZ))
        self.now_patch.start()

    def tearDown(self):
        self.now_patch.stop()
        self.file_patch.stop()
        self.temp_dir.cleanup()

    def test_inbox_creation_idempotency_and_status(self):
        first = service.create_inbox_item("Email my lecturer", "whatsapp", "wamid.1")
        duplicate = service.create_inbox_item("Different retry text", "whatsapp", "wamid.1")
        self.assertEqual(first["id"], duplicate["id"])
        self.assertEqual(duplicate["original_text"], "Email my lecturer")
        processed = service.set_inbox_status(first["id"], "processed")
        self.assertEqual(processed["status"], "processed")
        self.assertIsNotNone(processed["processed_at"])

    def test_inbox_conversion_uses_shared_task_manager_and_blocks_repeat(self):
        item = service.create_inbox_item("Finish assignment")
        saved = {"id": 12, "title": "Finish assignment", "status": "pending"}
        with patch.object(service.main, "add_task", return_value=(saved, None)) as add_task:
            converted = service.convert_inbox_item(item["id"], "task", {"title": saved["title"]})
            with self.assertRaises(ValueError):
                service.convert_inbox_item(item["id"], "task", {"title": saved["title"]})
        self.assertEqual(converted["record"], saved)
        add_task.assert_called_once()

    def test_calendar_conversion_requires_explicit_start_and_success(self):
        item = service.create_inbox_item("Maybe study tomorrow")
        with self.assertRaisesRegex(ValueError, "specific start"):
            service.convert_inbox_item(item["id"], "calendar_event", {})
        with patch.object(service.main, "create_calendar_event", return_value=(False, "unavailable")):
            with self.assertRaisesRegex(ValueError, "could not confirm"):
                service.convert_inbox_item(item["id"], "calendar_event", {
                    "start": "2026-10-30T10:00:00+02:00", "title": "Study"
                })
        self.assertEqual(service._find_record("inbox", item["id"])["status"], "open")

    def test_project_progress_and_milestone_overdue_records(self):
        project = service.create_project("Research", target_deadline="2026-11-05")
        milestone = service.create_milestone(project["id"], "Literature review", target_date="2026-10-28")
        tasks = [
            {"id": 1, "title": "Collect sources", "project_id": project["id"], "status": "completed",
             "duration_minutes": 60, "completed_at": "2026-10-29T07:00:00+02:00"},
            {"id": 2, "title": "Write draft", "project_id": project["id"], "status": "pending",
             "duration_minutes": 90, "deadline": "2026-10-28"},
        ]
        with patch.object(service.main, "load_tasks", return_value=tasks):
            dashboard = service.list_projects()[0]
        self.assertEqual(dashboard["progress_percent"], 33)
        self.assertEqual(dashboard["outstanding_task_count"], 1)
        self.assertEqual(dashboard["overdue_task_count"], 1)
        self.assertEqual(dashboard["estimated_remaining_minutes"], 90)
        with patch.object(service.main, "load_tasks", return_value=tasks):
            completed = service.update_milestone(milestone["id"], {"completed": True})
        self.assertTrue(completed["completed"])

    def test_project_dashboard_counts_undated_open_tasks_without_error(self):
        project = service.create_project("Undated task project")
        with patch.object(service.main, "load_tasks", return_value=[{
            "id": 8, "title": "Unscheduled work", "project_id": project["id"],
            "status": "pending", "duration_minutes": 45, "deadline": None,
        }]):
            dashboard = service.list_projects()[0]
        self.assertEqual(dashboard["overdue_task_count"], 0)
        self.assertEqual(dashboard["outstanding_task_count"], 1)
        self.assertEqual(dashboard["estimated_remaining_minutes"], 45)

    def test_project_task_association_uses_existing_task_update(self):
        project = service.create_project("Coursework")
        task = {"id": 4, "title": "Essay", "project_id": project["id"]}
        with patch.object(service.main, "update_task", return_value=(task, None)) as update:
            result = service.link_task_to_project(4, project["id"])
        update.assert_called_once_with(4, {"project_id": project["id"]})
        self.assertEqual(result["project_id"], project["id"])

    def test_daily_plan_respects_events_and_does_not_overlap(self):
        tasks = [
            {"id": 1, "title": "Urgent report", "deadline": "2026-10-29", "priority": "high",
             "duration_minutes": 90, "status": "pending"},
            {"id": 2, "title": "Completed task", "priority": "high", "duration_minutes": 30,
             "status": "completed"},
            {"id": 3, "title": "Research", "priority": "medium", "duration_minutes": 60,
             "status": "pending"},
        ]
        events = [{"id": "evt", "summary": "Lecture", "start": "2026-10-29T10:00:00+02:00",
                   "end": "2026-10-29T11:00:00+02:00"}]
        with patch.object(service.main, "load_tasks", return_value=tasks), \
             patch.object(service.main, "list_calendar_events", return_value=events):
            plan = service.generate_daily_plan()
        task_blocks = [block for block in plan["blocks"] if block["kind"] == "task"]
        self.assertNotIn("Completed task", [block["title"] for block in task_blocks])
        for block in task_blocks:
            self.assertFalse(block["start"] < events[0]["end"] and block["end"] > events[0]["start"])
        self.assertEqual(task_blocks[0]["title"], "Urgent report")
        self.assertTrue(plan["fixed_events"])
        self.assertTrue(all(not block["confirmed_calendar_event"] for block in task_blocks))

    def test_daily_plan_detects_deadlines_dependencies_and_insufficient_time(self):
        tasks = [
            {"id": 1, "title": "Past due", "deadline": "2026-10-28", "duration_minutes": 30, "status": "pending"},
            {"id": 2, "title": "Blocked", "depends_on": [3], "duration_minutes": 30, "status": "pending"},
            {"id": 3, "title": "Prerequisite", "duration_minutes": 30, "status": "pending"},
            {"id": 4, "title": "Long task", "duration_minutes": 600, "status": "pending"},
        ]
        with patch.object(service.main, "load_tasks", return_value=tasks), \
             patch.object(service.main, "list_calendar_events", return_value=[]):
            plan = service.generate_daily_plan()
        reasons = {item["title"]: item["reason"] for item in plan["unscheduled"]}
        self.assertEqual(reasons["Past due"], "deadline has passed")
        self.assertEqual(reasons["Blocked"], "waiting for prerequisite tasks")
        self.assertEqual(reasons["Long task"], "not enough working time remains today")

    def test_calendar_unavailable_is_reported_as_unknown_not_empty(self):
        with patch.object(service.main, "load_tasks", return_value=[
            {"id": 7, "title": "Unplaced", "duration_minutes": 30, "status": "pending"}
        ]), \
             patch.object(service.main, "list_calendar_events", return_value=None):
            plan = service.generate_daily_plan()
        self.assertFalse(plan["events_available"])
        self.assertIn("could not be verified", plan["conflicts"][0])
        self.assertEqual(plan["blocks"], [])
        self.assertEqual(plan["unscheduled"][0]["reason"], "calendar availability cannot be verified")

    def test_workload_insight_reads_do_not_create_proposals(self):
        tasks = [{"id": 1, "title": "Late task", "deadline": "2026-10-28", "status": "pending"}]
        with patch.object(service.main, "load_tasks", return_value=tasks), \
             patch.object(service.main, "list_calendar_events", return_value=[]):
            insights = service.workload_insights()
        self.assertTrue(any(item["kind"] == "overdue" for item in insights))
        self.assertEqual(service.list_proposals(), [])

    def test_accept_capacity_insight_adds_the_reviewed_slot_to_calendar(self):
        tasks = [
            {"id": 1, "title": "First long task", "priority": "high", "duration_minutes": 450,
             "status": "pending", "deadline": None, "depends_on": []},
            {"id": 2, "title": "Second task", "priority": "low", "duration_minutes": 60,
             "status": "pending", "deadline": None, "depends_on": []},
        ]
        with patch.object(service.main, "load_tasks", return_value=tasks), \
             patch.object(service.main, "list_calendar_events", return_value=[]), \
             patch.object(service.main, "update_task", return_value=({**tasks[1], "scheduled_at": "2026-10-30T09:00:00+02:00"}, None)) as update_task, \
             patch.object(service.main, "create_calendar_event", return_value=(True, {
                 "id": "calendar-event-2", "summary": "Second task",
                 "start": "2026-10-30T09:00:00+02:00", "end": "2026-10-30T10:00:00+02:00",
             })) as create_event:
            insight = next(item for item in service.workload_insights() if item["kind"] == "capacity")
            self.assertEqual(insight["suggested_slot"]["start"], "2026-10-30T09:00:00+02:00")
            result = service.accept_workload_insight(
                insight["id"], "2026-10-29", insight["suggested_slot"]["start"]
            )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["event"]["summary"], "Second task")
        self.assertEqual(result["event"]["id"], "calendar-event-2")
        update_task.assert_called_once_with(2, {"scheduled_at": "2026-10-30T09:00:00+02:00"})
        create_event.assert_called_once_with(
            "Second task",
            datetime(2026, 10, 30, 9, 0, tzinfo=TZ),
            datetime(2026, 10, 30, 10, 0, tzinfo=TZ),
            "Scheduled from the accepted workload insight for task 2.",
            idempotency_key=f"workload-insight:{insight['id']}",
        )
        self.assertIn(insight["id"], service.load_state()["dismissed_insights"])

    def test_accept_capacity_insight_rejects_a_stale_calendar_slot(self):
        tasks = [
            {"id": 1, "title": "First long task", "priority": "high", "duration_minutes": 450,
             "status": "pending", "deadline": None, "depends_on": []},
            {"id": 2, "title": "Second task", "priority": "low", "duration_minutes": 60,
             "status": "pending", "deadline": None, "depends_on": []},
        ]
        with patch.object(service.main, "load_tasks", return_value=tasks), \
             patch.object(service.main, "list_calendar_events", return_value=[]), \
             patch.object(service.main, "create_calendar_event") as create_event:
            insight = next(item for item in service.workload_insights() if item["kind"] == "capacity")
            with self.assertRaisesRegex(ValueError, "availability changed"):
                service.accept_workload_insight(insight["id"], "2026-10-29", "2026-10-30T10:00:00+02:00")
        create_event.assert_not_called()

    def test_schedule_requires_approval_before_calendar_writes(self):
        task = {"id": 8, "title": "Write report", "duration_minutes": 30, "status": "pending"}
        with patch.object(service.main, "load_tasks", return_value=[task]), \
             patch.object(service.main, "list_calendar_events", return_value=[]), \
               patch.object(service.main, "create_calendar_event", return_value=(True, "event-link")) as create_event:
            proposal = service.generate_daily_plan()
            with self.assertRaisesRegex(ValueError, "Approve"):
                service.commit_proposal_to_calendar(proposal["id"])
            service.approve_proposal(proposal["id"])
            service.commit_proposal_to_calendar(proposal["id"])
        create_event.assert_called_once()

    def test_reschedule_rejects_fixed_event_conflict(self):
        task = {"id": 9, "title": "Read", "duration_minutes": 60, "status": "pending"}
        event = {"id": "lecture", "summary": "Lecture", "start": "2026-10-29T13:00:00+02:00",
                 "end": "2026-10-29T14:00:00+02:00"}
        with patch.object(service.main, "load_tasks", return_value=[task]), \
             patch.object(service.main, "list_calendar_events", return_value=[]):
            proposal = service.generate_daily_plan()
        block = next(block for block in proposal["blocks"] if block["kind"] == "task")
        with patch.object(service.main, "load_tasks", return_value=[task]), \
             patch.object(service.main, "list_calendar_events", return_value=[event]):
            with self.assertRaisesRegex(ValueError, "conflicts"):
                service.reschedule_proposed_task(proposal["id"], block["id"], "2026-10-29T13:00:00+02:00")

    def test_calendar_changes_after_approval_block_stale_commit(self):
        task = {"id": 10, "title": "Prepare report", "duration_minutes": 60, "status": "pending"}
        with patch.object(service.main, "load_tasks", return_value=[task]), \
             patch.object(service.main, "list_calendar_events", return_value=[]):
            proposal = service.generate_daily_plan()
            service.approve_proposal(proposal["id"])
        block = next(block for block in proposal["blocks"] if block["kind"] == "task")
        collision = {"id": "new-lecture", "summary": "New lecture", "start": block["start"], "end": block["end"]}
        with patch.object(service.main, "load_tasks", return_value=[task]), \
             patch.object(service.main, "list_calendar_events", return_value=[collision]), \
             patch.object(service.main, "create_calendar_event") as create_event:
            with self.assertRaisesRegex(ValueError, "conflicts"):
                service.commit_proposal_to_calendar(proposal["id"])
        create_event.assert_not_called()

    def test_commit_reports_a_block_that_has_already_ended(self):
        task = {"id": 11, "title": "Expired slot", "duration_minutes": 60, "status": "pending"}
        with patch.object(service.main, "load_tasks", return_value=[task]), \
                patch.object(service.main, "list_calendar_events", return_value=[]):
            proposal = service.generate_daily_plan()
            service.approve_proposal(proposal["id"])
        later = datetime(2026, 10, 29, 10, 0, tzinfo=TZ)
        with patch.object(service, "_now", return_value=later), \
                patch.object(service.main, "load_tasks", return_value=[task]), \
                patch.object(service.main, "list_calendar_events", return_value=[]), \
                patch.object(service.main, "create_calendar_event") as create_event:
            with self.assertRaisesRegex(ValueError, "has already ended"):
                service.commit_proposal_to_calendar(proposal["id"])
        create_event.assert_not_called()

    def test_partial_calendar_commit_persists_success_and_retry_skips_it(self):
        tasks = [
            {"id": 12, "title": "First block", "duration_minutes": 30, "status": "pending"},
            {"id": 13, "title": "Second block", "duration_minutes": 30, "status": "pending"},
        ]
        with patch.object(service.main, "load_tasks", return_value=tasks), \
                patch.object(service.main, "list_calendar_events", return_value=[]):
            proposal = service.generate_daily_plan()
            service.approve_proposal(proposal["id"])
        with patch.object(service.main, "load_tasks", return_value=tasks), \
                patch.object(service.main, "list_calendar_events", return_value=[]), \
                patch.object(service.main, "create_calendar_event",
                              side_effect=[(True, "created"), (False, "HTTP 503")]) as create_event:
            with self.assertRaisesRegex(ValueError, "Successfully created blocks were saved"):
                service.commit_proposal_to_calendar(proposal["id"])
            saved = service.load_state()["proposals"][0]
            committed = [block for block in saved["blocks"] if block.get("calendar_committed")]
            self.assertEqual(len(committed), 1)
            create_event.reset_mock()
            create_event.side_effect = None
            create_event.return_value = (True, "created")
            result = service.commit_proposal_to_calendar(proposal["id"])
        self.assertEqual(len(result["results"]), 1)
        self.assertTrue(result["results"][0]["created"])
        self.assertEqual(create_event.call_count, 1)
        self.assertEqual(result["proposal"]["status"], "committed")

    def test_notes_search_and_links_preserve_content(self):
        project = service.create_project("Deep Learning")
        note = service.create_note("Model decision", "Use a convolutional network.\nKeep this formatting.",
                                   project_id=project["id"], tags=["model"])
        updated = service.update_note(note["id"], {"content": "Use a transformer.\nSecond line."})
        self.assertEqual(updated["content"], "Use a transformer.\nSecond line.")
        self.assertEqual(service.search_notes("transformer")[0]["id"], note["id"])
        self.assertEqual(service.search_notes("model")[0]["project_id"], project["id"])
        service.delete_note(note["id"])
        self.assertEqual(service.search_notes("transformer"), [])

    def test_weekly_review_uses_completion_timestamps_and_flags_missing_history(self):
        tasks = [
            {"id": 1, "title": "Done this week", "status": "completed", "completed_at": "2026-10-28T15:00:00+02:00", "duration_minutes": 90},
            {"id": 2, "title": "Unknown history", "status": "completed", "duration_minutes": 50},
            {"id": 3, "title": "Still due", "status": "pending", "deadline": "2026-10-30"},
        ]
        with patch.object(service.main, "load_tasks", return_value=tasks):
            review = service.weekly_review("2026-10-29")
        self.assertEqual(review["week_start"], "2026-10-26")
        self.assertEqual(review["week_end"], "2026-11-01")
        self.assertEqual([task["id"] for task in review["completed_tasks"]], [1])
        self.assertEqual(review["completed_estimated_minutes"], 90)
        self.assertEqual(review["historical_completion_dates_missing"], 1)
        self.assertTrue(review["history_limited"])

    def test_capture_parses_explicit_deadline_and_duration(self):
        preview = service.extract_capture("Study Image Processing tomorrow for 90 minutes")
        self.assertEqual(preview["deadline"], "2026-10-30")
        self.assertEqual(preview["duration_minutes"], 90)
        self.assertEqual(preview["suggested_type"], "task")
        self.assertEqual(preview["title"], "Image Processing")

    def test_settings_validate_hours_and_breaks(self):
        with self.assertRaisesRegex(ValueError, "after start"):
            service.update_planning_settings({"work_start": "17:00", "work_end": "09:00"})
        updated = service.update_planning_settings({"work_start": "08:30", "break_minutes": 15})
        self.assertEqual(updated["work_start"], "08:30")
        self.assertEqual(updated["break_minutes"], 15)


if __name__ == "__main__":
    unittest.main()
