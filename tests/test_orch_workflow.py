"""Regression coverage for the message bindings behind conversation-first Orch."""

from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app
import mcp_bridge
from jobs import JobStore
from orch_workflow import history, list_orch_workflow_actions, publish_orch_workflow
from orch_workflow import read_orch_workflow, record_reply, reply_metadata
from run import _publish_orch_gate
from store import MessageStore


class OrchWorkflowTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(TemporaryDirectory()))
        self.jobs = JobStore(self.root / "jobs.json")
        self.store = MessageStore(self.root / "messages.jsonl")
        self.job = self.create_job("tools")

    def create_job(self, channel):
        source = self.store.add("owner", "Please work on this", channel=channel, sender_kind="human")
        job = self.jobs.create("Workflow fixture", "job", channel, "owner", source_msg_id=source["id"],
                               managed_by="orch", proposal_writer="orch")
        # The configured coordinator can claim a fresh managed Job when it
        # publishes its first prompt. Attach is only a legacy transition.
        return job

    def transition(self, job, event):
        return self.jobs.transition_job(job["id"], event, actor="orch", actor_kind="orch",
                                        expected_uid=job["uid"], expected_version=job["lifecycle"]["version"],
                                        operation_id=f"{event}:{job['lifecycle']['version']}")

    def publish(self, job=None, **changes):
        if job is None:
            job = self.job
        payload = {"coordinator": "orch", "operation_id": "intake", "kind": "intake",
                   "revision": None, "parent_message_id": None, "text": "Please draft a brief", "recipient": "agy"}
        payload.update(changes)
        return publish_orch_workflow(self.jobs, self.store, job["uid"], **payload)

    def activate(self, job=None):
        return self.jobs.activate_orch_workflow((job or self.job)["uid"], coordinator="orch")

    def reply(self, channel, parent_id, text="Please keep the same scope", *, ask=False):
        metadata = reply_metadata(self.jobs, self.store, channel, parent_id)
        if ask:
            metadata["orch_workflow"]["requires_reply"] = True
        message = self.store.add("agy" if ask else "owner", text, channel=channel, reply_to=parent_id,
                                 sender_kind="agent" if ask else "human", metadata=metadata)
        record_reply(self.jobs, message)
        return message

    def read(self, job, prompt_id, coordinator="orch"):
        return read_orch_workflow(self.jobs, self.store, job["uid"], coordinator=coordinator, prompt_id=prompt_id)

    def status(self, coordinator="orch"):
        return list_orch_workflow_actions(self.jobs, self.store, coordinator=coordinator)

    def test_operation_replay_wrong_coordinator_parent_job_and_channel(self):
        neighbor = self.create_job("tools")
        other = self.create_job("elsewhere")
        self.activate()
        self.activate(neighbor)
        self.assertIsNone(self.publish(parent_message_id=neighbor["source_msg_id"]))
        self.assertIsNone(self.publish(parent_message_id=other["source_msg_id"]))
        prompt = self.publish()
        self.assertIsNone(self.publish(coordinator="other"))
        next_prompt = self.publish(neighbor)
        self.assertEqual(prompt, self.publish())
        with self.assertRaises(ValueError):
            self.publish(text="Changed replay")
        self.assertIsNone(self.read(self.job, prompt["prompt_id"], coordinator="other"))
        self.assertIsNone(self.read(neighbor, prompt["prompt_id"]))
        with self.assertRaises(ValueError):
            self.reply("elsewhere", prompt["prompt_id"])
        reply = self.reply("tools", prompt["prompt_id"])
        self.assertEqual([reply["id"]], [item["id"] for item in self.read(self.job, prompt["prompt_id"])["replies"]])
        self.assertEqual([], self.read(neighbor, next_prompt["prompt_id"])["replies"])
        self.assertEqual(self.job["source_msg_id"], self.store.get_by_id(prompt["prompt_id"])["reply_to"])

    def test_action_tracks_exact_question_and_survives_restart_rename_and_removed_message(self):
        self.activate()
        prompt_id = self.publish()["prompt_id"]
        self.assertEqual([], self.status())
        first = self.reply("tools", prompt_id, "Which checks?", ask=True)
        second = self.reply("tools", prompt_id, "Which branch?", ask=True)
        self.assertEqual([first["id"], second["id"]], [entry["message_id"] for entry in self.status()])
        self.reply("tools", second["id"])
        self.assertEqual([first["id"]], [entry["message_id"] for entry in self.status()])

        self.jobs.rename_channel("tools", "renamed")
        self.store.rename_channel("tools", "renamed")
        self.jobs = JobStore(self.root / "jobs.json")
        self.store = MessageStore(self.root / "messages.jsonl")
        self.assertEqual("renamed", self.status()[0]["channel"])
        self.assertEqual("renamed", self.publish()["channel"])
        self.assertTrue(self.jobs.get(self.job["id"])["orch_workflow_active"])

        self.store.delete([first["id"]])
        self.assertEqual([], self.status())
        payload = history(self.jobs, self.store, self.job["uid"])
        self.assertIsNone(payload["current_action_message_id"])
        self.assertIn(first["id"], [row["message_id"] for row in payload["entries"]])

    def test_preexisting_gate_stays_valid_after_activation_and_revise_clears_attention(self):
        self.job = self.transition(self.job, "submit_brief")
        first = _publish_orch_gate(self.jobs, self.store, self.job["uid"], coordinator="orch",
                                   gate="brief", revision=1, choices=["Approve", "Revise"],
                                   text="Review the brief", operation_id="brief:1")
        self.assertEqual([first["decision_id"]], [entry["message_id"] for entry in self.status()])
        self.assertFalse(history(self.jobs, self.store, self.job["uid"])["workflow_active"])
        self.assertIsNone(self.jobs.activate_orch_workflow(self.job["uid"], coordinator="unbound"))
        self.assertTrue(self.jobs.activate_orch_workflow(self.job["uid"], coordinator="OrCh")["workflow_active"])
        self.assertEqual("orch", self.jobs.get(self.job["id"])["orch_coordinator"])
        resolved = self.jobs.resolve_orch_gate(self.job["uid"], decision_id=first["decision_id"],
                                              gate="brief", revision=1, choice="Revise", actor="owner")
        self.assertEqual("Revise", resolved["choice"])
        self.assertEqual([], self.status())
        self.job = self.transition(self.job, "revise_brief")
        prompt = self.publish(kind="revise", revision=1, parent_message_id=first["decision_id"])
        self.assertEqual([], self.status())
        self.job = self.transition(self.job, "submit_brief")
        second = _publish_orch_gate(self.jobs, self.store, self.job["uid"], coordinator="orch",
                                    gate="brief", revision=2, choices=["Approve", "Revise"],
                                    text="Review the revised brief", operation_id="brief:2")
        self.assertEqual(prompt["prompt_id"], self.store.get_by_id(second["decision_id"])["reply_to"])
        self.assertEqual([second["decision_id"]], [entry["message_id"] for entry in self.status()])

    def test_history_routes_authorize_and_resolve_exact_messages_and_intake_is_suppressed(self):
        other = self.create_job("elsewhere")
        self.activate()
        prompt_id = self.publish()["prompt_id"]
        reply = self.reply("tools", prompt_id, "@orch please keep the same scope")
        web = FastAPI()
        history_path = "/api/jobs/{job_uid}/orch-history"
        web.add_api_route(history_path, app.get_orch_history, methods=["GET"])
        web.add_api_route(history_path + "/{message_id}", app.get_orch_history_context, methods=["GET"])
        with patch.multiple(app, app=web, jobs=self.jobs, store=self.store, session_token=None):
            app._install_security_middleware("browser-fixture", {})
            app._offer_orch_intake(reply)
            self.assertEqual(reply["id"], self.store.last_id)
            with TestClient(web, client=("127.0.0.1", 50000)) as client:
                path = history_path.format(job_uid=self.job["uid"])
                self.assertEqual(403, client.get(path).status_code)
                token = {"X-Session-Token": "browser-fixture"}
                self.assertTrue(client.get(path, headers=token).json()["workflow_active"])
                result = client.get(f"{path}/{reply['id']}", headers=token).json()
                self.assertEqual(reply["id"], result["message"]["id"])
                wrong = history_path.format(job_uid=other["uid"])
                self.assertEqual(404, client.get(f"{wrong}/{reply['id']}", headers=token).status_code)
                self.store.delete([reply["id"]])
                self.assertEqual(404, client.get(f"{path}/{reply['id']}", headers=token).status_code)

    def test_browser_created_job_activation_is_an_atomic_coordinator_claim(self):
        source = self.store.add("owner", "A new Job in another channel", channel="notes", sender_kind="human")
        web = FastAPI()
        web.add_api_route("/api/jobs/orch-intake", app.confirm_orch_intake, methods=["POST"])
        with patch.multiple(app, app=web, jobs=self.jobs, store=self.store, session_token=None,
                            room_settings={"channels": ["notes"], "username": "owner"}):
            app._install_security_middleware("browser-fixture", {})
            with TestClient(web, client=("127.0.0.1", 50000)) as client:
                response = client.post("/api/jobs/orch-intake", json={"source_msg_id": source["id"]},
                                       headers={"X-Session-Token": "browser-fixture"})
                self.assertEqual(200, response.status_code)
                job = response.json()["job"]
        self.assertNotIn("orch_coordinator", job)
        self.assertIsNone(self.publish(job))
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                lambda writer: self.jobs.activate_orch_workflow(job["uid"], coordinator=writer),
                ["orch", "another"],
            ))
        self.assertEqual(1, sum(result is not None for result in results))
        winner = self.jobs.get(job["id"])["orch_coordinator"]
        self.assertEqual(next(result for result in results if result),
                         self.jobs.activate_orch_workflow(job["uid"], coordinator=winner))
        self.assertEqual("notes", self.publish(job, coordinator=winner)["channel"])
        manual = self.jobs.create("Manual", "job", "notes", "owner")
        self.assertIsNone(self.jobs.activate_orch_workflow(manual["uid"], coordinator=winner))

    def test_agent_question_and_browser_reply_share_the_real_message_paths(self):
        self.activate()
        prompt_id = self.publish()["prompt_id"]
        actor = {"name": "agy", "identity_id": "registered-writer", "base": "gemini"}
        with patch.multiple(mcp_bridge, store=self.store, jobs=self.jobs, registry=None):
            for identity, name in ((None, "visitor"), ({**actor, "name": "another"}, "another")):
                with patch.object(mcp_bridge, "_resolve_tool_actor", return_value=(name, identity, None)):
                    denied = mcp_bridge.chat_send(name, "Untrusted question?", channel="tools",
                                                  reply_to=prompt_id, workflow_requires_reply=True)
                    self.assertTrue(denied.startswith("Error:"), denied)
            with patch.object(mcp_bridge, "_resolve_tool_actor", return_value=("agy", actor, None)):
                sent = mcp_bridge.chat_send("agy", "Keep the prior scope?", channel="tools",
                                            reply_to=prompt_id, workflow_requires_reply=True)
        self.assertTrue(sent.startswith("Sent "), sent)
        question_id = self.store.last_id
        self.assertEqual(question_id, self.status()[0]["message_id"])

        web = FastAPI()
        web.add_api_websocket_route("/ws", app.websocket_endpoint)
        with patch.multiple(app, jobs=self.jobs, store=self.store, session_token="browser-fixture",
                            room_settings={"channels": ["tools"], "username": "owner"},
                            ws_clients=set(), _initialize_websocket=AsyncMock()):
            with TestClient(web, client=("127.0.0.1", 50000)) as client:
                with client.websocket_connect("/ws?token=browser-fixture") as socket:
                    socket.send_json({"type": "message", "channel": "elsewhere", "reply_to": question_id,
                                      "text": "Crossed reply"})
                    self.assertEqual("message_error", socket.receive_json()["type"])
                    socket.send_json({"type": "message", "channel": "tools", "reply_to": question_id,
                                      "sender": "forged-label", "text": "@orch same scope"})
                    socket.send_json({"type": "message", "channel": "elsewhere", "reply_to": question_id,
                                      "text": "Crossed reply"})
                    self.assertEqual("message_error", socket.receive_json()["type"])
        replies = self.read(self.job, prompt_id)["replies"]
        self.assertEqual(["agy", "owner"], [reply["sender"] for reply in replies])
        self.assertEqual("registered-writer", replies[0]["sender_identity"])
        self.assertEqual("@orch same scope", self.store.get_by_id(self.store.last_id)["text"])
        self.assertEqual([], self.status())
