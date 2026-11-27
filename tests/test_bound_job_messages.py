"""Verify session-bound delivery and visibility for Job messages."""

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from jobs import JobStore
import mcp_bridge as bridge
from registry import RuntimeRegistry


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
