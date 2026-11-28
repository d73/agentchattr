"""Regression tests for conversation-first Orch intake."""
import asyncio
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from threading import Barrier
from unittest.mock import AsyncMock, Mock, patch

import app
import archive
import mcp_bridge
from jobs import JobStore
from registry import RuntimeRegistry
from router import Router
from store import MessageStore


class BrowserRequest:
    def __init__(self, payload, headers=None):
        self.payload = payload
        self.headers = headers or {}

    async def json(self):
        return self.payload


class OrchIntakeTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.stack = stack
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.job_path = root / "jobs.json"
        self.msg_path = root / "messages.jsonl"
        self.jobs = JobStore(str(self.job_path))
        self.messages = MessageStore(str(self.msg_path))
        self.registry = RuntimeRegistry(str(root))
        self.registry.seed({"codex": {}})
        self.writer = self.registry.register("codex", requested_name="writer")
        self.settings = {"channels": ["general", "tools", "notes"], "username": "Derek"}
        stack.enter_context(patch.multiple(app, jobs=self.jobs, store=self.messages,
                                           registry=self.registry, room_settings=self.settings,
                                           _broadcast=AsyncMock()))
        stack.enter_context(patch.multiple(mcp_bridge, store=self.messages, registry=self.registry,
                                           _touch_presence=Mock()))

    def message(self, text="Keep the selected row visible.", channel="tools", **fields):
        return self.messages.add("Derek", text, channel=channel, sender_kind="human", **fields)

    def confirm(self, source, **fields):
        request = BrowserRequest({"source_msg_id": source["id"], **fields})
        return asyncio.run(app.confirm_orch_intake(request))

    def proposal(self, source):
        result = mcp_bridge.chat_propose_job("visitor", "Keep row visible", channel=source["channel"],
                                             source_msg_id=source["id"])
        self.assertIn("Proposed job", result)
        return self.messages.get_recent(1)[0]

    def accept(self, proposal):
        return asyncio.run(app.create_job(BrowserRequest({
            "title": "Keep row visible", "channel": proposal["channel"], "anchor_msg_id": proposal["id"],
        })))

    def test_exact_browser_mention_offers_one_card_without_creating_a_job(self):
        source = self.message()
        mention = self.message("@Orch can you pick this up?", reply_to=source["id"])
        app._offer_orch_intake(mention)
        app._offer_orch_intake(mention)
        card = self.messages.get_recent(1)[0]
        self.assertEqual(card["type"], "decision")
        self.assertEqual(card["channel"], "tools")
        self.assertEqual(card["reply_to"], source["id"])
        self.assertEqual(card["metadata"]["orch_intake"]["source_msg_id"], source["id"])
        self.assertEqual(len(self.messages.get_recent()), 3)
        self.assertEqual(self.jobs.list_all(), [])

        generic = asyncio.run(app.resolve_decision(card["id"], BrowserRequest({"choice": "Create Orch Job"})))
        self.assertEqual(generic.status_code, 409)
        denied = asyncio.run(app.confirm_orch_intake(BrowserRequest(
            {"source_msg_id": source["id"], "card_msg_id": card["id"]},
            {"authorization": f"Bearer {self.writer['token']}"},
        )))
        self.assertEqual(denied.status_code, 403)

        result = self.confirm(source, card_msg_id=card["id"])
        self.assertTrue(result["created"])
        self.assertEqual(result["job"]["source_msg_id"], source["id"])
        self.assertEqual(result["job"]["lifecycle"]["stage_id"], "orch.drafting")
        self.assertEqual(result["job"]["lifecycle"]["managed_by"], "orch")
        self.assertFalse(self.confirm(source, card_msg_id=card["id"])["created"])
        self.assertEqual(self.messages.get_by_id(card["id"])["metadata"]["job_uid"], result["job"]["uid"])

    def test_broadcast_code_agent_system_and_similar_spellings_do_not_offer_intake(self):
        for text in ("@all", "`@orch`", "```python\n@orch\n```", "~~~\n@orch\n~~~",
                     "```python\n@orch", "@or`code`ch", "@orch-2", "me@orch", "@orch.invalid",
                     "@orchard"):
            with self.subTest(text=text):
                message = self.message(text)
                app._offer_orch_intake(message)
                self.assertEqual(self.messages.get_recent(1)[0]["id"], message["id"])
        for kind in ("agent", "system"):
            message = self.messages.add("writer", "@orch", channel="tools", sender_kind=kind)
            app._offer_orch_intake(message)
            self.assertEqual(self.messages.get_recent(1)[0]["id"], message["id"])

    def test_orch_chat_does_not_wake_the_agent_queue_or_default_targets(self):
        router = Router(["orch", "writer"], default_mention="all")
        agents = Mock()
        agents.trigger = AsyncMock()
        with (
            patch.multiple(app, router=router, agents=agents, session_engine=None,
                           broadcast=AsyncMock(), _unarchive_for_activity=Mock(return_value=False)),
            patch.object(mcp_bridge, "is_online", return_value=True),
        ):
            plain = self.message("@orch Please handle this.")
            for names in (["orch", "writer"], ["writer"]):
                with self.subTest(names=names):
                    router.update_agents(names)
                    asyncio.run(app._handle_new_message(plain))
                    agents.trigger.assert_not_awaited()
            mixed = self.message("@orch and @writer Please look at this.")
            asyncio.run(app._handle_new_message(mixed))
            self.assertEqual(agents.trigger.await_args.args, ("writer",))
            self.assertEqual(agents.trigger.await_count, 1)

    def test_manual_conversion_passes_the_original_source_to_its_writer(self):
        source = self.message()
        agents = Mock()
        agents.trigger = AsyncMock()
        with patch.object(app, "agents", agents):
            response = asyncio.run(app.trigger_agent_silent(BrowserRequest({
                "agent": "writer", "channel": source["channel"], "message": "Draft this",
                "source_msg_id": source["id"],
            })))
        self.assertTrue(response["ok"])
        self.assertIn(f"source_msg_id={source['id']}", agents.trigger.await_args.kwargs["prompt"])
        error = mcp_bridge.chat_propose_job("visitor", "Wrong channel", channel="notes",
                                            source_msg_id=source["id"])
        self.assertIn("does not exist in this channel", error)

    def test_channels_keep_distinct_jobs_and_wrong_card_source_is_rejected(self):
        first = self.message(channel="tools")
        second = self.message(channel="notes")
        mention = self.message("@orch", channel="notes", reply_to=first["id"])
        app._offer_orch_intake(mention)
        card = self.messages.get_recent(1)[0]
        self.assertEqual(card["reply_to"], mention["id"])
        self.assertEqual(self.confirm(first, card_msg_id=card["id"]).status_code, 409)
        self.assertEqual(self.confirm(first, proposal_writer="writer").status_code, 400)
        one = self.confirm(first, proposal_writer="orch")["job"]
        two = self.confirm(second)["job"]
        self.assertNotEqual(one["uid"], two["uid"])
        self.assertEqual(one["channel"], "tools")
        self.assertEqual(two["channel"], "notes")
        self.assertEqual(one["proposal_writer"], "orch")

    def test_proposal_acceptance_assigns_the_verified_writer_after_rename(self):
        source = self.message()
        proposal = self.messages.add(
            "former-name", "Job proposal", msg_type="job_proposal", channel=source["channel"],
            metadata={"status": "pending", "source_msg_id": source["id"]},
            sender_kind="agent", sender_identity=self.writer["identity_id"], sender_base="codex",
        )
        self.registry.register("codex", requested_name="former-name")
        job = self.accept(proposal)
        self.assertEqual(job["lifecycle"]["stage_id"], "orch.drafting")
        self.assertEqual(job["assignee"], "writer")
        self.assertEqual(job["proposal_author_identity"], self.writer["identity_id"])

    def test_orch_and_manual_acceptance_share_one_source_in_either_order(self):
        for manual_first in (True, False):
            with self.subTest(manual_first=manual_first):
                source = self.message()
                proposal = self.proposal(source)
                manual = self.accept(proposal) if manual_first else None
                orch = self.confirm(source)["job"]
                repeated = self.accept(proposal)
                self.assertEqual(orch["uid"], repeated["uid"])
                self.assertEqual(orch["lifecycle"]["managed_by"], "manual" if manual_first else "orch")
                self.assertEqual(orch["source_msg_id"], source["id"])
                stored = self.messages.get_by_id(proposal["id"])
                self.assertEqual(stored["metadata"]["job_uid"], orch["uid"])
                self.assertEqual(stored["metadata"]["source_msg_id"], source["id"])
                if manual_first:
                    self.assertEqual(manual["anchor_msg_id"], proposal["id"])
        self.assertEqual(len(self.jobs.list_all()), 2)
        self.assertEqual(sum(row["type"] == "job_created" for row in self.messages.get_recent()), 2)

    def test_legacy_explicit_source_is_recovered_without_changing_its_proposal_link(self):
        source = self.message()
        proposal = self.proposal(source)
        legacy = self.jobs.create("Existing", "job", "tools", "Derek",
                                  anchor_msg_id=proposal["id"], from_proposal=True)
        found = self.confirm(source)
        self.assertFalse(found["created"])
        self.assertEqual(found["job"]["uid"], legacy["uid"])
        self.assertEqual(found["job"]["anchor_msg_id"], proposal["id"])
        restored = JobStore(str(self.job_path))
        self.assertEqual(restored.get_for_source(source["id"])["uid"], legacy["uid"])

    def test_archive_remaps_the_source_without_reactivating_management(self):
        source = self.message()
        proposal = self.proposal(source)
        original = self.confirm(source)["job"]
        self.assertEqual(self.accept(proposal)["uid"], original["uid"])
        mention = self.message("@orch", reply_to=source["id"])
        app._offer_orch_intake(mention)
        card = self.messages.get_recent(1)[0]
        self.confirm(source, card_msg_id=card["id"])

        with tempfile.TemporaryDirectory() as directory:
            dest_root = Path(directory)
            dest_messages = MessageStore(str(dest_root / "messages.jsonl"))
            dest_jobs = JobStore(str(dest_root / "jobs.json"))
            dest_messages.add("Derek", "Existing message", channel="tools")
            report = archive.import_archive(
                archive.build_export(self.messages, self.jobs, None, None),
                dest_messages, dest_jobs, None, None, ["general", "tools", "notes"],
            )
            self.assertTrue(report["ok"])
            imported = {message["uid"]: message for message in dest_messages.get_recent()}
            imported_source = imported[source["uid"]]
            self.assertNotEqual(imported_source["id"], source["id"])
            job = dest_jobs.get_for_source(imported_source["id"])
            self.assertEqual(job["uid"], original["uid"])
            self.assertEqual(job["lifecycle"]["managed_by"], "manual")
            self.assertEqual(job["anchor_msg_id"], imported_source["id"])
            self.assertEqual(imported[proposal["uid"]]["metadata"]["job_id"], job["id"])
            self.assertEqual(imported[card["uid"]]["metadata"]["orch_intake"]["source_msg_id"],
                             imported_source["id"])
            self.assertEqual(imported[proposal["uid"]]["metadata"]["source_msg_id"], imported_source["id"])

    def test_closed_link_is_opened_without_reopening_or_changing_its_writer(self):
        source = self.message()
        created = self.confirm(source, proposal_writer="orch")["job"]
        closed = self.jobs.transition_job(
            created["id"], "cancel", actor="orch", actor_kind="orch",
            expected_uid=created["uid"], expected_version=0, operation_id="cancel-test",
        )
        reopened_store = JobStore(str(self.job_path))
        with patch.object(app, "jobs", reopened_store):
            repeated = self.confirm(source)
        self.assertFalse(repeated["created"])
        self.assertEqual(repeated["job"]["uid"], closed["uid"])
        self.assertEqual(repeated["job"]["lifecycle"], closed["lifecycle"])
        self.assertEqual(repeated["job"]["proposal_writer"], "orch")

    def test_concurrent_creates_commit_only_one_job_for_a_source(self):
        source = self.message()
        start = Barrier(2)

        def create(manager):
            start.wait(timeout=5)
            return self.jobs.create("Race", "job", "tools", "Derek",
                                    source_msg_id=source["id"], managed_by=manager)

        with ThreadPoolExecutor(max_workers=2) as worker:
            first, second = worker.map(create, ("orch", "manual"))
        self.assertEqual(first["uid"], second["uid"])
        self.assertEqual(len(JobStore(str(self.job_path)).list_all()), 1)


if __name__ == "__main__":
    unittest.main()
