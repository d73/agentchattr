import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app
import mcp_bridge
from jobs import JobStore
from orch_events import OrchEvents, QUEUE_LIMIT, RESET_PATH
from run import _publish_orch_gate
from store import MessageStore


PARENT = Path(__file__).resolve().parents[2]
WORKSPACE_TEMP = PARENT if PARENT.name.casefold() == "temp" else PARENT / "Temp"


class OrchWakeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = Path(self.enterContext(TemporaryDirectory(dir=WORKSPACE_TEMP if WORKSPACE_TEMP.is_dir() else None)))
        self.jobs = JobStore(root / "jobs.json")
        self.messages = MessageStore(str(root / "messages.jsonl"))
        self.events = OrchEvents("general", "orch", self.jobs, root / "receipts.sqlite3")
        self.jobs.on_change(self.events.job_changed)
        self.messages.on_message(self.events.message_added, committed_only=True)

    async def test_committed_sources_cross_channel_scope_and_roster_deduplication(self):
        async with self.events.subscribe(channel="general", coordinator="orch") as stream:
            async def expect(kind, **fields):
                self.assertEqual({"kind": kind, **fields}, await asyncio.wait_for(anext(stream), 1))

            self.jobs.create("Unrelated", "job", "elsewhere", "operator", assignee="other")
            source = self.messages.add("operator", "Fixture source", channel="tools", sender_kind="human")
            job = await asyncio.to_thread(self.jobs.create, "Scoped", "job", "tools", "operator",
                                          managed_by="orch", source_msg_id=source["id"])
            fields = {"channel": "tools", "job_uid": job["uid"]}
            await expect("job", **fields)
            self.jobs.add_message(job["id"], "worker", "orch-event fixture")
            await expect("job_message", **fields)
            job = self.jobs.transition_job(job["id"], "submit_brief", actor="orch", actor_kind="orch",
                                           expected_uid=job["uid"], expected_version=0, operation_id="brief")
            await expect("job", **fields)
            card = _publish_orch_gate(self.jobs, self.messages, job["uid"], coordinator="orch", gate="brief",
                                      revision=1, choices=["Approve", "Revise", "Cancel"],
                                      text="Gate", operation_id="publish")
            for _ in range(2):
                self.jobs.resolve_orch_gate(job["uid"], decision_id=card["decision_id"], gate="brief",
                                            revision=1, choice="Approve", actor="operator")
            await expect("decision", **fields)

            worker = {"c1": {"state": "active", "base": "codex", "identity_id": "registration"}}
            status = {"c1": {"available": True, "busy": False, "needs_input": False}}
            self.events.roster_changed(worker, status)
            self.events.roster_changed(worker, status)
            await expect("roster")
            status["c1"]["needs_input"] = True
            self.events.roster_changed(worker, status)
            await expect("roster")

            for channel, text in (("elsewhere", "@orch"), ("tools", "`@orch`"), ("tools", "@orch.invalid"),
                                  ("tools", "@orch /reset")):
                self.messages.add("agent", text, channel=channel, sender_kind="agent")
            with patch.object(self.messages, "_log_repair_required", True):
                self.messages.add("agent", "@orch failed", channel="tools", sender_kind="agent")
            mention = self.messages.add("agent", "@orch please check", channel="tools", sender_kind="agent")
            await expect("mention", channel="tools", message_id=mention["id"])

            for event in ("approve_brief", "plan_ready", "start", "pr_ready", "complete"):
                job = self.jobs.transition_job(job["id"], event, actor="orch", actor_kind="orch",
                                               expected_uid=job["uid"], expected_version=job["lifecycle"]["version"],
                                               operation_id=event)
                await expect("job", **fields)
            scope = {"expected_uid": job["uid"], "expected_version": job["lifecycle"]["version"]}
            reopen = self.jobs.request_reopen(job["id"], requester="operator", reason="Correction", **scope)
            await expect("job", **fields)
            self.jobs.decide_reopen(job["id"], reopen["request"]["id"], actor="operator", role="owner",
                                    decision="approve", source="local-browser", **scope)
            await expect("job", **fields)
            await expect("decision", **fields)

    async def test_overflow_requires_resubscription_and_releases_the_old_subscription(self):
        with self.assertRaises(ValueError):
            async with self.events.subscribe(channel="elsewhere", coordinator="orch"):
                self.fail("Accepted the wrong subscription scope")
        async with self.events.subscribe(channel="general", coordinator="orch") as stream:
            for index in range(QUEUE_LIMIT + 10):
                self.messages.add("agent", "@orch update", sender_kind="agent")
            self.assertEqual({"kind": "resync_required"}, await asyncio.wait_for(anext(stream), 1))
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)
        self.assertFalse(self.events.subscribers)
        async with self.events.subscribe(channel="general", coordinator="orch") as stream:
            message = self.messages.add("agent", "@orch after reconnect", sender_kind="agent")
            self.assertEqual(message["id"], (await asyncio.wait_for(anext(stream), 1))["message_id"])


class OrchBrowserResetTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(
            dir=WORKSPACE_TEMP if WORKSPACE_TEMP.is_dir() else None,
        )))
        self.jobs = JobStore(self.root / "jobs.json")
        self.messages = MessageStore(str(self.root / "messages.jsonl"))
        self.events = OrchEvents("general", "orch", self.jobs, self.root / "receipts.sqlite3")
        self.token_file = self.root / ".orchestrator.env"
        self.token = "disposable-orch-closeout-token-for-tests"
        self.token_file.write_text(f"ORCH_CLOSEOUT_TOKEN={self.token}\n", encoding="utf-8")
        self.api = api = FastAPI()
        api.state.orch_events = self.events
        api.add_api_websocket_route("/ws", app.websocket_endpoint)
        api.add_api_route(RESET_PATH, app.read_orch_reset_commands, methods=["GET"])
        config = {"operator": {"identity": "verified-operator"}}
        self.enterContext(patch.multiple(app, app=api, jobs=self.jobs, store=self.messages, config=config,
                                         room_settings={"channels": ["general", "other"]}, session_token=None,
                                         _initialize_websocket=AsyncMock()))
        self.enterContext(patch.object(mcp_bridge, "orch_job_auth", ("orch", self.token_file)))
        app._install_security_middleware("fixture-session", config)
        self.client = self.enterContext(TestClient(api, client=("127.0.0.1", 50000)))
        self.service_headers = {"Authorization": f"Bearer {self.token}"}

    def test_reset_is_attributed_durable_and_requires_local_browser_and_service_credentials(self):
        origin = {"Origin": "http://127.0.0.1:8300"}
        with self.client.websocket_connect("/ws?token=fixture-session", headers=origin) as ws:
            ws.send_json({"type": "message", "text": "@orch /reset", "sender": "untrusted-label", "channel": "general"})
            ws.send_json({"type": "message", "text": "@orch /reset", "channel": "other"})
            self.assertEqual("message_error", ws.receive_json()["type"])
        for headers in ({**origin, "X-Forwarded-Proto": "http"}, {**origin, "Authorization": "Bearer fake"},
                        {"Origin": "http://external.example:8300"}, {}):
            with self.client.websocket_connect("/ws?token=fixture-session", headers=headers) as ws:
                ws.send_json({"type": "message", "text": "@orch /reset", "channel": "general"})
                self.assertEqual("message_error", ws.receive_json()["type"])
        with TestClient(self.api, client=("192.0.2.20", 50000)) as remote:
            with self.assertRaises(WebSocketDisconnect):
                with remote.websocket_connect("/ws?token=fixture-session", headers=origin) as ws:
                    ws.receive_json()
            self.assertEqual(403, remote.get(RESET_PATH, headers=self.service_headers).status_code)
        valid, = self.messages.get_recent()
        self.assertEqual("verified-operator", valid["sender"])
        self.assertEqual("chat", valid["type"])
        self.messages.add("verified-operator", "@orch /reset", sender_kind="agent")
        self.assertEqual(403, self.client.get(RESET_PATH).status_code)
        self.assertEqual(403, self.client.get(RESET_PATH, headers={**self.service_headers, "Forwarded": "for=127.0.0.1"}).status_code)
        fresh = OrchEvents("general", "orch", self.jobs, self.events.receipt_path)
        app.app.state.orch_events = fresh
        self.messages.delete([valid["id"]])
        self.assertEqual([valid["id"]], self.client.get(RESET_PATH, headers=self.service_headers).json())
        self.assertEqual([], self.client.get(RESET_PATH, headers=self.service_headers,
                                           params={"after_message_id": valid["id"]}).json())
        self.assertEqual([], OrchEvents("general", "other", self.jobs, self.events.receipt_path).reset_ids(-1))
