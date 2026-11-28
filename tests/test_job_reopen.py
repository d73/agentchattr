import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import app
import mcp_bridge
from config_loader import load_config
from job_reopen import ReopenConflict, REOPEN_WAIT_SECONDS, create_request, record_decision
from jobs import JobStore
from operator_auth import PROOF_FILE, PROOF_KEY, prepare_operator_verifier
from store import MessageStore


class JobReopenPolicyTests(unittest.TestCase):
    def setUp(self):
        self.request = create_request(request_id="request-1", job_uid="job-stable-id",
                                      job_version=7, requester="requester", reason="Correct the result", now=100)
        self.deadline = 1900

    def decide(self, role, decision, now, *, actor=None, request=None):
        return record_decision(self.request if request is None else request,
                               actor=actor or f"principal-{role}", role=role, decision=decision, now=now)

    def test_owner_can_approve_before_deadline(self):
        approved = self.decide("owner", "approve", 101)
        self.assertEqual(approved["approval_basis"], "owner")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(self.request["decisions"], [])
        self.assertIs(self.decide("owner", "approve", 102, request=approved), approved)

    def test_two_distinct_delegates_at_or_after_persisted_deadline(self):
        restored = json.loads(json.dumps(self.request))
        with self.assertRaisesRegex(ValueError, "deadline"):
            self.decide("claude", "approve", self.deadline - 1, request=restored)
        one_vote = self.decide("claude", "approve", self.deadline, actor="first", request=restored)
        self.assertEqual(one_vote["status"], "pending")
        with self.assertRaisesRegex(ValueError, "distinct"):
            self.decide("agy", "approve", self.deadline + 1, actor="first", request=one_vote)
        approved = self.decide("agy", "approve", self.deadline + 1, actor="second", request=one_vote)
        self.assertEqual(approved["approval_basis"], "delegates")
        self.assertEqual([vote["actor"] for vote in approved["decisions"]], ["first", "second"])

    def test_owner_clarification_prevents_delegation_but_allows_later_decision(self):
        clarified = self.decide("owner", "clarify", 101)
        self.assertEqual(clarified["status"], "pending")
        with self.assertRaisesRegex(ValueError, "owner"):
            self.decide("claude", "approve", self.deadline, request=clarified)
        denied = self.decide("owner", "decline", self.deadline + 1, request=clarified)
        self.assertEqual(denied["status"], "declined")
        with self.assertRaisesRegex(ValueError, "resolved"):
            self.decide("owner", "approve", self.deadline + 2, request=denied)

    def test_delegate_decline_leaves_the_decision_with_owner(self):
        first = self.decide("agy", "approve", self.deadline)
        declined = self.decide("agy", "decline", self.deadline + 1, request=first)
        with self.assertRaisesRegex(ValueError, "owner"):
            self.decide("claude", "approve", self.deadline + 2, request=declined)
        self.assertEqual(self.decide("owner", "approve", self.deadline + 3, request=declined)["status"],
                         "approved")

    def test_unconfigured_role_cannot_vote(self):
        with self.assertRaisesRegex(ValueError, "identity"):
            self.decide("agent", "approve", self.deadline)


class JobReopenIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "jobs.json"
        self.jobs = JobStore(self.path)
        job = self.jobs.create("Completed work", "job", "general", "operator")
        started = self.emit(job, "start")
        self.closed = self.emit(started, "complete")
        self.scope = {"expected_uid": self.closed["uid"],
                      "expected_version": self.closed["lifecycle"]["version"]}

    def emit(self, job, event):
        return self.jobs.transition_job(job["id"], event, actor="verified-operator", actor_kind="human",
                                        expected_uid=job["uid"], expected_version=job["lifecycle"]["version"],
                                        operation_id=event)

    def request(self):
        return self.jobs.request_reopen(self.closed["id"], requester="local-browser",
                                        reason="Fix the existing output", **self.scope)["request"]

    def decide(self, request, *, role="owner", decision="approve"):
        return self.jobs.decide_reopen(self.closed["id"], request["id"], **self.scope,
                                       actor=f"verified-{role}", role=role, decision=decision)

    def test_approval_survives_restart_and_commits_exactly_once(self):
        with patch("jobs.time.time", return_value=1000):
            request = self.request()
        self.jobs = JobStore(self.path)
        with patch("jobs.time.time", return_value=1000 + REOPEN_WAIT_SECONDS):
            self.assertEqual(self.decide(request, role="claude")["request"]["status"], "pending")
        self.jobs = JobStore(self.path)
        with patch("jobs.time.time", return_value=1001 + REOPEN_WAIT_SECONDS):
            reopened = self.decide(request, role="agy")
        after_restart = JobStore(self.path).get(self.closed["id"])
        self.assertEqual(after_restart["lifecycle"]["core_id"], "core.active")
        event = after_restart["lifecycle_events"][-1]
        self.assertEqual(event["reopen_approval"]["approval_basis"], "delegates")
        self.assertEqual([vote["role"] for vote in event["reopen_approval"]["decisions"]], ["claude", "agy"])
        self.assertEqual(self.decide(request, role="agy"), reopened)
        self.assertEqual(len(self.jobs.get(self.closed["id"])["lifecycle_events"]), 3)

    def test_failed_commit_keeps_the_job_closed_and_allows_retry(self):
        request = self.request()
        with patch.object(self.jobs, "_save", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                self.decide(request)
        self.assertEqual(self.jobs.get(self.closed["id"])["reopen_requests"], [request])
        self.assertEqual(JobStore(self.path).get(self.closed["id"])["lifecycle"]["core_id"], "core.closed")
        self.assertEqual(self.decide(request)["job"]["lifecycle"]["core_id"], "core.active")

    def test_stale_request_and_cancelled_job_cannot_be_reopened(self):
        first = self.request()
        self.decide(first, decision="decline")
        second = self.request()
        self.assertNotEqual(first["id"], second["id"])
        with self.assertRaises(ReopenConflict):
            self.decide(first)
        another = self.jobs.create("Cancelled work", "job", "general", "operator")
        cancelled = self.jobs.update_status(another["id"], "archived")
        with self.assertRaises(ReopenConflict):
            self.jobs.request_reopen(cancelled["id"], expected_uid=cancelled["uid"],
                                     expected_version=cancelled["lifecycle"]["version"],
                                     requester="local-browser", reason="Try again")

    def test_orch_reopen_keeps_dispatch_paused_until_correction_is_recorded(self):
        job = self.jobs.create("Orch work", "job", "general", "operator")
        for event in ("attach", "submit_brief", "approve_brief", "plan_ready", "start", "pr_ready", "complete"):
            job = self.jobs.transition_job(
                job["id"], event, actor="verified-coordinator", actor_kind="orch",
                expected_uid=job["uid"], expected_version=job["lifecycle"]["version"],
                operation_id=f"orch-{event}",
            )
        self.closed = job
        self.scope = {"expected_uid": job["uid"], "expected_version": job["lifecycle"]["version"]}
        result = self.decide(self.request())
        self.assertEqual(result["job"]["lifecycle"]["stage_id"], "orch.coding")
        self.assertEqual(result["job"]["lifecycle"]["flags"], ["paused"])
        self.assertEqual(result["job"]["lifecycle_events"][-1]["operation_id"], result["request"]["id"])

    def test_http_bound_card_requires_independent_operator_proof(self):
        config = {"server": {"data_dir": str(self.root)}, "operator": {"identity": "operator"}}
        proof = "ab" * 32
        prepare_operator_verifier(self.root)
        digest = hashlib.sha256(proof.encode("ascii")).hexdigest()
        (self.root / PROOF_FILE).write_text(f"{PROOF_KEY}={digest}\n", encoding="utf-8")
        prepare_operator_verifier(self.root)
        headers = {"x-session-token": "browser-session"}
        body = {"job_uid": self.closed["uid"], "expected_version": self.scope["expected_version"],
                "reason": "Fix the existing output"}
        messages = MessageStore(self.root / "messages.jsonl")
        with patch.multiple(app, jobs=self.jobs, store=messages, config=config, session_token="browser-session"):
            request = Mock(headers=headers, json=AsyncMock(return_value=body))
            created = asyncio.run(app.create_job_reopen_request(self.closed["id"], request))
            self.assertEqual(asyncio.run(app.create_job_reopen_request(self.closed["id"], request)), created)
            msg = messages.get_by_id(created["card_msg_id"])
            self.assertEqual(msg["metadata"]["job_reopen"]["request_id"], created["request"]["id"])
            self.assertEqual(json.loads(mcp_bridge._serialize_messages([msg]))[0]["job_reopen"],
                             msg["metadata"]["job_reopen"])
            generic_click = Mock(json=AsyncMock(return_value={"choice": "Approve"}))
            self.assertEqual(asyncio.run(app.resolve_decision(msg["id"], generic_click)).status_code, 409)
            request.json.return_value = {**body, "decision": "approve"}
            rid = created["request"]["id"]
            self.assertEqual(asyncio.run(app.decide_job_reopen_request(self.closed["id"], rid, request)).status_code, 403)
            headers["x-orch-operator-proof"] = proof
            headers["authorization"] = "Bearer agent-token"
            self.assertEqual(asyncio.run(app.decide_job_reopen_request(self.closed["id"], rid, request)).status_code, 403)
            headers.pop("authorization")
            result = asyncio.run(app.decide_job_reopen_request(self.closed["id"], rid, request))
            self.assertEqual(result["request"]["approval_basis"], "owner")
            self.assertEqual(result["job"]["lifecycle_events"][-1]["actor"], "operator")

    def test_mcp_votes_require_exact_active_registration(self):
        request = self.request()
        config = {"job_approvals": {"claude_identity_id": "allowed-one", "agy_identity_id": "allowed-two"}}
        args = (self.closed["id"], self.closed["uid"], self.scope["expected_version"], request["id"], "approve")
        instance = {"name": "claude", "identity_id": "replacement", "state": "active"}
        with patch.multiple(mcp_bridge, jobs=self.jobs, config=config), \
                patch.object(mcp_bridge, "_authenticated_instance", return_value=instance), \
                patch("jobs.time.time", return_value=request["delegate_after"]):
            self.assertIn("verified", mcp_bridge.chat_decide_job_reopen(*args))
            instance.update(name="renamed", identity_id="allowed-one")
            self.assertEqual(json.loads(mcp_bridge.chat_decide_job_reopen(*args))["request"]["status"], "pending")
            instance.update(name="unrelated-label", identity_id="allowed-two")
            self.assertEqual(json.loads(mcp_bridge.chat_decide_job_reopen(*args))["request"]["status"], "approved")

    def test_approval_configuration_is_local_only(self):
        (self.root / "config.toml").write_text('[operator]\nidentity = "tracked"\n', encoding="utf-8")
        self.assertNotIn("operator", load_config(self.root))
        self.assertNotIn("job_approvals", load_config(self.root))
        (self.root / "config.local.toml").write_text(
            '[operator]\nidentity = "owner"\n[job_approvals]\nclaude_identity_id = "delegate"\n', encoding="utf-8")
        self.assertEqual(load_config(self.root)["operator"], {"identity": "owner"})
        self.assertEqual(load_config(self.root)["job_approvals"], {"claude_identity_id": "delegate"})
