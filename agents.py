"""Agent trigger — writes to queue files picked up by visible worker terminals."""

import json
import logging
from pathlib import Path
import time
import uuid

from atomic_io import atomic_write_text

log = logging.getLogger(__name__)


def _take_spooled_trigger(queue: Path) -> str | None:
    for path in sorted(queue.glob("*.json")):
        try:
            payload = path.read_text(encoding="utf-8")
            path.unlink()
        except FileNotFoundError:
            continue
        return payload
    return None


def take_identity_trigger(data_dir: Path, identity_id: str) -> str | None:
    """Read one complete trigger addressed to this registration."""
    if identity_id:
        return _take_spooled_trigger(data_dir / "identity_triggers" / identity_id)
    return None


def take_trigger(queue_file: Path, identity_id: str = "") -> dict | None:
    """Take one wake, preserving any other queued work for later polls."""
    payload = take_identity_trigger(queue_file.parent, identity_id)
    if payload is not None:
        entry = json.loads(payload)
        # Older identity spool records are suggestion acceptances.
        entry.setdefault("kind", "suggestion_accepted")
        return entry

    payload = _take_spooled_trigger(queue_file.with_suffix(".d"))
    if payload is not None:
        return json.loads(payload)

    # Older servers used a JSONL file. Rotate it before reading so a new
    # append that opens the original name cannot be erased by the reader.
    pending = queue_file.with_name(f"{queue_file.name}.pending")
    if not pending.exists():
        try:
            queue_file.rename(pending)
        except FileNotFoundError:
            return None
    lines = pending.read_text(encoding="utf-8").splitlines(keepends=True)
    for index, line in enumerate(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            if index + 1 < len(lines):
                atomic_write_text(pending, "".join(lines[index + 1:]))
            else:
                pending.unlink()
            return entry
    pending.unlink()
    return None


def _spool_trigger(queue: Path, entry: dict) -> None:
    queue.mkdir(parents=True, exist_ok=True)
    atomic_write_text(queue / f"{time.time_ns():020d}-{uuid.uuid4().hex}.json", json.dumps(entry))


class AgentTrigger:
    def __init__(self, registry, data_dir: str = "./data"):
        self._registry = registry
        self._data_dir = Path(data_dir)

    def is_available(self, name: str) -> bool:
        return self._registry.is_registered(name)

    def notify_suggestion_accepted(self, identity_id: str, channel: str, job_id: int):
        """Queue a Job wake that survives renames and cannot follow a reused ID."""
        if not self._registry.is_active_identity(identity_id):
            return
        queue = self._data_dir / "identity_triggers" / identity_id
        _spool_trigger(queue, {"channel": channel, "job_id": job_id, "kind": "suggestion_accepted"})

    def get_status(self) -> dict:
        from mcp_bridge import is_online, is_active, is_needs_input, is_activity_known, get_role
        instances = self._registry.get_all()
        return {
            name: {
                "available": is_online(name),
                "busy": is_active(name) if is_activity_known(name) else None,
                "activity_known": is_activity_known(name),
                "needs_input": is_online(name) and is_needs_input(name),
                "label": info["label"],
                "color": info["color"],
                "role": get_role(name),
            }
            for name, info in instances.items()
        }

    async def trigger(self, agent_name: str, message: str = "", channel: str = "general",
                      job_id: int | None = None, **kwargs):
        """Queue a wake for the worker."""
        self.trigger_sync(agent_name, message, channel, job_id, **kwargs)

    def trigger_sync(self, agent_name: str, message: str = "", channel: str = "general",
                     job_id: int | None = None, **kwargs):
        """Publish a complete wake for the addressed registration."""
        instance = self._registry.get_instance(agent_name) if self._registry else None
        if instance and instance.get("identity_id"):
            queue = self._data_dir / "identity_triggers" / instance["identity_id"]
        else:
            queue = self._data_dir / f"{agent_name}_queue.d"
        entry = {
            "sender": message.split(":")[0].strip() if ":" in message else "?",
            "text": message,
            "time": time.strftime("%H:%M:%S"),
            "channel": channel,
            "kind": "mention",
        }
        custom_prompt = kwargs.get("prompt", "")
        if isinstance(custom_prompt, str) and custom_prompt.strip():
            entry["prompt"] = custom_prompt.strip()
        if job_id is not None:
            entry["job_id"] = job_id

        _spool_trigger(queue, entry)

        log.info("Queued @%s trigger (ch=%s, job=%s): %s", agent_name, channel, job_id, message[:80])
