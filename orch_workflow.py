"""Bind Orch's main-channel workflow to an authoritative Job and message."""

import re
import uuid

from jobs import JobStore, _ORCH_GATE_STAGE


_HISTORY_REASONS = {
    "assignment": set(), "pr_ready": set(), "changes_requested": set(), "merge_verified": set(),
    "result_rejected": {"approval_only_for_spec", "invalid_result"},
    "deadline_missed": {"ack_deadline", "evidence_deadline"},
    "delivery_unconfirmed": set(), "result_accepted_late": set(), "ack_accepted_late": set(),
    "operator_retry": set(), "cleanup_required": set(), "closeout": set(), "canceled": set(),
    "result_accepted": set(),
}
_EVENT_ID = re.compile(r"(?:orch:[0-9a-f]{32}|message:[0-9]{1,16}|(?:lifecycle|reopen):[a-zA-Z0-9_-]{1,128})")
_JOB_UID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def publish_orch_history_event(jobs, job_uid, *, coordinator, payload):
    required = {"v", "kind", "revision", "attempt", "operation_id", "time"}
    optional = {"task", "actor", "reason_code", "repo", "pr", "commit"}
    if not isinstance(payload, dict) or not required <= payload.keys() <= required | optional:
        raise ValueError("Invalid Orch history fields")
    v, kind, revision, attempt, operation, stamp = (payload[key] for key in
        ("v", "kind", "revision", "attempt", "operation_id", "time"))
    if (type(v) is not int or v != 1 or not isinstance(kind, str) or kind not in _HISTORY_REASONS
            or revision is not None and (type(revision) is not int or not 0 < revision < 2**53)
            or attempt is not None and (not isinstance(attempt, str) or not re.fullmatch(r"[0-9a-f]{32}", attempt))
            or not isinstance(operation, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", operation)
            or type(stamp) not in (int, float) or not 0 < stamp < 253402300800):
        raise ValueError("Invalid Orch history event")
    patterns = {"task": r"[0-9a-f]{32}", "actor": r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}",
                "repo": r"[a-zA-Z0-9_.-]{1,100}/[a-zA-Z0-9_.-]{1,100}", "commit": r"[0-9a-f]{40,64}"}
    for field, pattern in patterns.items():
        if field in payload and (not isinstance(payload[field], str) or not re.fullmatch(pattern, payload[field])):
            raise ValueError("Invalid Orch history identifier")
    if ("pr" in payload and (type(payload["pr"]) is not int or not 0 < payload["pr"] < 2**53)
            or "reason_code" in payload and (not isinstance(payload["reason_code"], str)
                                             or payload["reason_code"] not in _HISTORY_REASONS[kind])
            or "commit" in payload and kind != "merge_verified"):
        raise ValueError("Invalid Orch history outcome")
    event_id = "orch:" + uuid.uuid5(uuid.NAMESPACE_URL,
        f"agentchattr:orch-history:{job_uid}:{revision}:{attempt}:{kind}:{operation}").hex
    return jobs.record_orch_history_event(job_uid, coordinator=coordinator, event={**payload, "event_id": event_id})


def _workflow_binding(message):
    return (message.get("metadata") or {}).get("orch_workflow")


def _owned_by(job, coordinator):
    bound = job.get("orch_coordinator")
    return bool(bound and isinstance(coordinator, str) and bound.casefold() == coordinator.casefold())


def _message_binding(message):
    metadata = message.get("metadata") or {}
    if metadata.get("orch_workflow"):
        return metadata["orch_workflow"]
    for field, kind in (("orch_gate", "gate"), ("job_reopen", "reopen")):
        binding = metadata.get(field)
        if isinstance(binding, dict) and isinstance(binding.get("job_uid"), str):
            return {"job_uid": binding["job_uid"], "kind": kind,
                    "revision": binding.get("revision"), "prompt_id": message["id"]}
    return None


def _job_message(store, job, message_id):
    message = store.get_by_id(message_id) if type(message_id) is int else None
    if message and message.get("channel") == job["channel"]:
        return message
    return None


def _belongs_to_job(state, message):
    job, gates, _, _ = state
    if message["id"] == job.get("source_msg_id"):
        return True
    metadata = message.get("metadata") or {}
    workflow = metadata.get("orch_workflow")
    if workflow:
        return workflow.get("job_uid") == job["uid"]
    if gate := metadata.get("orch_gate"):
        return any(message["id"] == recorded["decision_id"] and gate == {
            key: recorded[key] for key in ("job_uid", "gate", "revision")
        } for recorded in gates)
    if reopen := metadata.get("job_reopen"):
        return any(message["uid"] == f"job-reopen-{request['id']}"
                   and reopen.get("job_uid") == job["uid"] and reopen.get("request_id") == request["id"]
                   for request in job.get("reopen_requests", []))
    return False


def publish_orch_workflow(jobs, store, job_uid, *, coordinator, operation_id, kind,
                          revision, parent_message_id, text, recipient=None, requires_reply=False, event_id=None):
    if (not isinstance(operation_id, str) or not operation_id.strip()
            or not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", kind)
            or revision is not None and (type(revision) is not int or revision < 1)
            or parent_message_id is not None and (type(parent_message_id) is not int or parent_message_id < 0)
            or not isinstance(text, str) or not text.strip()
            or type(requires_reply) is not bool
            or recipient is not None and (not isinstance(recipient, str)
                                         or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", recipient))):
        raise ValueError("Invalid Orch workflow prompt")
    state = jobs.orch_workflow_state(job_uid)
    if state is None or not _owned_by(state[0], coordinator):
        return None
    job, gates, retained, _ = state
    parent_id = job.get("source_msg_id") if parent_message_id is None else parent_message_id
    binding = {"job_uid": job_uid, "kind": kind, "revision": revision, "prompt_id": None}
    if recipient is not None:
        binding["recipient"] = recipient
    if requires_reply:
        binding["requires_reply"] = True
    if (isinstance(event_id, str) and _EVENT_ID.fullmatch(event_id) and _JOB_UID.fullmatch(job_uid)
            and (event_id in jobs.orch_history_events(job_uid)
                 or any(event_id == f"reopen:{request['id']}" for request in job.get("reopen_requests", [])))):
        binding["event_id"] = event_id
        # Keep the delivery marker at the end for legacy outbox reconciliation.
        head, marker, tail = text.rpartition("\n\n[orch:")
        link = f"\n\n[View event](#job/{job_uid}/event/{event_id})"
        text = head + link + marker + tail if marker else text + link
    uid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"agentchattr:orch-workflow:{job_uid}:{operation_id}"))
    message = store.get_by_uid(uid)
    if message is None:
        # The first publication is validated against live anchors. An operation
        # already recorded in Job history is replayable after its source is deleted.
        message = next((entry for entry in retained.values() if entry["uid"] == uid), None)
    if message is None:
        parent = _job_message(store, job, parent_id)
        if parent_id is not None and (parent is None or not _belongs_to_job(state, parent)):
            return None
        if kind == "revise" and not any(
            gate["decision_id"] == parent_id and gate["revision"] == revision and gate["choice"] == "Revise"
            for gate in gates
        ):
            return None
        message = store.add(
            coordinator, text, channel=job["channel"], reply_to=parent_id, sender_kind="agent",
            metadata={"orch_workflow": binding}, uid=uid, once=True,
        )
    expected = {**binding, "prompt_id": message["id"]}
    if (message["text"], message.get("reply_to"), _workflow_binding(message)) != (text, parent_id, expected):
        raise ValueError("Orch workflow operation already has a different binding")
    if message["channel"] == job["channel"]:
        jobs.remember_orch_workflow_message(job_uid, message)
    jobs.activate_orch_workflow(job_uid, coordinator=coordinator)
    return _prompt_result(job, message)


def _prompt_result(job, message):
    binding = _workflow_binding(message)
    return {**binding, "channel": job["channel"]}


def _prompt_open(state, prompt_id):
    job, gates, _, completed = state
    return (prompt_id not in completed
            and job["lifecycle"]["core_id"] != "core.closed"
            and all(gate["decision_id"] <= prompt_id for gate in gates))


def reply_metadata(jobs, store, channel, reply_to):
    """Resolve a reply from its closest persisted Job anchor in the same channel."""
    parent = store.get_by_id(reply_to) if type(reply_to) is int else None
    while parent is not None:
        binding = _message_binding(parent)
        if binding is not None:
            state = jobs.orch_workflow_state(binding["job_uid"])
            if state is None and (parent.get("metadata") or {}).get("job_reopen"):
                return None
            root = _job_message(store, state[0], binding["prompt_id"]) if state is not None else None
            if (parent["channel"] != channel or state is None or state[0]["channel"] != channel
                    or root is None or not _belongs_to_job(state, parent) or not _belongs_to_job(state, root)):
                raise ValueError("Orch workflow reply must use its Job's current channel and prompt")
            reply = {key: value for key, value in binding.items() if key != "requires_reply"}
            if _workflow_binding(parent) and not _prompt_open(state, binding["prompt_id"]):
                reply["closed"] = True
            return {"orch_workflow": reply}
        ancestor_id = parent.get("reply_to")
        if parent["channel"] != channel or type(ancestor_id) is not int or ancestor_id >= parent["id"]:
            break
        parent = store.get_by_id(ancestor_id)
    return None


def record_reply(jobs, message):
    if (binding := _workflow_binding(message)) and not message.get("persistence_error"):
        jobs.remember_orch_workflow_message(binding["job_uid"], message)


def _bound_messages(store, job, retained):
    current = store.get_orch_workflow_messages(job["channel"], job["uid"])
    return {**{message["id"]: message for message in current},
            **{int(key): value for key, value in retained.items()}}


def read_orch_workflow(jobs, store, job_uid, *, coordinator, prompt_id):
    state = jobs.orch_workflow_state(job_uid)
    if state is None or not _owned_by(state[0], coordinator):
        return None
    job, _, retained, _ = state
    messages = _bound_messages(store, job, retained)
    prompt = messages.get(prompt_id)
    if prompt is None or _workflow_binding(prompt)["prompt_id"] != prompt_id:
        return None
    fields = ("id", "uid", "reply_to", "text", "sender", "sender_kind", "sender_identity", "timestamp", "time")
    replies = [{key: message.get(key) for key in fields}
               for message_id, message in sorted(messages.items())
               if message_id != prompt_id and _workflow_binding(message)["prompt_id"] == prompt_id
               and not _workflow_binding(message).get("closed")]
    return {**_prompt_result(job, prompt), "completed": not _prompt_open(state, prompt_id), "replies": replies}


def complete_orch_workflow(jobs, store, job_uid, *, coordinator, prompt_id):
    state = jobs.orch_workflow_state(job_uid)
    if state is None or not _owned_by(state[0], coordinator) or type(prompt_id) is not int:
        return None
    job, _, retained, _ = state
    prompt = _job_message(store, job, prompt_id) or retained.get(str(prompt_id))
    binding = _workflow_binding(prompt) if prompt is not None else None
    if not binding or binding["job_uid"] != job_uid or binding["prompt_id"] != prompt_id:
        return None
    return jobs.complete_orch_workflow_prompt(job_uid, coordinator=coordinator, prompt_id=prompt_id)


def _history_entry(job, kind, text, *, event_id=None, time=None, message_id=None, revision=None, choice=None, actor=None):
    return {"event_id": event_id or f"message:{message_id}",
            "kind": kind, "revision": revision, "text": text, "message_id": message_id,
            "channel": job["channel"], "choice": choice, "actor": actor, "time": time}


def _lifecycle_entry(job, event, index):
    kind = event.get("event") or "lifecycle"
    kind = {"pr_ready": "begin_review", "changes_requested": "resume_coding"}.get(kind, kind)
    operation = event.get("operation_id")
    event_id = f"lifecycle:{operation}"
    if isinstance(operation, str) and not _EVENT_ID.fullmatch(event_id):
        event_id = "lifecycle:" + uuid.uuid5(uuid.NAMESPACE_URL, operation).hex
    elif not isinstance(operation, str):
        event_id = f"lifecycle:legacy-{index}"
    before, after = event.get("before") or {}, event.get("after") or {}
    row = _history_entry(job, kind, None, event_id=event_id, actor=event.get("actor"),
                         time=event.get("time"), revision=event.get("revision"))
    if kind == "set_flags":
        old_flags, flags = before.get("flags"), after.get("flags", event.get("flags"))
        if isinstance(flags, list):
            row["flags"] = flags
            if isinstance(old_flags, list):
                row["flags_added"] = [flag for flag in flags if flag not in old_flags]
                row["flags_removed"] = [flag for flag in old_flags if flag not in flags]
        row["detail"] = True
    return row


def _attempt_history(job, events):
    outcomes = {}
    problems = {"result_rejected", "deadline_missed", "delivery_unconfirmed"}
    recovery = {"result_accepted_late", "operator_retry", "merge_verified", "pr_ready",
                "changes_requested", "result_accepted"}
    for event in reversed(sorted(events, key=lambda row: row["time"])):
        kind, attempt = event["kind"], event["attempt"]
        key = (event["revision"], attempt)
        row = _history_entry(job, kind, None, event_id=event["event_id"],
                             time=event["time"], revision=event["revision"], actor=event.get("actor"))
        row.update({field: event[field] for field in ("reason_code", "repo", "pr", "commit", "task", "attempt")
                    if field in event})
        if kind in problems:
            row["problem"] = True
            resolution = outcomes.get(key, {}).get(kind)
            if resolution is not None:
                row["resolved_at"] = resolution
        elif attempt and (kind in recovery or kind == "ack_accepted_late"):
            affected = problems - {"result_rejected"} if kind == "ack_accepted_late" else problems
            outcomes.setdefault(key, {}).update(dict.fromkeys(affected, event["time"]))
        yield row


def _pending_actions(store, state, messages):
    job, gates, _, _ = state
    stage = job["lifecycle"]["stage_id"]
    actions = []
    answered = set()
    for message in messages.values():
        if message.get("sender_kind") != "human":
            continue
        parent_id = message.get("reply_to")
        while parent_id in messages and parent_id not in answered and parent_id < message["id"]:
            answered.add(parent_id)
            parent_id = messages[parent_id].get("reply_to")
    for message_id, message in messages.items():
        binding = _workflow_binding(message)
        if (binding.get("requires_reply") and not binding.get("closed")
                and message_id not in answered and _prompt_open(state, binding["prompt_id"])
                and _job_message(store, job, message_id)):
            actions.append((message_id, binding["kind"], "Reply requested"))
    latest_revision = gates[-1]["revision"] if gates else None
    latest_by_kind = {gate["gate"]: gate for gate in gates if gate["revision"] == latest_revision}
    for kind, gate in latest_by_kind.items():
        card_id = gate["decision_id"]
        if (gate["choice"] is None and _ORCH_GATE_STAGE[kind] == stage
                and gate["stage_version"] == JobStore._stage_version(job)
                and _job_message(store, job, card_id)):
            label = {"brief": "Brief approval", "spec": "Plan approval",
                     "mockup": "Mockup approval", "staffing": "Staffing decision"}[kind]
            actions.append((card_id, kind, label))
    for request in job.get("reopen_requests", []):
        if request["status"] == "pending" and request["job_version"] == job["lifecycle"]["version"]:
            card = store.get_by_uid(f"job-reopen-{request['id']}")
            if card and card["channel"] == job["channel"]:
                actions.append((card["id"], "reopen", "Reopen decision"))
    return sorted(actions)


def list_orch_workflow_actions(jobs, store, *, coordinator):
    result = []
    for job in jobs.list_all():
        if not _owned_by(job, coordinator):
            continue
        state = jobs.orch_workflow_state(job["uid"])
        if state is None:
            continue
        job, _, retained, _ = state
        for message_id, kind, label in _pending_actions(store, state, _bound_messages(store, job, retained)):
            result.append({"job_uid": job["uid"], "job_id": job["id"], "title": job["title"],
                           "channel": job["channel"], "message_id": message_id, "kind": kind, "label": label})
    return result


def history(jobs, store, job_uid):
    state = jobs.orch_workflow_state(job_uid)
    if state is None:
        return None
    job, gates, retained, _ = state
    entries = []
    messages = _bound_messages(store, job, retained)
    for message in messages.values():
        binding = _workflow_binding(message)
        is_prompt = binding["prompt_id"] == message["id"]
        actor = message.get("sender")
        if is_prompt and isinstance(actor, str) and _owned_by(job, actor):
            actor = None
        entries.append(_history_entry(
            job, binding["kind"] if is_prompt else "reply", None, time=message["timestamp"],
            message_id=message["id"], revision=binding["revision"], actor=actor,
        ))
    for gate in gates:
        card_id = gate["decision_id"]
        card = _job_message(store, job, card_id)
        entries.append(_history_entry(
            job, gate["gate"], None, time=gate["decided_at"] or (card or {}).get("timestamp"),
            message_id=card_id, revision=gate["revision"], choice=gate["choice"], actor=gate["actor"],
        ))
    for request in job.get("reopen_requests", []):
        card = store.get_by_uid(f"job-reopen-{request['id']}")
        card_id = card["id"] if card and card["channel"] == job["channel"] else None
        decision = request["decisions"][-1] if request["decisions"] else {}
        entries.append(_history_entry(job, "reopen", None, message_id=card_id,
                                      event_id=f"reopen:{request['id']}",
                                      choice=decision.get("decision"), actor=decision.get("actor"),
                                      time=request["requested_at"]))
    entries.extend(_lifecycle_entry(job, event, index) for index, event in enumerate(job.get("lifecycle_events", [])))
    entries.extend(reversed(list(_attempt_history(job, jobs.orch_history_events(job_uid).values()))))
    entries.sort(key=lambda row: (row["time"] or 0, row["message_id"] or 0))
    actions = _pending_actions(store, state, messages)
    return {"job_uid": job_uid, "channel": job["channel"], "entries": entries,
            "workflow_active": bool(job.get("orch_workflow_active")),
            "source_message_id": job.get("source_msg_id"), "current_action_message_id": actions[-1][0] if actions else None}


def history_context(jobs, store, job_uid, message_id):
    state = jobs.orch_workflow_state(job_uid)
    if state is None:
        return None
    job = state[0]
    message = _job_message(store, job, message_id)
    if message is None or not _belongs_to_job(state, message):
        return None
    return {"channel": job["channel"], "message": message, "messages": store.get_message_context(message)}


def history_event_context(jobs, store, job_uid, event_id):
    if not _JOB_UID.fullmatch(job_uid):
        return None
    result = history(jobs, store, job_uid)
    if result is None:
        return None
    entry = next((row for row in result["entries"] if row["event_id"] == event_id), None) if (
        _EVENT_ID.fullmatch(event_id)) else None
    return {"job_uid": job_uid, "event_id": entry["event_id"] if entry else None,
            "message_id": entry["message_id"] if entry else None}
