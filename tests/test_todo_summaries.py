import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from store import MessageStore


class TodoSummaryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = MessageStore(str(Path(self._tmp.name) / "log.jsonl"))
        self.a = self.store.add("derek", "first", channel="stocks")["id"]
        self.b = self.store.add("claude", "x" * 900, channel="general")["id"]
        self.c = self.store.add("codex", "unpinned")["id"]

    def test_summaries_cover_pinned_messages_only(self):
        self.store.add_todo(self.a)
        self.store.add_todo(self.b)
        self.store.complete_todo(self.b)
        s = self.store.get_todo_summaries()
        self.assertEqual(set(s), {self.a, self.b})
        self.assertEqual(s[self.a]["status"], "todo")
        self.assertEqual(s[self.a]["sender"], "derek")
        self.assertEqual(s[self.a]["channel"], "stocks")
        self.assertEqual(s[self.b]["status"], "done")
        self.assertLessEqual(len(s[self.b]["text"]), MessageStore.TODO_SUMMARY_TEXT_MAX + 3)
        self.assertTrue(s[self.b]["text"].endswith("..."))

    def test_single_summary_and_unpinned_or_missing(self):
        self.store.add_todo(self.a)
        self.assertEqual(self.store.get_todo_summary(self.a)["text"], "first")
        self.assertIsNone(self.store.get_todo_summary(self.c))
        self.assertIsNone(self.store.get_todo_summary(9999))

    def test_legacy_status_map_is_unchanged(self):
        self.store.add_todo(self.a)
        self.assertEqual(self.store.get_todos(), {self.a: "todo"})


if __name__ == "__main__":
    unittest.main()
