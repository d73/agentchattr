"""JSONL persistence with a disk-backed runtime message index."""

import json
import logging
import os
import sqlite3
import time
import threading
import uuid
import weakref
from pathlib import Path

from atomic_io import atomic_write_text

log = logging.getLogger(__name__)


class MessageStore:
    def __init__(self, path: str):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._todos_path = self._path.parent / "todos.json"
        self._id_path = self._path.with_name(self._path.name + ".next-id")
        # An unnamed, file-backed SQLite database is deleted when it closes.
        # The existing JSONL log remains the source of truth across restarts.
        self._db = sqlite3.connect("", check_same_thread=False, isolation_level=None)
        weakref.finalize(self, self._db.close)
        self._db.executescript(
            "PRAGMA temp_store = FILE;"
            "PRAGMA cache_size = -2048;"
            "CREATE TABLE messages (position INTEGER PRIMARY KEY, msg_id INTEGER NOT NULL,"
            " uid TEXT, channel TEXT, has_channel INTEGER, sender TEXT, activity REAL, payload TEXT NOT NULL);"
            "CREATE INDEX messages_by_id ON messages(msg_id);"
            "CREATE INDEX messages_by_uid ON messages(uid);"
            "CREATE INDEX messages_by_channel ON messages(channel, position);"
        )
        self._next_id: int = 0  # monotonically increasing, survives deletions
        self._todos: dict[int, str] = {}  # msg_id → "todo" | "done"
        self._lock = threading.Lock()
        self._log_dirty = False
        self._log_error = None
        self._log_repair_required = False
        self._log_callbacks: list = []
        self._callbacks: list = []  # called on each new message
        self._todo_callbacks: list = []  # called on todo changes
        self._delete_callbacks: list = []  # called on message deletion
        self.upload_dir = self._path.parent.parent / "uploads"  # Default fallback
        self._load()
        if self._id_path.exists():
            try:
                marker = int(self._id_path.read_text(encoding="utf-8"))
                if marker < 0:
                    raise ValueError("next ID must not be negative")
                self._next_id = max(self._next_id, marker)
            except (OSError, ValueError) as exc:
                raise ValueError(f"Cannot read message ID marker {self._id_path.resolve()}: {exc}. The file was not changed.") from exc
        self._load_todos()

    def _load(self):
        if not self._path.exists():
            return
        max_id = -1
        self._db.execute("BEGIN")
        with open(self._path, "rb") as f:
            for i, raw_line in enumerate(f):
                if not raw_line.strip():
                    continue
                try:
                    msg = json.loads(raw_line.decode("utf-8"))
                    if "id" not in msg:
                        msg["id"] = i
                    if msg["id"] > max_id:
                        max_id = msg["id"]
                    self._insert_message(msg)
                    # A complete JSON record without a newline must be rewritten
                    # before appending the next one.
                    if not raw_line.endswith(b"\n"):
                        self._log_dirty = True
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    if not raw_line.endswith(b"\n"):
                        self._log_repair_required = True
                        self._log_dirty = True
                        self._log_error = (f"Incomplete final record at line {i + 1}: {exc}. "
                                           "The file was left unchanged. Repair the log and restart; "
                                           "new messages are available only in this running session.")
                        log.warning("Message log %s: %s", self._path.resolve(), self._log_error)
                        break
                    raise ValueError(f"Cannot read message history {self._path.resolve()}, line {i + 1}: {exc}. The file was not changed.") from exc
        self._db.commit()
        self._next_id = max_id + 1

    def on_message(self, callback):
        """Register a callback(msg) called whenever a message is added."""
        self._callbacks.append(callback)

    def on_log_status(self, callback):
        self._log_callbacks.append(callback)

    def log_status(self):
        with self._lock:
            return {"path": str(self._path.resolve()), "error": self._log_error}

    def add(self, sender: str, text: str, msg_type: str = "chat",
            attachments: list | None = None, reply_to: int | None = None,
            channel: str = "general",
            metadata: dict | None = None,
            uid: str | None = None,
            timestamp: float | None = None,
            time_str: str | None = None,
            _bulk: bool = False, *,
            sender_kind: str | None = None,
            sender_identity: str | None = None,
            sender_base: str | None = None,
            once: bool = False) -> dict:
        """Add a message. With once=True, persist its stable UID before delivery."""
        if once and (not uid or _bulk):
            raise ValueError("An idempotent message requires a UID and immediate persistence")
        persistence_error = None
        log_status_changed = False
        with self._lock:
            if once:
                row = self._db.execute(
                    "SELECT payload FROM messages WHERE uid = ? ORDER BY position LIMIT 1", (json.dumps(uid),)
                ).fetchone()
                if row:
                    return json.loads(row[0])
            previous_error = self._log_error
            ts = timestamp if timestamp is not None else time.time()
            msg = {
                "id": self._next_id,
                "uid": uid or str(uuid.uuid4()),
                "sender": sender,
                "text": text,
                "type": msg_type,
                "timestamp": ts,
                "time": time_str or time.strftime("%H:%M:%S"),
                "attachments": attachments or [],
                "channel": channel,
            }
            if reply_to is not None:
                msg["reply_to"] = reply_to
            if sender_kind is not None:
                msg["sender_kind"] = sender_kind
            if sender_identity is not None:
                msg["sender_identity"] = sender_identity
            if sender_base is not None:
                msg["sender_base"] = sender_base
            if metadata:
                msg["metadata"] = metadata
            position = self._insert_message(msg)
            self._next_id += 1
            if not _bulk:
                try:
                    if self._log_repair_required:
                        raise OSError(self._log_error)
                    if once or self._log_dirty:
                        self._rewrite()
                    else:
                        with open(self._path, "a", encoding="utf-8") as f:
                            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
                            f.flush()
                            os.fsync(f.fileno())
                    self._log_dirty = False
                    self._log_error = None
                except OSError as exc:
                    if once:
                        self._db.execute("DELETE FROM messages WHERE position = ?", (position,))
                        self._next_id -= 1
                        raise
                    self._log_dirty = True
                    persistence_error = str(exc)
                    self._log_error = persistence_error
                    log.warning("Message %s delivered but log write failed: %s", msg["id"], exc)
                log_status_changed = self._log_error != previous_error

        # Fire callbacks outside the lock (skip during bulk import)
        if not _bulk:
            if log_status_changed:
                for cb in self._log_callbacks:
                    try:
                        cb()
                    except Exception:
                        log.exception("Log status callback failed")
            for cb in self._callbacks:
                try:
                    cb(msg)
                except Exception:
                    pass

        if persistence_error:
            return {**msg, "persistence_error": persistence_error}
        return msg

    def flush_bulk(self):
        """Flush newly indexed messages to the JSONL log after bulk adds."""
        with self._lock:
            self._rewrite()

    @staticmethod
    def _message_fields(msg):
        timestamp = msg.get("timestamp")
        activity = timestamp if isinstance(timestamp, (int, float)) and timestamp > 0 else None
        return (
            msg["id"], json.dumps(msg.get("uid")), json.dumps(msg.get("channel", "general")), "channel" in msg,
            json.dumps(msg.get("sender")), activity, json.dumps(msg, ensure_ascii=False),
        )

    def _insert_message(self, msg):
        return self._db.execute(
            "INSERT INTO messages (msg_id, uid, channel, has_channel, sender, activity, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            self._message_fields(msg),
        ).lastrowid

    def _message_row(self, msg_id):
        row = self._db.execute(
            "SELECT position, payload FROM messages WHERE msg_id = ? ORDER BY position LIMIT 1", (msg_id,)
        ).fetchone()
        return (row[0], json.loads(row[1])) if row else (None, None)

    def _save_message(self, position, msg):
        self._db.execute(
            "UPDATE messages SET msg_id = ?, uid = ?, channel = ?, has_channel = ?, sender = ?, activity = ?, payload = ?"
            " WHERE position = ?",
            (*self._message_fields(msg), position),
        )

    def update_reply_to(self, msg_id: int, reply_to: int):
        """Set reply_to on an existing message (used by import to rebuild links)."""
        with self._lock:
            position, msg = self._message_row(msg_id)
            if msg is not None:
                msg["reply_to"] = reply_to
                self._save_message(position, msg)
                self._rewrite()

    def _rewrite(self):
        """Stream the runtime index to the JSONL log before atomically replacing it."""
        if self._log_repair_required:
            raise OSError(self._log_error)
        rows = self._db.execute("SELECT payload FROM messages ORDER BY position")
        atomic_write_text(self._path, (payload + "\n" for (payload,) in rows))

    def next_job_id_from_history(self) -> int:
        """Reserve IDs referenced by breadcrumbs written before Job ID persistence."""
        highest_id = 0
        before_id = self.last_id + 1
        while True:
            messages, has_more = self.get_before(before_id, count=512)
            for message in messages:
                if message.get("type") != "job_created":
                    continue
                metadata = message.get("metadata")
                job_id = metadata.get("job_id") if isinstance(metadata, dict) else None
                if type(job_id) is int and highest_id < job_id < 2**53 - 1:
                    highest_id = job_id
            if not has_more:
                return highest_id + 1
            before_id = messages[0]["id"]

    def get_by_id(self, msg_id: int) -> dict | None:
        with self._lock:
            return self._message_row(msg_id)[1]

    def get_recent(self, count: int = 50, channel: str | None = None) -> list[dict]:
        where, parameters = (" WHERE channel = ?", (json.dumps(channel),)) if channel else ("", ())
        with self._lock:
            if count <= 0:
                rows = self._db.execute(
                    "SELECT payload FROM messages" + where + " ORDER BY position LIMIT -1 OFFSET ?",
                    (*parameters, -count),
                )
                return [json.loads(payload) for (payload,) in rows]
            rows = self._db.execute(
                "SELECT payload FROM messages" + where + " ORDER BY position DESC LIMIT ?", (*parameters, count)
            )
            messages = [json.loads(payload) for (payload,) in rows]
            messages.reverse()
            return messages

    def get_before(self, before_id: int, count: int = 50, channel: str | None = None) -> tuple[list[dict], bool]:
        """Up to `count` messages older than `before_id` (oldest first) and whether more exist."""
        count = max(1, count)
        where, parameters = (" AND channel = ?", (json.dumps(channel),)) if channel else ("", ())
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM messages WHERE msg_id < ?" + where + " ORDER BY position DESC LIMIT ?",
                (before_id, *parameters, count + 1),
            ).fetchall()
            return [json.loads(payload) for (payload,) in reversed(rows[:count])], len(rows) > count

    def count_channel(self, channel: str) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM messages WHERE channel = ?", (json.dumps(channel),)).fetchone()[0]

    def channel_activity(self) -> dict[str, float]:
        """Epoch timestamp of the newest message in each channel that has any."""
        with self._lock:
            return {
                json.loads(channel): timestamp
                for channel, timestamp in self._db.execute(
                    "SELECT channel, MAX(activity) FROM messages WHERE activity IS NOT NULL GROUP BY channel"
                )
            }

    def get_since(self, since_id: int = 0, channel: str | None = None, *, limit: int | None = None) -> list[dict]:
        where, parameters = (" AND channel = ?", (json.dumps(channel),)) if channel else ("", ())
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM messages WHERE msg_id > ?" + where + " ORDER BY position LIMIT ?",
                (since_id, *parameters, limit if limit is not None else -1),
            )
            return [json.loads(payload) for (payload,) in rows]

    def delete(self, msg_ids: list[int]) -> list[int]:
        """Delete messages by ID. Returns list of IDs actually deleted."""
        deleted = []
        deleted_attachments = []
        with self._lock:
            for mid in msg_ids:
                position, msg = self._message_row(mid)
                if msg is None:
                    continue
                for att in msg.get("attachments", []):
                    url = att.get("url", "")
                    if url.startswith("/uploads/"):
                        deleted_attachments.append(url.split("/")[-1])
                self._todos.pop(mid, None)
                self._db.execute("DELETE FROM messages WHERE position = ?", (position,))
                deleted.append(mid)
            if deleted:
                self._rewrite_jsonl()
                self._save_todos()

        # Clean up uploaded images outside the lock
        for filename in deleted_attachments:
            filepath = self.upload_dir / filename
            if filepath.exists():
                try:
                    filepath.unlink()
                except Exception:
                    pass

        # Fire callbacks
        for cb in self._delete_callbacks:
            try:
                cb(deleted)
            except Exception:
                pass

        return deleted

    def on_delete(self, callback):
        """Register a callback(ids) called when messages are deleted."""
        self._delete_callbacks.append(callback)

    def update_message(self, msg_id: int, updates: dict) -> dict | None:
        """Update fields on a message. Returns the updated message or None."""
        with self._lock:
            position, msg = self._message_row(msg_id)
            if msg is None:
                return None
            msg.update(updates)
            self._save_message(position, msg)
            self._rewrite_jsonl()
            return msg

    def project_orch_gate_decision(self, decision: dict) -> dict | None:
        """Refresh the display from a fully committed Orch gate decision."""
        binding = {key: decision[key] for key in ("job_uid", "gate", "revision")}
        with self._lock:
            position, card = self._message_row(decision["decision_id"])
            if not card or card.get("type") != "decision":
                return None
            metadata = card.get("metadata") or {}
            if metadata.get("orch_gate") != binding or metadata.get("choices") != decision["choices"]:
                return None
            if (metadata.get("resolved"), metadata.get("chosen")) == (True, decision["choice"]):
                return None
            updated = dict(card, metadata=dict(metadata, resolved=True, chosen=decision["choice"]))
            try:
                self._save_message(position, updated)
                self._rewrite_jsonl()
            except OSError:
                self._save_message(position, card)
                raise
            return updated

    def _rewrite_jsonl(self):
        """Save the ID high-water mark before rewriting messages."""
        if self._log_repair_required:
            raise OSError(self._log_error)
        atomic_write_text(self._id_path, str(self._next_id))
        self._rewrite()

    def _delete_channel_messages(self, channel, *, include_legacy_general, always_rewrite):
        # Match the normal channel index, but keep channel-less legacy messages
        # when deleting an explicitly named channel.
        where = "channel = ?"
        if not include_legacy_general and channel == "general":
            where += " AND has_channel"
        parameters = (json.dumps(channel),)
        todo_ids = [
            msg_id for msg_id in self._todos
            if self._db.execute("SELECT 1 FROM messages WHERE msg_id = ? AND " + where, (msg_id, *parameters)).fetchone()
        ]
        removed = self._db.execute("DELETE FROM messages WHERE " + where, parameters).rowcount
        if removed or always_rewrite:
            self._rewrite_jsonl()
        if removed:
            for msg_id in todo_ids:
                del self._todos[msg_id]
            self._save_todos()

    def clear(self, channel: str | None = None):
        """Wipe messages and rewrite the log file."""
        with self._lock:
            if channel:
                self._delete_channel_messages(channel, include_legacy_general=True, always_rewrite=True)
            else:
                self._db.execute("DELETE FROM messages")
                self._rewrite_jsonl()
                self._todos.clear()
                self._save_todos()

    def rename_channel(self, old_name: str, new_name: str):
        """Migrate all messages from old_name to new_name."""
        with self._lock:
            self._rename_field("channel", "channel = ? AND has_channel", old_name, new_name)

    def rename_sender(self, old_name: str, new_name: str, identity: str | None = None) -> int:
        """Rename matching agent messages. Legacy history can match only the exact old ID."""
        def matches_identity(msg):
            return msg.get("sender_kind") != "human" and (
                not msg.get("sender_identity") or msg["sender_identity"] == identity
            )

        with self._lock:
            return self._rename_field("sender", "sender = ?", old_name, new_name, matches_identity)

    def _rename_field(self, field, condition, old_name, new_name, include=None):
        position = updated = 0
        while True:
            rows = self._db.execute(
                "SELECT position, payload FROM messages WHERE position > ? AND " + condition
                + " ORDER BY position LIMIT 128",
                (position, json.dumps(old_name)),
            ).fetchall()
            if not rows:
                break
            for position, payload in rows:
                msg = json.loads(payload)
                if include is None or include(msg):
                    msg[field] = new_name
                    self._save_message(position, msg)
                    updated += 1
        if updated:
            self._rewrite_jsonl()
        return updated

    def delete_channel(self, name: str):
        """Remove all messages belonging to a deleted channel."""
        with self._lock:
            self._delete_channel_messages(name, include_legacy_general=False, always_rewrite=False)

    # --- Todos ---

    def _load_todos(self):
        # Migrate old pins.json (list of ints) → todos.json (dict of id→status)
        old_pins = self._todos_path.parent / "pins.json"
        if old_pins.exists() and not self._todos_path.exists():
            try:
                ids = json.loads(old_pins.read_text("utf-8"))
                if isinstance(ids, list):
                    self._todos = {int(i): "todo" for i in ids}
                    self._save_todos()
                    old_pins.unlink()
            except (OSError, ValueError, TypeError) as exc:
                raise ValueError(f"Cannot migrate pinned messages from {old_pins.resolve()}: {exc}. Check the file before retrying.") from exc

        if self._todos_path.exists():
            try:
                raw = json.loads(self._todos_path.read_text("utf-8"))
                self._todos = {int(k): v for k, v in raw.items()}
            except (OSError, ValueError, TypeError) as exc:
                raise ValueError(f"Cannot load pinned messages from {self._todos_path.resolve()}: {exc}. The file was not changed.") from exc

    def _save_todos(self):
        atomic_write_text(
            self._todos_path,
            json.dumps({str(k): v for k, v in self._todos.items()}, indent=2),
        )

    def on_todo(self, callback):
        """Register a callback(msg_id, status) called on todo changes.
        status is 'todo', 'done', or None (removed)."""
        self._todo_callbacks.append(callback)

    def _fire_todo(self, msg_id: int, status: str | None):
        for cb in self._todo_callbacks:
            try:
                cb(msg_id, status)
            except Exception:
                pass

    def add_todo(self, msg_id: int) -> bool:
        with self._lock:
            if not self._db.execute("SELECT 1 FROM messages WHERE msg_id = ?", (msg_id,)).fetchone():
                return False
            self._todos[msg_id] = "todo"
            self._save_todos()
        self._fire_todo(msg_id, "todo")
        return True

    def complete_todo(self, msg_id: int) -> bool:
        with self._lock:
            if msg_id not in self._todos:
                return False
            self._todos[msg_id] = "done"
            self._save_todos()
        self._fire_todo(msg_id, "done")
        return True

    def reopen_todo(self, msg_id: int) -> bool:
        with self._lock:
            if msg_id not in self._todos:
                return False
            self._todos[msg_id] = "todo"
            self._save_todos()
        self._fire_todo(msg_id, "todo")
        return True

    def remove_todo(self, msg_id: int) -> bool:
        with self._lock:
            if msg_id not in self._todos:
                return False
            del self._todos[msg_id]
            self._save_todos()
        self._fire_todo(msg_id, None)
        return True

    def get_todo_status(self, msg_id: int) -> str | None:
        return self._todos.get(msg_id)

    def get_todos(self) -> dict[int, str]:
        """Returns {msg_id: status} for all todos."""
        return dict(self._todos)

    TODO_SUMMARY_TEXT_MAX = 500

    @classmethod
    def _todo_summary(cls, msg: dict, status: str) -> dict:
        """Just enough of a pinned message to render it without the message in the DOM."""
        text = msg.get("text") or ""
        if len(text) > cls.TODO_SUMMARY_TEXT_MAX:
            text = text[:cls.TODO_SUMMARY_TEXT_MAX].rstrip() + "..."
        return {
            "id": msg["id"],
            "status": status,
            "sender": msg.get("sender", ""),
            "text": text,
            "time": msg.get("time", ""),
            "timestamp": msg.get("timestamp", 0),
            "channel": msg.get("channel", "general"),
        }

    def get_todo_summary(self, msg_id: int) -> dict | None:
        """Summary of one pinned message, or None if it isn't pinned or no longer exists."""
        with self._lock:
            status = self._todos.get(msg_id)
            if status is None:
                return None
            _, msg = self._message_row(msg_id)
            return self._todo_summary(msg, status) if msg is not None else None

    def _todo_message_rows(self, status=None):
        rows = []
        for msg_id, todo_status in self._todos.items():
            if not status or status == todo_status:
                position, msg = self._message_row(msg_id)
                if msg is not None:
                    rows.append((position, msg, todo_status))
        return sorted(rows, key=lambda row: row[0])

    def get_todo_summaries(self) -> dict[int, dict]:
        """Return summaries of pinned messages that still exist."""
        with self._lock:
            return {msg["id"]: self._todo_summary(msg, status) for _, msg, status in self._todo_message_rows()}

    def get_todo_messages(self, status: str | None = None) -> list[dict]:
        """Get todo messages, optionally filtered by status."""
        with self._lock:
            return [msg for _, msg, _ in self._todo_message_rows(status)]

    @property
    def last_id(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT msg_id FROM messages ORDER BY position DESC LIMIT 1").fetchone()
            return row[0] if row else -1
