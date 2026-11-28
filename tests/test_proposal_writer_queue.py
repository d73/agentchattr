"""Manual conversion dispatches to one idle instance of the selected agent type."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import app
from agents import AgentTrigger


def instance(name, base, slot, *, state="active"):
    return {"name": name, "base": base, "slot": slot, "state": state, "identity_id": "identity-" + name}


class ProposalWriterQueueTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.registry = Mock()
        self.registry.is_active_identity.return_value = True
        self.registry.get_all.return_value = {
            "c3": instance("c3", "codex", 3),
            "writing": instance("writing", "codex", 1),
            "review": instance("review", "claude", 1),
        }
        self.trigger = AgentTrigger(self.registry, self.root)
        self.status = {
            name: {"available": True, "busy": False, "needs_input": False}
            for name in self.registry.get_all.return_value
        }
        status_patch = patch.object(self.trigger, "get_status", return_value=self.status)
        status_patch.start()
        self.addCleanup(status_patch.stop)

    def request(self, source=42):
        return self.trigger.queue_proposal_writer(
            base="codex", channel="review", prompt=f"Draft proposal for {source}", source_msg_id=source,
        )

    def delivered(self, name):
        path = self.root / "identity_triggers" / ("identity-" + name)
        return [json.loads(file.read_text(encoding="utf-8")) for file in sorted(path.glob("*.json"))]

    def test_uses_the_lowest_idle_slot_of_the_selected_type(self):
        self.assertTrue(self.request())
        self.assertEqual([message["source_msg_id"] for message in self.delivered("writing")], [42])
        self.assertEqual(self.delivered("c3"), [])
        self.assertEqual(self.delivered("review"), [])

    def test_queues_while_busy_and_recovers_pending_work_after_restart(self):
        self.status["writing"]["busy"] = True
        self.status["c3"]["busy"] = None
        self.assertFalse(self.request())
        self.assertEqual(self.delivered("writing"), [])
        self.trigger = AgentTrigger(self.registry, self.root)
        self.status["c3"]["busy"] = False
        with patch.object(self.trigger, "get_status", return_value=self.status):
            self.trigger.dispatch_proposal_writers()
            self.trigger.dispatch_proposal_writers()
        self.assertEqual([message["source_msg_id"] for message in self.delivered("c3")], [42])
        self.assertEqual(self.delivered("review"), [])

    def test_does_not_refill_a_recently_claimed_spool_before_activity_updates(self):
        self.status["c3"]["available"] = False
        self.assertTrue(self.request())
        for file in (self.root / "identity_triggers" / "identity-writing").glob("*.json"):
            file.unlink()
        self.assertFalse(self.request(43))
        self.assertEqual(self.delivered("writing"), [])

    def test_request_uses_the_source_channel_and_returns_the_queued_status(self):
        request = Mock()
        request.json = AsyncMock(return_value={"writer_base": "codex", "source_msg_id": 42, "channel": "other"})
        store = Mock()
        store.get_by_id.return_value = {"id": 42, "channel": "review"}
        self.status["writing"]["busy"] = True
        self.status["c3"]["busy"] = True
        with patch.multiple(app, store=store, agents=self.trigger):
            self.assertEqual(asyncio.run(app.request_job_proposal(request)), {"status": "queued"})
        [pending] = (self.root / "proposal_writer_queue").glob("*.json")
        record = json.loads(pending.read_text(encoding="utf-8"))
        self.assertEqual(record["channel"], "review")
        self.assertIn("source_msg_id=42", record["prompt"])
