import json
import tempfile
import tracemalloc
import unittest
from pathlib import Path

from store import MessageStore


class HistoryMemoryTests(unittest.TestCase):
    def test_loading_and_paging_do_not_retain_all_history_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "messages.jsonl"
            payload_size = 8192
            message_count = 512
            with path.open("w", encoding="utf-8") as stream:
                for msg_id in range(message_count):
                    message = {"id": msg_id, "channel": "general", "text": f"{msg_id} " + "x" * payload_size}
                    stream.write(json.dumps(message) + "\n")

            tracemalloc.start()
            try:
                store = MessageStore(str(path))
                recent = store.get_recent(3, channel="general")
                unread = store.get_since(0, channel="general", limit=3)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

            self.assertEqual([msg["id"] for msg in recent], [509, 510, 511])
            self.assertEqual([msg["id"] for msg in unread], [1, 2, 3])
            self.assertLess(peak, message_count * payload_size // 2)
            earlier, more = store.get_before(509, 2, channel="general")
            self.assertEqual([msg["id"] for msg in earlier], [507, 508])
            self.assertTrue(more)

            store.update_message(0, {"text": "edited after startup"})
            restored = MessageStore(str(path))
            self.assertEqual(restored.get_by_id(0)["text"], "edited after startup")
            self.assertEqual(restored.count_channel("general"), message_count)
