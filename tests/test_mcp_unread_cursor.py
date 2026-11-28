import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import mcp_bridge as bridge
from store import MessageStore


class UnreadCursorTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.store = MessageStore(str(Path(directory) / "messages.jsonl"))
        self.enterContext(patch.multiple(
            bridge, store=self.store, registry=None, _cursors={}, _presence={},
            _empty_read_count={}, _CURSORS_FILE=None, _last_read_channel={}, _last_read_job_id={},
        ))

    def add(self, count):
        return [self.store.add("human", str(index))["id"] for index in range(count)]

    def read(self, **kwargs):
        return [item["id"] for item in json.loads(bridge.chat_read("reader", channel="general", **kwargs))]

    def test_pages_unread_messages_from_oldest_even_when_first_cursor_is_zero(self):
        first, = self.add(1)
        self.assertEqual([first], self.read(limit=1))
        unread = self.add(3)
        self.assertEqual(unread[:1], self.read(limit=1))
        self.assertEqual(unread[1:], self.read(limit=2))
        self.assertTrue(bridge.chat_read("reader", channel="general").startswith("No new messages"))

    def test_sends_and_job_proposals_leave_unread_messages_pending(self):
        self.add(2)
        self.read(limit=1)
        unread = self.add(2)
        self.assertTrue(bridge.chat_send("reader", "reply", channel="general").startswith("Sent"))
        self.assertTrue(bridge.chat_propose_job("reader", "Job", channel="general").startswith("Proposed"))
        self.assertEqual(unread, self.read(limit=2))

    def test_nonzero_explicit_paging_does_not_move_live_cursor(self):
        _, second, third = self.add(3)
        self.assertEqual([third], self.read(limit=1))
        unread = self.add(3)
        self.assertEqual([third], self.read(since_id=second, limit=1))
        self.assertEqual(unread[-1:], self.read(since_id=unread[-2], limit=1))
        self.assertEqual(unread, self.read())

    def test_explicit_zero_uses_and_advances_the_default_cursor(self):
        initial = self.add(3)
        self.assertEqual(initial[-1:], self.read(since_id=0, limit=1))
        unread = self.add(2)
        self.assertEqual(unread[:1], self.read(since_id=0, limit=1))
        self.assertEqual(unread[1:], self.read(since_id=0, limit=1))

    def test_one_wake_can_drain_all_pages_without_guessing_when_to_read_again(self):
        self.add(2)
        initial = json.loads(bridge.chat_read("reader", channel="general", limit=1))
        self.assertNotIn("has_more", initial[-1])
        unread = self.add(4)
        with patch.object(self.store, "get_since", wraps=self.store.get_since) as get_since:
            first = json.loads(bridge.chat_read("reader", channel="general", limit=2))
        get_since.assert_called_once_with(initial[-1]["id"], channel="general", limit=3)
        self.assertTrue(first[-1]["has_more"])
        self.assertNotIn("has_more", first[0])
        last = json.loads(bridge.chat_read("reader", channel="general", limit=2))
        self.assertNotIn("has_more", last[-1])
        self.assertEqual(unread, [item["id"] for item in first + last])

    def test_first_read_and_resync_still_take_most_recent_context(self):
        initial = self.add(3)
        self.assertEqual(initial[-1:], self.read(limit=1))
        unread = self.add(3)
        resynced = json.loads(bridge.chat_resync("reader", limit=1, channel="general"))
        self.assertEqual(unread[-1:], [item["id"] for item in resynced])
        self.assertTrue(bridge.chat_read("reader", channel="general").startswith("No new messages"))
