"""Bind Orch's main-channel workflow to an authoritative Job and message."""

import re
import uuid

from jobs import JobStore, _ORCH_GATE_STAGE


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
        if binding := metadata.get(field):
            return {"job_uid": binding["job_uid"], "kind": kind,
                    "revision": binding.get("revision"), "prompt_id": message["id"]}
    return None


def _job_message(store, job, message_id):
    message = store.get_by_id(message_id) if type(message_id) is int else None
    if message and message.get("channel") == job["channel"]:
        return message
    return None


def _belongs_to_job(job, message):
    if message["id"] == job.get("source_msg_id"):
        return True
    binding = _message_binding(message)
    return binding is not None and binding["job_uid"] == job["uid"]


def publish_orch_workflow(jobs, store, job_uid, *, coordinator, operation_id, kind,
                          revision, parent_message_id, text, recipient=None, requires_reply=False):
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
    uid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"agentchattr:orch-workflow:{job_uid}:{operation_id}"))
    message = store.get_by_uid(uid)
    if message is None:
        # The first publication is validated against live anchors. An operation
        # already recorded in Job history is replayable after its source is deleted.
        message = next((entry for entry in retained.values() if entry["uid"] == uid), None)
    if message is None:
        parent = _job_message(store, job, parent_id)
        if parent_id is not None and (parent is None or not _belongs_to_job(job, parent)):
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
            if (parent["channel"] != channel or state is None or state[0]["channel"] != channel
                    or _job_message(store, state[0], binding["prompt_id"]) is None):
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
        entries.append(_history_entry(
            job, binding["kind"] if is_prompt else "reply", message["text"], time=message["timestamp"],
            message_id=message["id"], revision=binding["revision"], actor=message.get("sender"),
        ))
    for gate in gates:
        card_id = gate["decision_id"]
        card = _job_message(store, job, card_id)
        entries.append(_history_entry(
            job, gate["gate"], gate["text"], time=gate["decided_at"] or (card or {}).get("timestamp"),
            message_id=card_id, revision=gate["revision"], choice=gate["choice"], actor=gate["actor"],
        ))
    for request in job.get("reopen_requests", []):
        card = store.get_by_uid(f"job-reopen-{request['id']}")
        card_id = card["id"] if card and card["channel"] == job["channel"] else None
        decision = request["decisions"][-1] if request["decisions"] else {}
        entries.append(_history_entry(job, "reopen", request["reason"], message_id=card_id,
                                      event_id=f"reopen:{request['id']}",
                                      choice=decision.get("decision"), actor=decision.get("actor"),
                                      time=request["requested_at"]))
    for event in job.get("lifecycle_events", []):
        entries.append(_history_entry(job, "lifecycle", event["event"], actor=event["actor"], time=event["time"],
                                      event_id=f"lifecycle:{event['operation_id']}"))
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
    if message is None or not _belongs_to_job(job, message):
        return None
    return {"channel": job["channel"], "message": message, "messages": store.get_message_context(message)}
