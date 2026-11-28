"""Verify session-bound delivery and visibility for Job messages."""

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from agents import AgentTrigger, take_identity_trigger
import app
from jobs import JobStore
import mcp_bridge as bridge
from registry import RuntimeRegistry
import wrapper


class StopWatcher(BaseException):
    pass


class BoundJobMessageTests(unittest.TestCase):
    def test_presence_and_bound_job_delivery(self):
        parent = Path(__file__).resolve().parents[2]
        temp = parent if parent.name.casefold() == "temp" else parent / "Temp"
        temp.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp) as directory:
            registry = RuntimeRegistry(data_dir=directory)
            registry.seed({"codex": {}, "claude": {}})
            first = registry.register("codex", requested_name="manual-a")
            other = registry.register("codex", requested_name="manual-b")
            reviewer = registry.register("claude")
            jobs = JobStore(str(Path(directory) / "jobs.json"))
            job = jobs.create("Fixture", "job", "general", "fixture", status="open")["id"]
            triggers = Mock()
            triggers.get_status.return_value = {
                first["name"]: {"busy": True},
                other["name"]: {"busy": False},
                reviewer["name"]: {"busy": False},
            }
            live = {item["name"]: time.time() for item in (first, other, reviewer)}
            with (patch.object(bridge, "registry", registry), patch.object(bridge, "jobs", jobs),
                  patch.object(bridge, "agents", triggers), patch.object(bridge, "router", None),
                  patch.object(bridge, "_presence", live)):
                self.assertTrue(bridge.chat_send("fixture", "Assignment", job_id=job,
                                                recipient=other["name"],
                                                recipient_identity=other["identity_id"]).startswith("Sent"))

                def auth(worker):
                    context = Mock()
                    context.request_context.request.headers = {"authorization": f"Bearer {worker['token']}"}
                    return context

                self.assertEqual(1, len(json.loads(bridge.chat_read(sender=first["name"], job_id=job,
                                                                   ctx=auth(first)))))
                self.assertEqual("Assignment", json.loads(bridge.chat_read(sender=other["name"], job_id=job,
                                                                           ctx=auth(other)))[1]["text"])
                bridge.chat_send(other["name"], "Ack", job_id=job, ctx=auth(other))
                events = json.loads(bridge.chat_read(sender="fixture", job_id=job))
                self.assertEqual(other["identity_id"], events[-1]["sender_identity"])
                registry.deregister(other["name"])
                replacement = registry.register("codex", requested_name=other["name"])
                self.assertTrue(bridge.chat_send("fixture", "Stale assignment", job_id=job,
                                                recipient=other["name"],
                                                recipient_identity=other["identity_id"]).startswith("Error"))
                replacement_messages = json.loads(bridge.chat_read(sender=other["name"], job_id=job,
                                                                    ctx=auth(replacement)))
                self.assertNotIn("Assignment", [message["text"] for message in replacement_messages])


class SuggestionAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.data_dir = Path(directory)
        self.registry = RuntimeRegistry(data_dir=directory)
        self.registry.seed({"codex": {}})
        self.author = self.registry.register("codex", requested_name="original-author")
        self.jobs = JobStore(str(self.data_dir / "jobs.json"))
        self.job_id = self.jobs.create("Fixture", "job", "example", "fixture", status="open")["id"]
        self.enterContext(patch.object(app, "jobs", self.jobs))
        self.enterContext(patch.object(app, "agents", AgentTrigger(self.registry, directory)))

    async def accept(self, author_identity):
        message = self.jobs.add_message(self.job_id, self.author["name"], "Suggestion",
                                        msg_type="suggestion", sender_identity=author_identity)
        request = Mock(json=AsyncMock(return_value={"resolution": "accepted"}))
        result = await app.resolve_job_message(self.job_id, message["id"], request)
        self.assertEqual(result, {"ok": True, "resolution": "accepted"})
        self.assertEqual(self.jobs.get(self.job_id)["messages"][message["id"]]["resolved"], "accepted")

    async def test_wakes_original_author_after_rename_and_name_reuse(self):
        self.registry.rename(self.author["name"], "renamed-author")
        replacement = self.registry.register("codex", requested_name=self.author["name"])
        await self.accept(self.author["identity_id"])
        self.assertIsNone(take_identity_trigger(self.data_dir, replacement["identity_id"]))

        prompts = []

        def inject(prompt):
            prompts.append(prompt)
            raise StopWatcher()

        # Accept before the watcher notices a second rename.
        self.registry.rename("renamed-author", "latest-author")
        with (patch.object(wrapper.time, "sleep"), patch.object(wrapper, "_fetch_role", return_value=""),
              patch.object(wrapper, "_fetch_active_rules", return_value=None), self.assertRaises(StopWatcher)):
            wrapper._queue_watcher(
                lambda: ("renamed-author", self.data_dir / "renamed-author_queue.jsonl"),
                inject, get_identity_id_fn=lambda: self.author["identity_id"],
            )
        self.assertIn(f"job_id={self.job_id}", prompts[0])
        self.assertIn("your suggestion was accepted", prompts[0])
        self.assertIsNone(take_identity_trigger(self.data_dir, self.author["identity_id"]))

    async def test_does_not_wake_replacement_for_expired_or_legacy_author(self):
        self.registry.deregister(self.author["name"])
        replacement = self.registry.register("codex", requested_name=self.author["name"])
        await self.accept(self.author["identity_id"])
        await self.accept(None)
        self.assertIsNone(take_identity_trigger(self.data_dir, replacement["identity_id"]))
        self.assertIsNone(take_identity_trigger(self.data_dir, self.author["identity_id"]))
        self.assertFalse((self.data_dir / f"{replacement['name']}_queue.jsonl").exists())
