import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

from agents import AgentTrigger, take_trigger
import app
from registry import RuntimeRegistry
from router import Router
from session_engine import SessionEngine
from session_store import SessionStore
from store import MessageStore


class SessionParticipantIdentityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.registry = RuntimeRegistry(data_dir=str(self.root))
        self.registry.seed({"codex": {}})
        self.original = self.registry.register("codex", requested_name="worker")
        self.sessions = SessionStore(str(self.root / "runs.json"))
        self.sessions.save_custom_template({"id": "test", "name": "Test", "roles": ["worker"],
            "phases": [{"name": "Work", "participants": ["worker"], "is_output": True}]})
        self.messages = MessageStore(str(self.root / "messages.jsonl"))
        self.agent_trigger = AgentTrigger(self.registry, str(self.root))
        self.engine = SessionEngine(self.sessions, self.messages, self.agent_trigger, self.registry)
        self.identity = self.original["identity_id"]
        self.timers = []
        def timer(delay, callback, args):
            return Mock(start=lambda: self.timers.append(lambda: callback(*args)))
        self.enterContext(patch("session_engine.threading.Timer", timer))

    def take(self, name, identity):
        return take_trigger(self.root / f"{name}_queue.jsonl", identity)

    def test_renamed_participant_keeps_the_turn_after_reload_and_chat_id_reuse(self):
        self.engine.start_session("test", "general", {"worker": "worker"}, "human")
        self.registry.rename("worker", "renamed")
        replacement = self.registry.register("codex", requested_name="worker")
        self.assertIsNone(self.take("worker", replacement["identity_id"]))
        self.assertIn("YOUR ROLE: worker", self.take("renamed", self.identity)["prompt"])
        self.engine._store = SessionStore(str(self.root / "runs.json"))
        self.assertEqual(("renamed", self.identity), self.engine.get_allowed_wake_target("general"))
        self.assertEqual("renamed", self.engine.get_active("general")["current_agent"])
        self.messages.add("worker", "Wrong agent", sender_kind="agent", sender_identity=replacement["identity_id"])
        self.assertEqual([], self.timers)
        response = self.messages.add("renamed", "Done", sender_kind="agent", sender_identity=self.identity)
        self.timers.pop()()
        self.assertEqual(response["id"], self.engine._store.get(1)["output_message_id"])

    def test_pending_wake_retries_only_on_original_registration_after_rename(self):
        with patch.object(self.agent_trigger, "trigger_sync", side_effect=OSError("write failed")), \
             self.assertLogs("session_engine", level="ERROR"):
            self.engine.start_session("test", "general", {"worker": "worker"}, "human")
        self.registry.rename("worker", "renamed")
        replacement = self.registry.register("codex", requested_name="worker")
        self.engine._store = SessionStore(str(self.root / "runs.json"))
        self.engine.retry_pending_wakes("worker", replacement["identity_id"])
        self.assertTrue(self.engine._store.get(1)["wake_pending"])
        self.assertIsNone(self.take("worker", replacement["identity_id"]))
        self.engine.retry_pending_wakes("renamed", self.identity)
        self.engine.retry_pending_wakes("renamed", self.identity)
        self.assertIn("YOUR ROLE: worker", self.take("renamed", self.identity)["prompt"])
        self.assertIsNone(self.take("renamed", self.identity))
        self.assertFalse(self.engine._store.get(1).get("wake_pending"))

    def test_choice_answer_wakes_the_renamed_author_instead_of_the_replacement(self):
        self.engine.start_session("test", "general", {"worker": "worker"}, "human")
        self.take("worker", self.identity)
        card = self.messages.add("worker", "Which?", msg_type="decision", sender_kind="agent",
                                 sender_identity=self.identity, metadata={"choices": ["Yes"]})
        self.registry.rename("worker", "renamed")
        replacement = self.registry.register("codex", requested_name="worker")
        request = Mock(json=AsyncMock(return_value={"choice": "Yes"}))
        with patch.multiple(app, store=self.messages, registry=self.registry, agents=self.agent_trigger,
                            router=Router(agent_names=["worker", "renamed"]), session_engine=self.engine,
                            room_settings={"username": "human"}, ws_clients=set()):
            self.assertTrue(asyncio.run(app.resolve_decision(card["id"], request))["ok"])
            answer = self.messages.get_recent(count=1)[0]
            self.assertEqual("@renamed Yes", answer["text"])
            asyncio.run(app._handle_new_message(answer))
        self.assertIsNotNone(self.take("renamed", self.identity))
        self.assertIsNone(self.take("worker", replacement["identity_id"]))
        self.assertIsNone(self.sessions.get(1)["choice_message_id"])

    def test_choice_for_offline_participant_waits_for_original_registration(self):
        self.engine.start_session("test", "general", {"worker": "worker"}, "human")
        self.take("worker", self.identity)
        card = self.messages.add("worker", "Which?", msg_type="decision", sender_kind="agent",
                                 sender_identity=self.identity, metadata={"choices": ["Yes"]})
        self.registry.rename("worker", "renamed")
        replacement = self.registry.register("codex", requested_name="worker")
        self.registry.deregister("renamed", reclaimable=True)
        request = Mock(json=AsyncMock(return_value={"choice": "Yes"}))
        with patch.multiple(app, store=self.messages, registry=self.registry, agents=self.agent_trigger,
                            router=Router(agent_names=["worker"]), session_engine=self.engine,
                            room_settings={"username": "human"}, ws_clients=set()):
            self.assertTrue(asyncio.run(app.resolve_decision(card["id"], request))["ok"])
            answer = self.messages.get_recent(count=1)[0]
            self.assertEqual("Yes", answer["text"])
            asyncio.run(app._handle_new_message(answer))
        self.assertTrue(self.sessions.get(1)["wake_pending"])
        self.assertIsNone(self.take("worker", replacement["identity_id"]))
        reconnected = self.registry.resolve_token(self.original["token"])
        self.engine.retry_pending_wakes(reconnected["name"], reconnected["identity_id"])
        self.assertIn("YOUR ROLE: worker", self.take("renamed", self.identity)["prompt"])
