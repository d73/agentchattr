"""Decision rules for reopening a completed Job within its approved scope."""

from copy import deepcopy


REOPEN_WAIT_SECONDS = 30 * 60
DELEGATE_ROLES = {"claude", "agy"}


class ReopenConflict(ValueError):
    """A request or decision no longer matches the current Job."""



def delegate_role(config, instance):
    """Authorize the exact active registrations configured as distinct delegates."""
    configured = config.get("job_approvals", {})
    claude = configured.get("claude_identity_id")
    agy = configured.get("agy_identity_id")
    if (not isinstance(claude, str) or not claude or not isinstance(agy, str) or not agy
            or claude == agy or not instance or instance.get("state") != "active"):
        return None
    return {claude: "claude", agy: "agy"}.get(instance["identity_id"])


def create_request(*, request_id: str, job_uid: str, job_version: int,
                   requester: str, reason: str, now: float) -> dict:
    """Create a request that the Job store will persist before displaying it."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("A reopen request requires a reason")
    return {
        "id": request_id,
        "job_uid": job_uid,
        "job_version": job_version,
        "requester": requester,
        "reason": reason.strip(),
        "requested_at": now,
        "delegate_after": now + REOPEN_WAIT_SECONDS,
        "status": "pending",
        "decisions": [],
        "approval_basis": None,
    }


def record_decision(reopen: dict, *, actor: str, role: str, decision: str, now: float,
                    source: str | None = None) -> dict:
    """Return the new request state for an already authorized caller.

    The caller checks the Job UID and version and persists this record under
    the same Job lock as the lifecycle transition.
    """
    if role not in DELEGATE_ROLES and role != "owner":
        raise ValueError("This identity cannot decide reopen requests")
    choices = {"approve", "decline", "clarify"} if role == "owner" else {"approve", "decline"}
    if decision not in choices:
        raise ValueError("Invalid reopen decision")

    decisions = reopen["decisions"]
    last_vote = next((entry for entry in reversed(decisions) if entry["role"] == role), None)
    if last_vote and (last_vote["actor"], last_vote["decision"]) == (actor, decision):
        return reopen
    if reopen["status"] != "pending":
        raise ValueError("This reopen request is already resolved")

    if role in DELEGATE_ROLES:
        if now < reopen["delegate_after"]:
            raise ValueError("Delegated approval is unavailable before the deadline")
        if any(entry["role"] == "owner" or entry["decision"] == "decline" for entry in decisions):
            raise ValueError("The configured owner must decide this reopen request")
        if any(entry["actor"] == actor and entry["role"] != role for entry in decisions):
            raise ValueError("The two delegates must use distinct verified identities")
        if last_vote and last_vote["actor"] != actor:
            raise ValueError("This delegate role already recorded a decision")

    updated = deepcopy(reopen)
    vote = {"actor": actor, "role": role, "decision": decision, "at": now}
    if source:
        vote["source"] = source
    updated["decisions"].append(vote)
    if role == "owner":
        if decision == "approve":
            updated.update(status="approved", approval_basis="owner")
        elif decision == "decline":
            updated["status"] = "declined"
    elif decision == "approve":
        approving_roles = {entry["role"] for entry in updated["decisions"] if entry["decision"] == "approve"}
        if DELEGATE_ROLES <= approving_roles:
            updated.update(status="approved", approval_basis="delegates")
    return updated
