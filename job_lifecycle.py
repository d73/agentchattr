"""Canonical Job lifecycle states and legal state changes."""

TODO = "core.todo"
ACTIVE = "core.active"
CLOSED = "core.closed"
LEGACY_STATUS = {TODO: "open", ACTIVE: "done", CLOSED: "archived"}
CORE_STATUS = {alias: state for state, alias in LEGACY_STATUS.items()} | {
    state: state for state in LEGACY_STATUS
}

DRAFTING = "orch.drafting"
AWAITING_APPROVAL = "orch.awaiting_approval"
PLANNING = "orch.planning"
READY = "orch.ready"
CODING = "orch.coding"
IN_REVIEW = "orch.in_review"
DONE = "orch.done"
CANCELLED = "orch.cancelled"

STAGE_PARENT = {
    DRAFTING: TODO, AWAITING_APPROVAL: TODO, PLANNING: TODO, READY: TODO,
    CODING: ACTIVE, IN_REVIEW: ACTIVE, DONE: CLOSED, CANCELLED: CLOSED,
}
FLAGS = {"paused", "attention", "error"}
ORCH_STAGE_CHANGES = {
    "submit_brief": ({DRAFTING}, AWAITING_APPROVAL),
    "revise_brief": ({AWAITING_APPROVAL}, DRAFTING),
    "approve_brief": ({AWAITING_APPROVAL}, PLANNING),
    "plan_ready": ({PLANNING}, READY),
    "revise_plan": ({READY}, PLANNING),
    "change_scope": ({PLANNING, READY}, DRAFTING),
    "start": ({READY}, CODING),
    "pr_ready": ({CODING}, IN_REVIEW),
    "changes_requested": ({IN_REVIEW}, CODING),
    "return_to_backlog": ({CODING, IN_REVIEW}, PLANNING),
    "complete": ({IN_REVIEW}, DONE),
}
MANUAL_CHANGES = {
    "start": (TODO, ACTIVE),
    "return_to_backlog": (ACTIVE, TODO),
    "complete": (ACTIVE, CLOSED),
}


def core_status(value):
    return CORE_STATUS.get(value) if isinstance(value, str) else None


def new_lifecycle(core=TODO, *, stage=None, managed_by="manual"):
    return {"core_id": core, "stage_id": stage, "managed_by": managed_by,
            "flags": [], "close_outcome": None, "version": 0}


def validate_lifecycle(state):
    if not isinstance(state, dict):
        raise ValueError("invalid Job lifecycle")
    core, stage = state.get("core_id"), state.get("stage_id")
    if not isinstance(core, str) or core not in LEGACY_STATUS:
        raise ValueError("unknown Job core state")
    if stage is not None and (not isinstance(stage, str) or STAGE_PARENT.get(stage) != core):
        raise ValueError("Job stage does not belong to its core state")
    manager, outcome = state.get("managed_by"), state.get("close_outcome")
    if manager not in ("manual", "orch") or (manager == "orch" and stage is None):
        raise ValueError("invalid Job lifecycle manager")
    if outcome not in (None, "completed", "cancelled") or (outcome is not None and core != CLOSED):
        raise ValueError("invalid Job close outcome")
    if (stage == DONE and outcome != "completed") or (stage == CANCELLED and outcome != "cancelled"):
        raise ValueError("Job stage does not match the close outcome")
    flags, version = state.get("flags"), state.get("version")
    if (not isinstance(flags, list) or not all(isinstance(flag, str) and flag in FLAGS for flag in flags)
            or len(set(flags)) != len(flags) or type(version) is not int or version < 0):
        raise ValueError("invalid Job lifecycle flags or version")


def next_lifecycle(current, event, actor_kind, *, reason=None, flags=None, approved_reopen=False):
    """Apply one event from an already authenticated server or plugin caller."""
    core, stage = current["core_id"], current["stage_id"]
    managed = current["managed_by"] == "orch"
    updated = dict(current)
    if event == "attach":
        if actor_kind != "orch" or managed or core != TODO:
            return None
        updated.update(managed_by="orch", stage_id=DRAFTING)
    elif event == "reopen":
        if (actor_kind not in ("human", "orch") or core != CLOSED
                or current["close_outcome"] != "completed" or approved_reopen is not True or not reason):
            return None
        updated.update(core_id=ACTIVE, stage_id=CODING if managed else None, close_outcome=None, flags=[])
    else:
        if actor_kind != ("orch" if managed else "human") or core == CLOSED:
            return None
        if event == "set_flags":
            updated["flags"] = flags
        elif event == "cancel":
            updated.update(core_id=CLOSED, stage_id=CANCELLED if managed else None,
                           close_outcome="cancelled", flags=[])
        elif managed:
            change = ORCH_STAGE_CHANGES.get(event)
            if not change or stage not in change[0]:
                return None
            new_stage = change[1]
            updated.update(core_id=STAGE_PARENT[new_stage], stage_id=new_stage)
        else:
            change = MANUAL_CHANGES.get(event)
            if not change or core != change[0]:
                return None
            updated.update(core_id=change[1], stage_id=None)
        if event == "complete":
            updated.update(close_outcome="completed", flags=[])

    updated["version"] += 1
    validate_lifecycle(updated)
    return updated
