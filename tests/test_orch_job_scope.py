from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jobs import JobStore
import mcp_bridge


class OrchJobScopeTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.path = self.directory / "jobs.json"
        self.jobs = JobStore(self.path)
        self.credentials = self.directory / "service-fixture.env"
        self.token = "fixture-orch-service-token-for-job-tests"
        self.credentials.write_text(f"ORCH_CLOSEOUT_TOKEN={self.token}\n", encoding="utf-8")

    def send(self, job, actor="orchestrator", *, configured_actor=None, credential=True,
             host="127.0.0.1", channel=None):
        headers = {"x-orch-job-token": self.token} if credential else {}
        ctx = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(
            headers=headers, client=SimpleNamespace(host=host)
        )))
        with patch.multiple(mcp_bridge, jobs=self.jobs, registry=None, agents=None,
                            orch_job_auth=(configured_actor or actor, self.credentials)):
            return mcp_bridge.chat_send(actor, "Scoped update", job_id=job["id"], job_uid=job["uid"],
                                        job_channel=channel or job["channel"], ctx=ctx)

    def transition(self, job, event, actor="orchestrator", *, scope=None):
        return self.jobs.transition_job(
            job["id"], event, actor=actor, actor_kind="orch", expected_uid=job["uid"],
            expected_version=job["lifecycle"]["version"], operation_id=f"{event}:{job['lifecycle']['version']}",
            expected_scope=scope or {"type": "job", "channel": job["channel"]},
        )

    def test_blank_assignee_auto_jobs_bind_once_in_two_channels(self):
        jobs = [
            self.jobs.create("Auto", "job", channel, "fixture", managed_by="orch", source_msg_id=index)
            for index, channel in enumerate(("general", "tools"))
        ]
        for job in jobs:
            self.assertEqual(job["assignee"], "")
            self.assertTrue(self.send(job, credential=False).startswith("Error:"))
            self.assertTrue(self.send(job, host="192.0.2.10").startswith("Error:"))
            self.assertTrue(self.send(job, actor="another", configured_actor="orchestrator").startswith("Error:"))
            self.assertTrue(self.send(job, channel="elsewhere").startswith("Error:"))
            self.assertEqual(self.jobs.get_messages(job["id"]), [])
            self.assertTrue(self.send(job).startswith("Sent"))

        self.jobs = JobStore(self.path)
        for initial in jobs:
            job = self.jobs.get(initial["id"])
            self.assertEqual(job["orch_coordinator"], "orchestrator")
            self.assertEqual(job["assignee"], "")
            self.assertTrue(self.send(job, actor="another").startswith("Error:"))
            self.assertIsNone(self.transition(job, "submit_brief", actor="another"))
            self.assertEqual(len(self.jobs.get_messages(job["id"])), 1)
            stage = self.transition(job, "submit_brief",
                                    scope={"type": "job", "channel": job["channel"], "assignee": "old-value"})
            self.assertEqual(stage["lifecycle"]["stage_id"], "orch.awaiting_approval")

    def test_transition_can_establish_auto_binding_before_first_send(self):
        job = self.jobs.create("Auto", "job", "tools", "fixture", managed_by="orch", source_msg_id=1)
        job = self.transition(job, "submit_brief")
        self.assertEqual(job["orch_coordinator"], "orchestrator")
        self.assertTrue(self.send(job, actor="another").startswith("Error:"))
        self.assertTrue(self.send(job).startswith("Sent"))

    def test_manual_assignee_and_concurrent_attachment_guard(self):
        job = self.jobs.create("Manual", "job", "tools", "fixture", assignee="orchestrator")
        self.assertTrue(self.send(job, actor="another").startswith("Error:"))
        self.assertTrue(self.send(job, credential=False).startswith("Sent"))
        self.assertIsNone(self.transition(job, "attach", actor="another"))
        attached = self.transition(job, "attach")
        self.assertEqual(attached["orch_coordinator"], "orchestrator")
        stale_scope = {key: job[key] for key in ("uid", "channel", "status", "type", "assignee")}
        self.assertIsNone(self.jobs.add_message(job["id"], "orchestrator", "stale manual send",
                                               expected_scope=stale_scope))
        self.assertTrue(self.send(attached, credential=False).startswith("Error:"))
        self.assertTrue(self.send(attached).startswith("Sent"))
