import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from store import MessageStore


class HistoryEarlierTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = MessageStore(str(Path(self._tmp.name) / "log.jsonl"))
        self.ids = []
        for i in range(10):
            channel = "a" if i % 2 == 0 else "b"
            self.ids.append(self.store.add("u", f"m{i}", channel=channel)["id"])

    def test_pages_back_within_a_channel_oldest_first(self):
        a_ids = self.ids[0::2]  # five messages in channel a
        older, more = self.store.get_before(a_ids[-1] + 1, 2, channel="a")
        self.assertEqual([m["id"] for m in older], a_ids[-2:])
        self.assertTrue(more)
        older, more = self.store.get_before(a_ids[-2], 5, channel="a")
        self.assertEqual([m["id"] for m in older], a_ids[:-2])
        self.assertFalse(more)

    def test_nothing_before_the_first_message(self):
        older, more = self.store.get_before(self.ids[0], 50, channel="a")
        self.assertEqual((older, more), ([], False))

    def test_count_channel(self):
        self.assertEqual(self.store.count_channel("a"), 5)
        self.assertEqual(self.store.count_channel("missing"), 0)


if __name__ == "__main__":
    unittest.main()
