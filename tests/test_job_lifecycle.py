import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import app
import archive
import mcp_bridge
from jobs import JobStore
from store import MessageStore


class JobLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "jobs.json"
        self.jobs = JobStore(self.path)

    def create(self, **fields):
        return self.jobs.create("Work", "job", "general", "operator", **fields)

    def emit(self, job, event, *, actor_kind="orch", operation_id=None, **details):
        return self.jobs.transition_job(
            job["id"], event, actor=f"verified-{actor_kind}", actor_kind=actor_kind,
            expected_uid=job["uid"], expected_version=job["lifecycle"]["version"],
            operation_id=operation_id or f"{event}-{job['lifecycle']['version']}", **details,
        )

    def test_migration_preserves_legacy_meaning_and_persisted_order(self):
        records = [
            {"id": 6, "uid": "todo-uid", "status": "open", "sort_order": 3, "messages": []},
            {"id": 7, "uid": "active-uid", "status": "done", "sort_order": 5, "messages": [{"text": "old"}]},
            {"id": 8, "uid": "closed-uid", "status": "archived", "sort_order": 2, "messages": []},
            {"id": 9, "sort_order": 8, "messages": []},
        ]
        original = (json.dumps(records, indent=2).replace("\n", "\r\n") + "\r\n").encode("utf-8")
        self.path.write_bytes(original)
        backup = self.path.with_name("jobs.json.pre-lifecycle")
        with self.assertLogs("jobs", "INFO") as logs:
            self.jobs = JobStore(self.path)
        self.assertIn(str(backup), logs.output[0])
        self.assertEqual(backup.read_bytes(), original)
        expected = [("open", "core.todo"), ("done", "core.active"), ("archived", "core.closed"), ("open", "core.todo")]
        self.assertEqual([(job["status"], job["lifecycle"]["core_id"]) for job in self.jobs.list_all()], expected)
        active = self.jobs.get(7)
        self.assertEqual((active["uid"], active["sort_order"], active["messages"]), ("active-uid", 5, [{"text": "old"}]))
        self.assertIsNone(self.jobs.get(8)["lifecycle"]["close_outcome"])
        self.assertTrue(self.jobs.get(9)["uid"])
        self.assertTrue(all("status" not in job for job in json.loads(self.path.read_text(encoding="utf-8"))))
        with patch("jobs.atomic_write_text") as write:
            self.assertEqual(JobStore(self.path).list_all(), self.jobs.list_all())
        write.assert_not_called()
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(self.create()["id"], 10)

    def test_migration_keeps_original_backup_when_retrying_after_save_failure(self):
        original = b'[{"id": 1, "status": "open"}]\n'
        self.path.write_bytes(original)
        backup = self.path.with_name("jobs.json.pre-lifecycle")
        with patch("jobs.atomic_write_text", side_effect=OSError("save denied")):
            with self.assertRaises(OSError):
                JobStore(self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(backup.read_bytes(), original)
        # Another save of the legacy store must not replace the original rollback point.
        self.path.write_bytes(b'[{"id": 1, "status": "done"}]\n')
        self.assertEqual(JobStore(self.path).get(1)["lifecycle"]["core_id"], "core.active")
        self.assertEqual(backup.read_bytes(), original)

    def test_failed_backup_leaves_legacy_store_unchanged(self):
        original = b'[{"id": 1, "status": "open"}]\n'
        self.path.write_bytes(original)
        with patch("jobs.write_backup_once", side_effect=OSError("backup denied")):
            with self.assertRaises(OSError):
                JobStore(self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_migration_does_not_replace_file_with_unknown_status(self):
        original = json.dumps([{"id": 1, "status": "unknown"}])
        self.path.write_text(original, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unknown Job status"):
            JobStore(self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_orch_planning_revisions_and_completion(self):
        job = self.create(assignee="verified-orch")
        self.assertIsNone(self.emit(job, "start"))
        job = self.emit(job, "attach")
        self.assertEqual(job["lifecycle"]["stage_id"], "orch.drafting")
        self.assertIsNone(self.jobs.update_status(job["id"], "done"))
        job = self.emit(self.emit(job, "submit_brief"), "approve_brief")
        replanned = self.emit(job, "change_scope")
        self.assertEqual(replanned["lifecycle"]["stage_id"], "orch.drafting")
        self.assertIsNone(self.emit(replanned, "plan_ready"))
        job = self.emit(self.emit(replanned, "submit_brief"), "approve_brief")
        job = self.emit(job, "plan_ready")
        self.assertEqual(job["status"], "open")
        self.assertEqual(self.emit(job, "revise_plan")["lifecycle"]["stage_id"], "orch.planning")
        job = self.emit(self.jobs.get(job["id"]), "plan_ready")
        job = self.emit(job, "change_scope")
        self.assertEqual(job["lifecycle"]["stage_id"], "orch.drafting")
        for event in ("submit_brief", "approve_brief", "plan_ready", "start", "pr_ready"):
            job = self.emit(job, event)
        self.assertEqual((job["status"], job["lifecycle"]["stage_id"]), ("done", "orch.in_review"))
        paused = self.emit(job, "set_flags", flags=["paused"])
        self.assertEqual(paused["lifecycle"]["stage_id"], "orch.in_review")
        self.assertEqual(paused["lifecycle"]["flags"], ["paused"])
        job = self.emit(paused, "complete")
        self.assertEqual((job["status"], job["lifecycle"]["stage_id"], job["lifecycle"]["close_outcome"]),
                         ("archived", "orch.done", "completed"))

    def test_reopen_requires_approval_reason_and_exact_version(self):
        job = self.create()
        started = self.emit(job, "start", actor_kind="human")
        self.assertIsNone(self.emit(job, "cancel", actor_kind="human"))
        closed = self.emit(started, "complete", actor_kind="human")
        self.assertIsNone(self.jobs.update_status(job["id"], "done"))
        self.assertIsNone(self.emit(closed, "reopen", actor_kind="human", reason="Approved scope"))
        self.assertIsNone(self.emit(closed, "reopen", actor_kind="human", approved_reopen=True, reason="  "))
        args = {"actor_kind": "human", "operation_id": "reopen-1", "approved_reopen": True, "reason": "Correct label"}
        reopened = self.emit(closed, "reopen", **args)
        self.assertEqual(reopened["lifecycle"]["core_id"], "core.active")
        self.assertIsNone(reopened["lifecycle"]["close_outcome"])
        self.assertIsNone(self.jobs.delete(job["id"]))
        self.jobs = JobStore(self.path)
        self.assertEqual(self.emit(closed, "reopen", **args), reopened)
        self.assertEqual(self.jobs.get(job["id"])["lifecycle_events"][-1]["reason"], "Correct label")
        with self.assertRaisesRegex(ValueError, "already used"):
            self.emit(closed, "reopen", **dict(args, reason="different request"))
        self.assertIsNone(self.emit(closed, "reopen", **dict(args, operation_id="reopen-2")))

    def test_cancelled_job_cannot_reopen_and_only_closed_can_be_deleted(self):
        job = self.create()
        self.assertIsNone(self.jobs.delete(job["id"]))
        cancelled = self.jobs.update_status(job["id"], "archived")
        self.assertEqual(cancelled["lifecycle"]["close_outcome"], "cancelled")
        self.assertIsNone(self.emit(cancelled, "reopen", actor_kind="human", approved_reopen=True, reason="Retry"))
        self.assertIsNotNone(self.jobs.delete(job["id"]))

    def test_write_failure_preserves_state_and_audit(self):
        job = self.create()
        callback = Mock()
        self.jobs.on_change(callback)
        original = self.path.read_bytes()
        with patch("jobs.atomic_write_text", side_effect=OSError("denied")):
            with self.assertRaises(OSError):
                self.emit(job, "start", actor_kind="human")
        self.assertEqual(self.jobs.get(job["id"]), job)
        self.assertEqual(self.path.read_bytes(), original)
        callback.assert_not_called()
        self.assertIsNotNone(self.emit(job, "start", actor_kind="human"))

    def test_proposal_acceptance_uses_first_message_and_is_idempotent(self):
        messages = MessageStore(self.root / "messages.jsonl")
        proposal = messages.add("proposal-writer", "The draft", "job_proposal", sender_kind="agent",
                                sender_identity="stable-writer")
        request = Mock(json=AsyncMock(return_value={"title": "Job", "anchor_msg_id": proposal["id"]}))
        with patch.multiple(app, jobs=self.jobs, store=messages, _broadcast=AsyncMock()):
            first = asyncio.run(app.create_job(request))
            second = asyncio.run(app.create_job(request))
            self.jobs.update_status(first["id"], "archived")
            self.jobs.delete(first["id"])
            removed = asyncio.run(app.create_job(request))
        self.assertEqual(first, second)
        self.assertEqual(first["lifecycle"]["stage_id"], "orch.drafting")
        self.assertEqual(first["proposal_author_identity"], "stable-writer")
        self.assertEqual(first["assignee"], "")
        self.assertEqual(removed.status_code, 409)
        self.assertEqual(self.jobs.list_all(), [])
        self.assertEqual(sum(msg["type"] == "job_created" for msg in messages.get_recent()), 1)

    def test_legacy_job_mcp_scope_accepts_an_active_managed_job(self):
        job = self.create(assignee="verified-orch")
        for event in ("attach", "submit_brief", "approve_brief", "plan_ready", "start"):
            job = self.emit(job, event)
        credential = "fixture-active-coordinator-token-0001"
        credentials_path = self.root / "orch-fixture.env"
        credentials_path.write_text(f"ORCH_CLOSEOUT_TOKEN={credential}\n", encoding="utf-8")
        ctx = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(
            headers={"x-orch-job-token": credential}, client=SimpleNamespace(host="127.0.0.1")
        )))
        with patch.multiple(mcp_bridge, jobs=self.jobs, registry=None, agents=None,
                            orch_job_auth=("verified-orch", credentials_path)):
            result = mcp_bridge.chat_send("verified-orch", "Active update", job_id=job["id"],
                                          job_uid=job["uid"], job_channel="general", ctx=ctx)
        self.assertTrue(result.startswith("Sent"), result)
        with patch.multiple(mcp_bridge, jobs=self.jobs, registry=None, agents=None):
            header = json.loads(mcp_bridge.chat_read(sender="fixture", job_id=job["id"]))[0]
        self.assertEqual(header["lifecycle"], job["lifecycle"])

    def test_archive_preserves_proposal_and_audit_without_replaying_operations(self):
        source_messages = MessageStore(self.root / "source.jsonl")
        source = source_messages.add("writer", "Proposal", "job_proposal", sender_kind="agent",
                                     sender_identity="writer-id")
        job = self.create(anchor_msg_id=source["id"], from_proposal=True, proposal_author="writer",
                          proposal_author_identity="writer-id", assignee="verified-orch")
        job = self.emit(job, "attach")
        job = self.emit(job, "cancel")
        blob = archive.build_export(source_messages, self.jobs, None, None)
        target_messages = MessageStore(self.root / "target.jsonl")
        target_messages.add("person", "Already here")
        target_jobs = JobStore(self.root / "target-jobs.json")
        report = archive.import_archive(blob, target_messages, target_jobs, None, None, ["general"])
        imported = target_jobs.list_all()[0]
        self.assertEqual(report["sections"]["jobs"]["created"], 1)
        self.assertEqual(imported["uid"], job["uid"])
        self.assertEqual(imported["lifecycle"]["close_outcome"], "cancelled")
        self.assertEqual(imported["lifecycle"]["managed_by"], "manual")
        self.assertEqual(imported["lifecycle"]["version"], 0)
        self.assertEqual(imported["proposal_author_identity"], "writer-id")
        self.assertEqual(target_messages.get_by_id(imported["anchor_msg_id"])["uid"], source["uid"])
        self.assertNotIn("lifecycle_events", imported)
        self.assertEqual(imported["imported_lifecycle_events"], job["lifecycle_events"])
