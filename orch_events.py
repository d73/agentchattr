"""Scoped wake hints and durable local-browser reset receipts for Orch."""

import asyncio
import re
import sqlite3
import threading
from collections import deque
from contextlib import asynccontextmanager, closing

from jobs import orch_coordinator
from router import without_code


RESET_PATH = "/api/plugins/orch/reset-commands"
RESET_PAGE_SIZE = 128
QUEUE_LIMIT = 64
SUBSCRIBER_LIMIT = 8


def forwarded(headers):
    return any(name == "forwarded" or name.startswith("x-forwarded-") for name in headers)


class Subscription:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.ready = asyncio.Event()
        self.pending = deque()
        self.overflowed = False

    def offer(self, hint):
        if self.overflowed:
            return
        empty = not self.pending
        if len(self.pending) == QUEUE_LIMIT:
            self.pending.clear()
            self.pending.append({"kind": "resync_required"})
            self.overflowed = True
        else:
            self.pending.append(hint)
        if empty:
            self.loop.call_soon_threadsafe(self.ready.set)


class OrchEvents:
    def __init__(self, channel, coordinator, jobs, receipt_path):
        self.channel = channel
        self.coordinator = coordinator
        self.jobs = jobs
        self.receipt_path = receipt_path
        self.lock = threading.Lock()
        self.subscribers = set()
        self.visible_jobs = set()
        self.roster = None
        self.mention = re.compile(rf"(?<![\w@])@{re.escape(coordinator)}(?![\w-]|\.[\w-])", re.IGNORECASE)

    def owns(self, job):
        if not job or job.get("type") != "job":
            return False
        if job["lifecycle"]["managed_by"] == "orch":
            bound = orch_coordinator(job)
            if bound is None:
                return True
            return bound.casefold() == self.coordinator.casefold()
        return (job["channel"] == self.channel
                and (job.get("assignee") or "").casefold() == self.coordinator.casefold())

    def covers_channel(self, channel):
        return channel == self.channel or any(
            self.owns(job) and job["lifecycle"]["core_id"] != "core.closed"
            for job in self.jobs.list_all(channel=channel)
        )

    @asynccontextmanager
    async def subscribe(self, *, channel, coordinator):
        if (channel, coordinator) != (self.channel, self.coordinator):
            raise ValueError("Orch subscription is outside the configured scope")
        subscription = Subscription()
        with self.lock:
            if len(self.subscribers) >= SUBSCRIBER_LIMIT:
                raise RuntimeError("Orch subscription capacity reached")
            if not self.subscribers:
                self.visible_jobs = {job["uid"] for job in self.jobs.list_all() if self.owns(job)}
            self.subscribers.add(subscription)
        try:
            yield self.hints(subscription)
        finally:
            with self.lock:
                self.subscribers.discard(subscription)

    async def hints(self, subscription):
        while True:
            await subscription.ready.wait()
            with self.lock:
                if not subscription.pending:
                    subscription.ready.clear()
                    continue
                hint = subscription.pending.popleft()
            yield hint
            if hint["kind"] == "resync_required":
                return

    def _publish_locked(self, hint):
        for subscription in tuple(self.subscribers):
            try:
                subscription.offer(hint)
            except RuntimeError:
                self.subscribers.discard(subscription)

    def publish(self, kind, **fields):
        with self.lock:
            self._publish_locked({"kind": kind, **fields})

    def job_changed(self, action, data):
        if not self.subscribers:
            return
        if action in ("message", "message_delete", "decision"):
            job = self.jobs.get(data["job_id"])
            if self.owns(job):
                kind = "decision" if action == "decision" else "job_message"
                self.publish(kind, channel=job["channel"], job_uid=job["uid"])
            return
        uid = data["uid"]
        current = self.jobs.get(data["id"]) if action != "delete" else None
        with self.lock:
            if self.owns(current):
                self.visible_jobs.add(uid)
                self._publish_locked({"kind": "job", "channel": current["channel"], "job_uid": uid})
            elif uid in self.visible_jobs:
                self.visible_jobs.discard(uid)
                self._publish_locked({"kind": "job"})

    def roster_changed(self, registered, status):
        if not self.subscribers:
            return
        roster = set()
        for name, item in registered.items():
            activity = status.get(name, {})
            if item["state"] != "active" or item["base"] != "codex" or not activity.get("available"):
                continue
            busy = True if activity.get("needs_input") else activity.get("busy")
            roster.add((name, item["identity_id"], busy))
        with self.lock:
            if roster != self.roster:
                self.roster = roster
                self._publish_locked({"kind": "roster"})

    def is_reset(self, text):
        return text == f"@{self.coordinator} /reset"

    def message_added(self, message):
        if (not self.subscribers or message.get("type") != "chat"
                or message.get("sender_kind") not in ("human", "agent")):
            return
        text = message["text"]
        if not self.is_reset(text) and self.mention.search(without_code(text)):
            channel = message["channel"]
            if self.covers_channel(channel):
                self.publish("mention", channel=channel, message_id=message["id"])

    def decision_resolved(self, message):
        if (message["channel"] == self.channel and message["sender"].casefold() == self.coordinator.casefold()
                and (message.get("metadata") or {}).get("resolved")):
            self.publish("decision", channel=self.channel, message_id=message["id"])

    def open_receipts(self):
        db = sqlite3.connect(self.receipt_path)
        db.execute("CREATE TABLE IF NOT EXISTS reset_receipts ("
                   "coordinator TEXT NOT NULL, message_id INTEGER NOT NULL, channel TEXT NOT NULL, "
                   "actor TEXT NOT NULL, PRIMARY KEY (coordinator, message_id))")
        return db

    def record_reset(self, message, actor):
        with closing(self.open_receipts()) as db, db:
            inserted = db.execute("INSERT OR IGNORE INTO reset_receipts VALUES (?, ?, ?, ?)",
                                  (self.coordinator, message["id"], message["channel"], actor)).rowcount
        if inserted:
            self.publish("mention", channel=message["channel"], message_id=message["id"])

    def reset_ids(self, after):
        if not self.receipt_path.exists():
            return []
        with closing(self.open_receipts()) as db:
            return [row[0] for row in db.execute(
                "SELECT message_id FROM reset_receipts WHERE coordinator = ? AND message_id > ? "
                "ORDER BY message_id LIMIT ?", (self.coordinator, after, RESET_PAGE_SIZE),
            )]
