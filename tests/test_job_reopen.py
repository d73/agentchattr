import json
import unittest

from job_reopen import create_request, record_decision


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
