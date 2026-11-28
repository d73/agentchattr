from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app
from jobs import JobStore
from run import _publish_orch_gate
from store import MessageStore


class OrchGateApprovalTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.directory = Path(stack.enter_context(TemporaryDirectory()))
        self.jobs = JobStore(str(self.directory / "jobs.json"))
        self.store = MessageStore(str(self.directory / "messages.jsonl"))
        self.config = {"operator": {"identity": "fixture-operator"}, "server": {"data_dir": str(self.directory)}}
        self.browser = {"X-Session-Token": "fixture-session"}

        api = FastAPI()
        self.api = api
        api.add_api_route("/api/jobs/{job_uid}/orch-gates/{decision_id}/resolve", app.resolve_orch_gate, methods=["POST"])
        api.add_api_route("/api/messages/{msg_id}/resolve_decision", app.resolve_decision, methods=["POST"])
        for name, value in {"app": api, "session_token": None, "config": self.config,
                            "jobs": self.jobs, "store": self.store, "room_settings": {}}.items():
            stack.enter_context(patch.object(app, name, value))
        self.broadcast = stack.enter_context(patch.object(app, "_broadcast"))
        app._install_security_middleware("fixture-session", self.config)
        self.client = stack.enter_context(TestClient(api, client=("127.0.0.1", 50000)))
        self.job = self.jobs.create("Gate fixture", "job", "source-channel", "fixture", assignee="orch")
        self.transition("attach")
        self.transition("submit_brief")

    def transition(self, event):
        version = self.job["lifecycle"]["version"]
        self.job = self.jobs.transition_job(self.job["id"], event, actor="orch",
                                            actor_kind="orch", expected_uid=self.job["uid"],
                                            expected_version=version, operation_id=f"fixture:{version}")
        self.assertIsNotNone(self.job)

    def publish(self, revision=1, *, gate="brief", operation_id=None, coordinator="orch"):
        return _publish_orch_gate(
            self.jobs, self.store, self.job["uid"], coordinator=coordinator, gate=gate,
            revision=revision, choices=["Start", "Wait"] if gate == "staffing" else ["Approve", "Revise"],
            text="Review the current plan", operation_id=operation_id or f"{gate}:{revision}",
        )

    def resolve(self, card, *, headers=None, client=None, **overrides):
        values = {"job_uid": card["job_uid"], "decision_id": card["decision_id"],
                  "gate": card["gate"], "revision": card["revision"], "choice": card["choices"][0]}
        values.update(overrides)
        uid = values.pop("job_uid")
        decision_id = values.pop("decision_id")
        if client is None:
            client = self.client
        return client.post(f"/api/jobs/{uid}/orch-gates/{decision_id}/resolve",
                           headers=self.browser if headers is None else headers, json=values)

    def test_only_direct_local_browser_session_can_resolve_a_bound_card(self):
        card = self.publish()
        for headers in ({}, {"X-Session-Token": "wrong-session"},
                        {**self.browser, "Authorization": "Bearer fixture-agent"},
                        {**self.browser, "X-Agent-Token": "fixture-agent"},
                        {**self.browser, "X-Forwarded-For": "127.0.0.1"},
                        {**self.browser, "Forwarded": "for=127.0.0.1"}):
            with self.subTest(headers=list(headers)):
                self.assertEqual(self.resolve(card, headers=headers, actor="fixture-operator").status_code, 403)
        with TestClient(self.api, client=("192.0.2.10", 50000)) as lan:
            self.assertEqual(self.resolve(card, client=lan).status_code, 403)
            with patch.object(app, "is_local_client", return_value=True):
                self.assertEqual(self.resolve(card, client=lan).status_code, 403)
        generic = self.client.post(f"/api/messages/{card['decision_id']}/resolve_decision",
                                   headers=self.browser, json={"choice": "Approve"})
        self.assertEqual(generic.status_code, 403)
        self.assertIsNone(self.jobs.read_orch_gate(card["job_uid"], coordinator="orch",
                                                  decision_id=card["decision_id"])["choice"])
        self.broadcast.assert_not_called()
        response = self.resolve(card, actor="untrusted-label")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["actor"], "fixture-operator")
        self.assertEqual(response.json()["actor_source"], "local-browser")
        self.assertEqual(response.json()["choice"], "Approve")
        self.assertTrue(self.store.get_by_id(card["decision_id"])["metadata"]["resolved"])
        self.broadcast.assert_awaited_once()

    def test_stale_revision_wrong_binding_and_conflicting_replay_are_rejected(self):
        old = self.publish()
        self.transition("revise_brief")
        self.transition("submit_brief")
        self.assertEqual(self.resolve(old).status_code, 409)
        current = self.publish(2)
        self.assertIsNone(self.publish(1))
        self.assertIsNone(self.publish(3, coordinator="other"))
        first_job = self.job
        self.job = self.jobs.create("Second fixture", "job", "other-channel", "fixture", assignee="orch")
        self.transition("attach")
        self.transition("submit_brief")
        other_card = self.publish(2)
        self.job = first_job
        for fields in ({"revision": 1}, {"gate": "spec"}, {"job_uid": other_card["job_uid"]},
                       {"decision_id": old["decision_id"]}, {"choice": "Unknown"}):
            self.assertEqual(self.resolve(current, **fields).status_code, 409)
        result = self.resolve(current).json()
        self.assertEqual(result["choice"], "Approve")
        self.assertEqual(self.resolve(current).json(), result)
        self.assertEqual(self.resolve(current, choice="Revise").status_code, 409)
        reopened = JobStore(str(self.directory / "jobs.json"))
        self.assertEqual(reopened.read_orch_gate(current["job_uid"], coordinator="orch",
                                                decision_id=current["decision_id"]), result)
        self.assertIsNone(reopened.read_orch_gate(old["job_uid"], coordinator="orch",
                                                decision_id=old["decision_id"]))
        self.broadcast.assert_awaited_once()

    def test_unconfigured_operator_uses_room_username_after_authentication(self):
        card = self.publish()
        with patch.dict(self.config, operator={}), patch.dict(app.room_settings, username="room-owner"):
            self.assertEqual(self.resolve(card, headers={}).status_code, 403)
            response = self.resolve(card, actor="request-supplied-name")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["actor"], "room-owner")
        self.assertEqual(response.json()["actor_source"], "local-browser")
        self.broadcast.assert_awaited_once()

    def test_ipv6_loopback_and_mapped_loopback_allow_retries(self):
        card = self.publish()
        with TestClient(self.api, client=("::1", 50000)) as local_ipv6:
            first = self.resolve(card, client=local_ipv6)
        self.assertEqual(first.status_code, 200)
        with TestClient(self.api, client=("::ffff:127.0.0.1", 50000)) as mapped_loopback:
            second = self.resolve(card, client=mapped_loopback)
        self.assertEqual(second.json(), first.json())
        self.broadcast.assert_awaited_once()

    def test_concurrent_clicks_commit_once_and_wait_requires_a_new_staffing_card(self):
        brief = self.publish()
        self.assertEqual(self.resolve(brief).status_code, 200)
        self.transition("approve_brief")
        self.transition("plan_ready")
        card = self.publish(gate="staffing")
        self.assertEqual(self.publish(gate="staffing"), card)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.resolve(card, choice="Wait").json(), range(2)))
        self.assertEqual(results[0]["choice"], "Wait")
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.broadcast.await_count, 2)
        self.assertEqual(self.resolve(card, choice="Start").status_code, 409)
        replacement = self.publish(gate="staffing", operation_id="staffing:follow-up")
        self.assertNotEqual(card["decision_id"], replacement["decision_id"])
        self.assertEqual(self.resolve(card, choice="Wait").status_code, 409)
        self.assertEqual(self.resolve(replacement, choice="Start").json()["choice"], "Start")

    def test_publication_and_resolution_recover_from_save_failures(self):
        with patch.object(self.jobs, "_save", side_effect=OSError("fixture write failure")):
            with self.assertRaises(OSError):
                self.publish()
        card = self.publish()
        self.assertEqual(len(self.store.get_recent(channel="source-channel")), 1)
        with (patch.object(self.store, "_rewrite_jsonl", side_effect=OSError("fixture card failure")),
              self.assertLogs("app", level="ERROR")):
            result = self.resolve(card)
        self.assertEqual(result.status_code, 200)
        self.assertFalse(self.store.get_by_id(card["decision_id"])["metadata"]["resolved"])
        self.assertEqual(self.resolve(card).json(), result.json())
        self.assertTrue(self.store.get_by_id(card["decision_id"])["metadata"]["resolved"])
        self.broadcast.assert_awaited_once()
