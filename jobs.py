"""Job store — bounded work conversations with threaded messages."""

import json
import logging
from atomic_io import atomic_write_text, write_backup_once
import time
import threading
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Callable

from job_lifecycle import ACTIVE, CLOSED, DRAFTING, LEGACY_STATUS, TODO, core_status
from job_lifecycle import new_lifecycle, next_lifecycle, validate_lifecycle
from job_lifecycle import AWAITING_APPROVAL, PLANNING, READY
from job_reopen import ReopenConflict, create_request, record_decision

log = logging.getLogger(__name__)

_ORCH_GATE_STAGE = {
    "brief": AWAITING_APPROVAL, "spec": PLANNING, "mockup": PLANNING, "staffing": READY,
}
_ORCH_GATE_FIELDS = ("job_uid", "decision_id", "gate", "revision", "choices", "choice", "actor", "decided_at")


def orch_coordinator(job):
    """Read the persisted coordinator, including bindings made before this field existed."""
    if job.get("orch_coordinator"):
        return job["orch_coordinator"]
    for event in reversed(job.get("lifecycle_events", [])):
        if event["event"] == "attach":
            return event["actor"]
    gates = job.get("_orch_gates") or []
    return gates[0]["coordinator"] if gates else None


class JobStore:
    def __init__(self, path: str, *, history_next_id: Callable[[], int] | None = None):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._id_path = self._path.with_name(self._path.name + ".next-id")
        self._jobs: list[dict] = []
        self._next_id = 1
        self._lock = threading.Lock()
        self._callbacks: list = []  # (action, job) on any change
        has_marker = self._id_path.exists()
        if has_marker:
            try:
                marker = int(self._id_path.read_text(encoding="utf-8"))
                if marker < 1:
                    raise ValueError("next ID must be positive")
                self._next_id = max(self._next_id, marker)
            except (OSError, ValueError) as exc:
                raise ValueError(f"Cannot read Job ID marker {self._id_path.resolve()}: {exc}") from exc
        self._load()
        if not has_marker and history_next_id is not None:
            self._next_id = max(self._next_id, history_next_id())
            atomic_write_text(self._id_path, str(self._next_id))

    def _load(self):
        if not self._path.exists():
            return
        try:
            original = self._path.read_bytes()
            raw = json.loads(original.decode("utf-8"))
            if not isinstance(raw, list):
                raise ValueError("expected a JSON list of jobs")
            migrated = False
            legacy_format = False
            for job in raw:
                if not isinstance(job, dict):
                    raise ValueError("expected a Job record")
                legacy = job.get("status")
                if "lifecycle" not in job:
                    core = core_status(job.get("status", "open"))
                    if core is None:
                        raise ValueError(f"unknown Job status: {legacy!r}")
                    job["lifecycle"] = new_lifecycle(core)
                    migrated = True
                    legacy_format = True
                validate_lifecycle(job["lifecycle"])
                canonical = job["lifecycle"]["core_id"]
                if "status" in job:
                    if core_status(legacy) != canonical:
                        raise ValueError("conflicting Job status and lifecycle state")
                    migrated = True
                    legacy_format = True
                if not isinstance(job.get("uid"), str) or not job["uid"]:
                    job["uid"] = str(uuid.uuid4())
                    migrated = True
                job["status"] = LEGACY_STATUS[canonical]
            self._jobs = raw
            self._next_id = max(self._next_id, max((job["id"] for job in raw), default=0) + 1)
            reordered = self._ensure_sort_orders_locked()
            if legacy_format:
                backup = self._path.with_name(self._path.name + ".pre-lifecycle")
                if write_backup_once(backup, original):
                    log.info("Preserved pre-lifecycle Job format at %s", backup)
            if migrated or reordered:
                self._save()
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Cannot load jobs from {self._path.resolve()}: {exc}. The file was not changed.") from exc

    def _save(self):
        try:
            records = [{key: value for key, value in job.items() if key != "status"} for job in self._jobs]
            atomic_write_text(self._path, json.dumps(records, indent=2, ensure_ascii=False) + "\n", "utf-8")
        except OSError as exc:
            raise OSError(exc.errno, f"Could not save jobs: {exc}", str(self._path.resolve())) from exc

    def _next_sort_order_locked(self, status: str) -> int:
        max_order = 0
        for a in self._jobs:
            if a.get("status") != status:
                continue
            try:
                max_order = max(max_order, int(a.get("sort_order", 0)))
            except (TypeError, ValueError):
                continue
        return max_order + 1

    def _ensure_sort_orders_locked(self):
        max_by_group: dict[str, int] = {}
        changed = False
        for a in self._jobs:
            key = a.get("status", "open")
            try:
                cur = int(a.get("sort_order", 0))
            except (TypeError, ValueError):
                cur = 0
            if cur > 0:
                max_by_group[key] = max(max_by_group.get(key, 0), cur)

        for a in self._jobs:
            key = a.get("status", "open")
            try:
                cur = int(a.get("sort_order", 0))
            except (TypeError, ValueError):
                cur = 0
            if cur <= 0:
                next_order = max_by_group.get(key, 0) + 1
                a["sort_order"] = next_order
                max_by_group[key] = next_order
                changed = True
        return changed

    def on_change(self, callback):
        """Register a callback(action, job) on any change.
        action: 'create', 'update', 'message', 'message_delete'."""
        self._callbacks.append(callback)

    def _fire(self, action: str, job: dict):
        for cb in self._callbacks:
            try:
                cb(action, job)
            except Exception:
                pass

    @staticmethod
    def _snapshot(job):
        public = dict(job)
        public.pop("_orch_gates", None)
        public.pop("_orch_workflow_history", None)
        public.pop("_orch_workflow_completed", None)
        coordinator = orch_coordinator(job)
        if coordinator is not None:
            public["orch_coordinator"] = coordinator
        public["lifecycle"] = deepcopy(job["lifecycle"])
        if "lifecycle_events" in job:
            public["lifecycle_events"] = deepcopy(job["lifecycle_events"])
        if "reopen_requests" in job:
            public["reopen_requests"] = deepcopy(job["reopen_requests"])
        if "imported_lifecycle_events" in job:
            public["imported_lifecycle_events"] = deepcopy(job["imported_lifecycle_events"])
        return public

    def list_all(self, channel: str | None = None,
                 status: str | None = None) -> list[dict]:
        """List jobs, optionally filtered by channel and/or status."""
        if status:
            core = core_status(status)
            if core is None:
                return []
            status = LEGACY_STATUS[core]
        with self._lock:
            changed = self._ensure_sort_orders_locked()
            if changed:
                self._save()
            return [
                self._snapshot(job) for job in self._jobs
                if (not channel or job.get("channel") == channel)
                and (not status or job["status"] == status)
            ]

    def get(self, job_id: int) -> dict | None:
        with self._lock:
            for a in self._jobs:
                if a["id"] == job_id:
                    return self._snapshot(a)
            return None

    def get_for_proposal(self, anchor_msg_id: int) -> dict | None:
        with self._lock:
            job = next((item for item in self._jobs if item.get("anchor_msg_id") == anchor_msg_id), None)
            return self._snapshot(job) if job is not None else None

    def get_for_source(self, source_msg_id: int) -> dict | None:
        with self._lock:
            job = next((item for item in self._jobs if item.get("source_msg_id") == source_msg_id), None)
            return self._snapshot(job) if job is not None else None

    def bind_existing_source(self, job_id: int, job_uid: str, source_msg_id: int) -> dict | None:
        """Preserve a verified source reference on a legacy Job."""
        with self._lock:
            for job in self._jobs:
                if job.get("source_msg_id") == source_msg_id:
                    return self._snapshot(job)
            job = next((item for item in self._jobs
                        if item["id"] == job_id and item["uid"] == job_uid), None)
            if job is None or job.get("source_msg_id") is not None:
                return None
            job["source_msg_id"] = source_msg_id
            try:
                self._save()
            except OSError:
                del job["source_msg_id"]
                raise
            result = self._snapshot(job)
        self._fire("update", result)
        return result

    def _managed_job_locked(self, uid):
        return next((job for job in self._jobs
                     if job.get("uid") == uid and job["lifecycle"]["managed_by"] == "orch"), None)

    def orch_workflow_state(self, job_uid):
        with self._lock:
            job = self._managed_job_locked(job_uid)
            if job is None:
                return None
            return (self._snapshot(job), deepcopy(job.get("_orch_gates", [])),
                    deepcopy(job.get("_orch_workflow_history", {})),
                    set(job.get("_orch_workflow_completed", [])))

    def complete_orch_workflow_prompt(self, job_uid, *, coordinator, prompt_id):
        with self._lock:
            job = self._managed_job_locked(job_uid)
            bound = orch_coordinator(job) if job is not None else None
            if not bound or bound.casefold() != coordinator.casefold():
                return None
            previous = job.get("_orch_workflow_completed")
            if prompt_id not in (previous or []):
                job["_orch_workflow_completed"] = [*(previous or []), prompt_id]
                try:
                    self._save()
                except OSError:
                    if previous is None:
                        del job["_orch_workflow_completed"]
                    else:
                        job["_orch_workflow_completed"] = previous
                    raise
            return {"job_uid": job_uid, "channel": job["channel"], "prompt_id": prompt_id, "completed": True}

    def activate_orch_workflow(self, job_uid, *, coordinator):
        if not isinstance(coordinator, str) or not coordinator.strip():
            return None
        with self._lock:
            job = self._managed_job_locked(job_uid)
            bound = orch_coordinator(job) if job is not None else None
            if job is None or (bound and bound.casefold() != coordinator.casefold()):
                return None
            changed = not job.get("orch_workflow_active")
            if changed:
                previous = dict(job)
                job["orch_workflow_active"] = True
                if not bound:
                    job["orch_coordinator"] = coordinator
                try:
                    self._save()
                except OSError:
                    job.clear()
                    job.update(previous)
                    raise
            snapshot = self._snapshot(job)
        if changed:
            self._fire("update", snapshot)
        return {"job_uid": job_uid, "channel": snapshot["channel"], "workflow_active": True}

    def remember_orch_workflow_message(self, job_uid, message):
        """Retain a history snapshot even if the original chat message is removed."""
        with self._lock:
            job = self._managed_job_locked(job_uid)
            previous = job.get("_orch_workflow_history") if job else None
            key = str(message["id"])
            if (job is None or job["channel"] != message["channel"]
                    or key in (previous or {})):
                return
            job["_orch_workflow_history"] = {**(previous or {}), key: deepcopy(message)}
            try:
                self._save()
            except OSError:
                if previous is None:
                    del job["_orch_workflow_history"]
                else:
                    job["_orch_workflow_history"] = previous
                raise

    @staticmethod
    def _stage_version(job):
        for event in reversed(job.get("lifecycle_events", [])):
            before, after = event["before"], event["after"]
            if before["stage_id"] != after["stage_id"]:
                return after["version"]
        return 0

    @staticmethod
    def _current_gate(records, gate):
        if records:
            revision = records[-1]["revision"]
            return next((record for record in reversed(records)
                         if record["gate"] == gate and record["revision"] == revision), None)
        return None

    @staticmethod
    def _gate_result(record):
        return {key: deepcopy(record[key]) for key in _ORCH_GATE_FIELDS} | {"actor_source": record.get("actor_source")}

    def _write_gates_locked(self, job, records):
        previous = job.get("_orch_gates")
        job["_orch_gates"] = records
        try:
            self._save()
        except OSError:
            if previous is None:
                job.pop("_orch_gates")
            else:
                job["_orch_gates"] = previous
            raise

    def publish_orch_gate(self, job_uid, *, coordinator, gate, revision, choices, text,
                          operation_id, add_card):
        """Publish the current card for a trusted coordinator, with retry by operation ID."""
        if (not all(isinstance(value, str) and value.strip()
                    for value in (job_uid, coordinator, gate, text, operation_id))
                or gate not in _ORCH_GATE_STAGE or type(revision) is not int or revision < 1
                or not isinstance(choices, list) or len(choices) < 2
                or not all(isinstance(choice, str) and choice.strip() for choice in choices)
                or len(set(choices)) != len(choices)
                or (gate == "staffing" and set(choices) != {"Start", "Wait"})):
            raise ValueError("invalid Orch gate publication")
        with self._lock:
            job = self._managed_job_locked(job_uid)
            if job is None:
                return None
            records = job.get("_orch_gates", [])
            bound = orch_coordinator(job)
            if (bound and bound.casefold() != coordinator.casefold()
                    or records and (records[-1]["coordinator"] != coordinator or records[-1]["revision"] > revision)):
                return None
            current = self._current_gate(records, gate)
            stage_version = self._stage_version(job)
            existing = next((record for record in records if record["operation_id"] == operation_id), None)
            if existing is not None:
                if (existing is not current or existing["revision"] != revision
                        or existing["choices"] != choices or existing["text"] != text
                        or (existing["choice"] is None and existing["stage_version"] != stage_version)):
                    return None
                return self._gate_result(existing)
            if job["lifecycle"]["stage_id"] != _ORCH_GATE_STAGE[gate]:
                return None
            if (current is not None and current["revision"] == revision
                    and not (gate == "staffing" and current["choice"] == "Wait")):
                return None
            binding = {"job_uid": job_uid, "gate": gate, "revision": revision}
            decision_id = add_card(job["channel"], binding, job.get("source_msg_id"))
            record = dict(binding, decision_id=decision_id, choices=list(choices), choice=None,
                          actor=None, decided_at=None, coordinator=coordinator, operation_id=operation_id,
                          text=text, stage_version=stage_version)
            self._write_gates_locked(job, [*records, record])
            return self._gate_result(record)

    def read_orch_gate(self, job_uid, *, coordinator, decision_id):
        with self._lock:
            job = self._managed_job_locked(job_uid)
            records = job.get("_orch_gates", []) if job else []
            record = next((record for record in records if record["decision_id"] == decision_id), None)
            if (record is None or record["coordinator"] != coordinator
                    or record is not self._current_gate(records, record["gate"])
                    or (record["choice"] is None and record["stage_version"] != self._stage_version(job))):
                return None
            return self._gate_result(record)

    def resolve_orch_gate(self, job_uid, *, decision_id, gate, revision, choice, actor):
        with self._lock:
            job = self._managed_job_locked(job_uid)
            records = job.get("_orch_gates", []) if job else []
            current = self._current_gate(records, gate)
            if (current is None or current["decision_id"] != decision_id or current["revision"] != revision
                    or choice not in current["choices"]):
                return None
            if current["choice"] is not None:
                return self._gate_result(current) if (current["choice"], current["actor"]) == (choice, actor) else None
            if (job["lifecycle"]["stage_id"] != _ORCH_GATE_STAGE[gate]
                    or current["stage_version"] != self._stage_version(job)):
                return None
            committed = dict(current, choice=choice, actor=actor,
                             actor_source="local-browser", decided_at=time.time())
            self._write_gates_locked(job, [committed if record is current else record for record in records])
            return self._gate_result(committed)

    def rename_channel(self, old_name: str, new_name: str):
        with self._lock:
            changed = [job for job in self._jobs if job.get("channel") == old_name]
            if not changed:
                return
            for job in changed:
                job["channel"] = new_name
            try:
                self._save()
            except OSError:
                for job in changed:
                    job["channel"] = old_name
                raise

    def create(self, title: str, job_type: str, channel: str,
               created_by: str, anchor_msg_id: int | None = None,
               assignee: str | None = None,
               body: str | None = None,
               uid: str | None = None,
               status: str | None = None,
               created_at: float | None = None,
               updated_at: float | None = None, *,
               from_proposal: bool = False,
               source_msg_id: int | None = None,
               managed_by: str = "manual",
               proposal_writer: str | None = None,
               proposal_author: str | None = None,
               proposal_author_identity: str | None = None,
               archived_lifecycle: dict | None = None,
               archived_lifecycle_events: list[dict] | None = None) -> dict:
        """Create a new job. Returns the job dict."""
        core = core_status("open" if status is None else status)
        if core is None:
            raise ValueError(f"unknown Job status: {status!r}")
        if source_msg_id is not None and (type(source_msg_id) is not int or source_msg_id < 0):
            raise ValueError("invalid Job source message")
        if managed_by == "orch" and (source_msg_id is None or core != TODO or job_type != "job"):
            raise ValueError("Orch intake requires a source message and a new To Do Job")
        lifecycle = new_lifecycle(core, stage=DRAFTING if from_proposal or managed_by == "orch" else None,
                                  managed_by=managed_by)
        if archived_lifecycle is not None:
            validate_lifecycle(archived_lifecycle)
            if status is not None and archived_lifecycle["core_id"] != core:
                raise ValueError("conflicting archived Job status and lifecycle")
            lifecycle = dict(archived_lifecycle, managed_by="manual", flags=[], version=0)
        if archived_lifecycle_events is not None and (
            not isinstance(archived_lifecycle_events, list)
            or not all(isinstance(record, dict) for record in archived_lifecycle_events)
        ):
            raise ValueError("invalid archived Job lifecycle history")
        validate_lifecycle(lifecycle)
        st = LEGACY_STATUS[lifecycle["core_id"]]
        with self._lock:
            if source_msg_id is not None:
                for existing in self._jobs:
                    if existing.get("source_msg_id") == source_msg_id:
                        return self._snapshot(existing)
            if from_proposal and anchor_msg_id is not None:
                for existing in self._jobs:
                    if existing.get("anchor_msg_id") == anchor_msg_id:
                        return self._snapshot(existing)
            now = time.time()
            a = {
                "id": self._next_id,
                "uid": uid or str(uuid.uuid4()),
                "type": job_type,
                "title": title.strip()[:120],
                "body": (body or "").strip()[:1000],
                "status": st,
                "lifecycle": lifecycle,
                "channel": channel,
                "created_by": created_by,
                "assignee": assignee or "",
                "anchor_msg_id": anchor_msg_id,
                "messages": [],
                "created_at": created_at or now,
                "updated_at": updated_at or now,
                "sort_order": self._next_sort_order_locked(st),
            }
            if source_msg_id is not None:
                a["source_msg_id"] = source_msg_id
            if proposal_writer is not None:
                a["proposal_writer"] = proposal_writer
            if from_proposal or proposal_author is not None or proposal_author_identity is not None:
                a["proposal_author"] = proposal_author
                a["proposal_author_identity"] = proposal_author_identity
            if archived_lifecycle_events:
                a["imported_lifecycle_events"] = deepcopy(archived_lifecycle_events)
            self._next_id += 1
            self._jobs.append(a)
            try:
                self._save()
            except OSError:
                self._jobs.pop()
                self._next_id -= 1
                raise
            result = self._snapshot(a)
        self._fire("create", result)
        return result

    def update_status(self, job_id: int, status: str, *, expected_status: str | None = None,
                      expected_scope: dict | None = None, cancel: bool = False) -> dict | None:
        """Adapt legacy status writes to the same guarded lifecycle events."""
        target = core_status(status)
        if target is None:
            return None
        with self._lock:
            job = next((item for item in self._jobs if item["id"] == job_id), None)
            if job is None or (expected_scope and any(job.get(key) != value for key, value in expected_scope.items())):
                return None
            current = job["lifecycle"]["core_id"]
            if target == current or (expected_status is not None and core_status(expected_status) != current):
                return self._snapshot(job)
            events = {TODO: "return_to_backlog", ACTIVE: "start",
                      CLOSED: "cancel" if current == TODO or cancel else "complete"}
            event = events[target]
            updated = next_lifecycle(job["lifecycle"], event, "human")
            if updated is None:
                return None
            result = self._commit_lifecycle_locked(
                job, updated, event, "legacy-client", "human", str(uuid.uuid4()), None
            )
        self._fire("update", result)
        return result

    def transition_job(self, job_id: int, event: str, *, actor: str, actor_kind: str,
                       expected_uid: str, expected_version: int, operation_id: str,
                       reason: str | None = None, flags: list[str] | None = None,
                       approved_reopen: bool = False,
                       expected_scope: dict | None = None) -> dict | None:
        """Commit an authenticated caller's event and its audit entry atomically."""
        if (not all(isinstance(value, str) and value.strip()
                    for value in (event, actor, actor_kind, expected_uid, operation_id))
                or type(expected_version) is not int or expected_version < 0
                    or (reason is not None and not isinstance(reason, str))
                    or type(approved_reopen) is not bool):
            raise ValueError("invalid Job transition request")
        reason = reason.strip() if reason else None
        with self._lock:
            job = next((item for item in self._jobs if item["id"] == job_id), None)
            if job is None or job.get("uid") != expected_uid:
                return None
            managed = job["lifecycle"]["managed_by"] == "orch"
            if actor_kind == "orch":
                assigned = orch_coordinator(job) if managed else job.get("assignee")
                if ((assigned and assigned.casefold() != actor.casefold())
                        or (not managed and not assigned)):
                    return None
            if expected_scope and any(
                job.get(key) != value for key, value in expected_scope.items()
                if not (actor_kind == "orch" and managed and key == "assignee")
            ):
                return None
            seen = next((record for record in job.get("lifecycle_events", [])
                         if record["operation_id"] == operation_id), None)
            if seen is not None:
                if (seen["event"], seen["actor"], seen["actor_kind"], seen["before"]["version"],
                        seen["reason"], seen.get("approved_reopen", False)) != (
                        event, actor, actor_kind, expected_version, reason, approved_reopen
                ) or (event == "set_flags" and seen["after"]["flags"] != flags):
                    raise ValueError("Job operation ID was already used")
                result = self._snapshot(job)
                result["lifecycle"] = deepcopy(seen["after"])
                result["status"] = LEGACY_STATUS[seen["after"]["core_id"]]
                return result
            if job["lifecycle"]["version"] != expected_version:
                return None
            updated = next_lifecycle(job["lifecycle"], event, actor_kind, reason=reason,
                                     flags=flags, approved_reopen=approved_reopen)
            if updated is None:
                return None
            result = self._commit_lifecycle_locked(
                job, updated, event, actor, actor_kind, operation_id, reason, approved_reopen
            )
        self._fire("update", result)
        return result

    def _commit_lifecycle_locked(self, job, updated, event, actor, actor_kind,
                                 operation_id, reason, approved_reopen=False, *, reopen_requests=None):
        previous = dict(job)
        status = LEGACY_STATUS[updated["core_id"]]
        if status != job["status"]:
            job["sort_order"] = self._next_sort_order_locked(status)
        now = time.time()
        record = {"operation_id": operation_id, "event": event, "actor": actor, "actor_kind": actor_kind,
                  "approved_reopen": approved_reopen,
                  "time": now, "reason": reason, "before": deepcopy(job["lifecycle"]), "after": deepcopy(updated)}
        if reopen_requests is not None:
            approval = reopen_requests[-1]
            record["reopen_approval"] = {key: deepcopy(approval[key])
                                         for key in ("id", "approval_basis", "decisions")}
            source = approval["decisions"][-1].get("source")
            if source:
                record["source"] = source
            job["reopen_requests"] = reopen_requests
        if actor_kind == "orch" and updated["managed_by"] == "orch" and not job.get("orch_coordinator"):
            job["orch_coordinator"] = actor
        job.update(status=status, lifecycle=deepcopy(updated), updated_at=now,
                   lifecycle_events=[*job.get("lifecycle_events", []), record])
        try:
            self._save()
        except OSError:
            job.clear()
            job.update(previous)
            raise
        return self._snapshot(job)

    def request_reopen(self, job_id, *, expected_uid, expected_version, requester, reason):
        """Record a reopen request without changing the Job lifecycle."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("A reopen request requires a reason")
        reason = reason.strip()
        with self._lock:
            job = next((item for item in self._jobs if item["id"] == job_id), None)
            if job is None or job["uid"] != expected_uid:
                raise ReopenConflict("Job no longer matches the requested identity")
            state = job["lifecycle"]
            if (type(expected_version) is not int or state["version"] != expected_version
                    or state["core_id"] != CLOSED or state["close_outcome"] != "completed"):
                raise ReopenConflict("Job is no longer at the requested completed version")
            requests = job.get("reopen_requests", [])
            current = requests[-1] if requests else None
            if current and current["job_version"] == expected_version and current["status"] == "pending":
                if (current["requester"], current["reason"]) != (requester, reason):
                    raise ReopenConflict("A reopen request is already pending")
                return {"request": deepcopy(current), "job": self._snapshot(job)}
            current = create_request(request_id=str(uuid.uuid4()), job_uid=expected_uid,
                                     job_version=expected_version, requester=requester,
                                     reason=reason, now=time.time())
            self._save_reopen_requests_locked(job, [*requests, current])
            snapshot = self._snapshot(job)
        self._fire("update", snapshot)
        return {"request": deepcopy(current), "job": snapshot}

    def decide_reopen(self, job_id, request_id, *, expected_uid, expected_version, actor, role, decision,
                      source=None):
        """Persist an authorized decision and any resulting reopen atomically."""
        with self._lock:
            job = next((item for item in self._jobs if item["id"] == job_id), None)
            if job is None or job["uid"] != expected_uid:
                raise ReopenConflict("Job no longer matches the requested identity")
            requests = job.get("reopen_requests", [])
            current = requests[-1] if requests else None
            if (not current or current["id"] != request_id or current["job_uid"] != expected_uid
                    or type(expected_version) is not int or current["job_version"] != expected_version):
                raise ReopenConflict("Reopen request no longer matches the Job")
            state = job["lifecycle"]
            try:
                updated = record_decision(current, actor=actor, role=role, decision=decision,
                                          now=time.time(), source=source)
            except ValueError as exc:
                raise ReopenConflict(str(exc)) from exc
            if updated is current:
                if current["status"] == "pending" and state["version"] != expected_version:
                    raise ReopenConflict("Job has changed since the reopen request")
                return {"request": deepcopy(current), "job": self._snapshot(job)}
            if state["version"] != expected_version:
                raise ReopenConflict("Job has changed since the reopen request")
            replacement = [*requests[:-1], updated]
            if updated["status"] == "approved":
                next_state = next_lifecycle(state, "reopen", "human",
                                            reason=updated["reason"], approved_reopen=True)
                if next_state is None:
                    raise ReopenConflict("Job can no longer be reopened")
                if next_state["managed_by"] == "orch":
                    # Orch clears this gate after persisting the fresh corrective task.
                    next_state["flags"] = ["paused"]
                kind = "human" if role == "owner" else "agent"
                snapshot = self._commit_lifecycle_locked(
                    job, next_state, "reopen", actor, kind, request_id, updated["reason"],
                    approved_reopen=True, reopen_requests=replacement,
                )
            else:
                self._save_reopen_requests_locked(job, replacement)
                snapshot = self._snapshot(job)
        self._fire("update", snapshot)
        return {"request": deepcopy(updated), "job": snapshot}

    def _save_reopen_requests_locked(self, job, requests):
        original = job.get("reopen_requests")
        job["reopen_requests"] = requests
        try:
            self._save()
        except OSError:
            if original is None:
                job.pop("reopen_requests")
            else:
                job["reopen_requests"] = original
            raise

    def update_title(self, job_id: int, title: str) -> dict | None:
        with self._lock:
            for a in self._jobs:
                if a["id"] == job_id:
                    a["title"] = title.strip()[:120]
                    a["updated_at"] = time.time()
                    self._save()
                    result = self._snapshot(a)
                    break
            else:
                return None
        self._fire("update", result)
        return result

    def update_assignee(self, job_id: int, assignee: str) -> dict | None:
        with self._lock:
            for a in self._jobs:
                if a["id"] == job_id:
                    a["assignee"] = assignee.strip()
                    a["updated_at"] = time.time()
                    self._save()
                    result = self._snapshot(a)
                    break
            else:
                return None
        self._fire("update", result)
        return result

    def add_message(self, job_id: int, sender: str, text: str,
                    attachments: list | None = None,
                    msg_type: str = "chat",
                    uid: str | None = None,
                    timestamp: float | None = None,
                    time_str: str | None = None,
                    sender_identity: str | None = None,
                    recipient_identity: str | None = None,
                    expected_scope: dict | None = None, *,
                    coordinator: str | None = None,
                    archived_resolution: str | None = None) -> dict | None:
        """Add a message to a job's conversation. Returns the message."""
        with self._lock:
            for a in self._jobs:
                if a["id"] == job_id:
                    if expected_scope and any(a.get(key) != value for key, value in expected_scope.items()):
                        return None
                    state = a["lifecycle"]
                    if expected_scope and state["managed_by"] == "orch" and coordinator is None:
                        return None
                    old_coordinator = a.get("orch_coordinator")
                    if coordinator is not None:
                        bound = orch_coordinator(a)
                        if (state["managed_by"] != "orch" or state["core_id"] not in (TODO, ACTIVE)
                                or (bound and bound.casefold() != coordinator.casefold())):
                            return None
                        if not old_coordinator:
                            a["orch_coordinator"] = coordinator
                    msg_id = len(a["messages"])
                    ts = timestamp if timestamp is not None else time.time()
                    msg = {
                        "id": msg_id,
                        "uid": uid or str(uuid.uuid4()),
                        "sender": sender,
                        "text": text.strip(),
                        "time": time_str or time.strftime("%H:%M:%S"),
                        "timestamp": ts,
                        "attachments": attachments or [],
                    }
                    if msg_type != "chat":
                        msg["type"] = msg_type
                    if msg_type == "suggestion" and archived_resolution in ("accepted", "dismissed"):
                        msg["resolved"] = archived_resolution
                    if sender_identity:
                        msg["sender_identity"] = sender_identity
                    if recipient_identity:
                        msg["recipient_identity"] = recipient_identity
                    a["messages"].append(msg)
                    old_updated_at = a["updated_at"]
                    a["updated_at"] = time.time()
                    try:
                        self._save()
                    except OSError:
                        a["messages"].pop()
                        a["updated_at"] = old_updated_at
                        if coordinator is not None and not old_coordinator:
                            a.pop("orch_coordinator", None)
                        raise
                    result_msg = dict(msg)
                    result_msg["job_id"] = job_id
                    break
            else:
                return None
        self._fire("message", {"job_id": job_id, "message": result_msg})
        return result_msg

    def channel_activity(self) -> dict[str, float]:
        """Epoch timestamp of the newest real reply in each channel's job threads.

        Deleted replies and automated (system) notices don't count as activity."""
        latest: dict[str, float] = {}
        with self._lock:
            for job in self._jobs:
                channel = job.get("channel", "general")
                for m in job.get("messages", []):
                    ts = m.get("timestamp")
                    if m.get("deleted") or m.get("sender") == "system" or m.get("type") == "system":
                        continue
                    if isinstance(ts, (int, float)) and ts > latest.get(channel, 0):
                        latest[channel] = ts
        return latest

    def get_messages(self, job_id: int) -> list[dict] | None:
        """Get all messages for a job."""
        with self._lock:
            for a in self._jobs:
                if a["id"] == job_id:
                    return list(a["messages"])
            return None

    def delete_message(self, job_id: int, msg_id: int) -> dict | None:
        """Soft-delete a message from a job conversation by message id."""
        with self._lock:
            for a in self._jobs:
                if a["id"] != job_id:
                    continue
                msgs = a.get("messages", [])
                hit = None
                for i, m in enumerate(msgs):
                    try:
                        mid = int(m.get("id", -1))
                    except (TypeError, ValueError):
                        mid = -1
                    if mid == msg_id:
                        hit = (i, m)
                        break
                if hit is None:
                    return None
                _, msg = hit
                if msg.get("deleted"):
                    return {"job_id": job_id, "message_id": msg_id}
                msg["deleted"] = True
                msg["text"] = ""
                msg["attachments"] = []
                msg["updated_at"] = time.time()
                a["updated_at"] = time.time()
                self._save()
                payload = {"job_id": job_id, "message_id": msg_id}
                break
            else:
                return None
        self._fire("message_delete", payload)
        return payload

    def delete(self, job_id: int) -> dict | None:
        """Permanently delete a job."""
        with self._lock:
            for i, a in enumerate(self._jobs):
                if a["id"] == job_id:
                    if a["lifecycle"]["core_id"] != CLOSED:
                        return None
                    # Save the allocation floor before removing its evidence from jobs.json.
                    atomic_write_text(self._id_path, str(self._next_id))
                    removed = self._jobs.pop(i)
                    try:
                        self._save()
                    except OSError:
                        self._jobs.insert(i, removed)
                        raise
                    result = self._snapshot(removed)
                    break
            else:
                return None
        self._fire("delete", result)
        return result

    def reorder(self, status: str, ordered_ids: list[int]) -> list[dict]:
        """Reorder jobs within a status group by explicit id order (top to bottom)."""
        core = core_status(status)
        if core is None:
            return []
        status = LEGACY_STATUS[core]
        with self._lock:
            self._ensure_sort_orders_locked()
            group = [
                a for a in self._jobs
                if a.get("status") == status
            ]
            if not group:
                return []

            by_id = {int(a["id"]): a for a in group}
            ordered: list[int] = []
            seen = set()
            for raw in ordered_ids:
                try:
                    aid = int(raw)
                except (TypeError, ValueError):
                    continue
                if aid in by_id and aid not in seen:
                    ordered.append(aid)
                    seen.add(aid)

            if not ordered:
                return []

            existing_sorted = sorted(
                group,
                key=lambda x: (int(x.get("sort_order", 0) or 0), float(x.get("updated_at", 0) or 0)),
                reverse=True,
            )
            for a in existing_sorted:
                aid = int(a["id"])
                if aid not in seen:
                    ordered.append(aid)

            changed: list[dict] = []
            n = len(ordered)
            for idx, aid in enumerate(ordered):
                item = by_id.get(aid)
                if not item:
                    continue
                new_order = n - idx
                old_order = int(item.get("sort_order", 0) or 0)
                if old_order != new_order:
                    item["sort_order"] = new_order
                    changed.append(self._snapshot(item))

            if changed:
                self._save()

        for item in changed:
            self._fire("update", item)
        return changed
