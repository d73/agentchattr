import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import mcp_bridge


class CursorPersistenceTests(unittest.TestCase):
    def test_newer_cursor_cannot_commit_before_a_blocked_older_save(self):
        old_at_replace = threading.Event()
        release_old = threading.Event()
        new_at_save = threading.Event()
        new_at_replace = threading.Event()
        replace = os.replace
        save_cursors = mcp_bridge._save_cursors

        def save_with_signal():
            if old_at_replace.is_set():
                new_at_save.set()
            save_cursors()

        def replace_with_delay(source, destination):
            if not old_at_replace.is_set():
                old_at_replace.set()
                self.assertTrue(release_old.wait(5))
            else:
                new_at_replace.set()
            return replace(source, destination)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mcp_cursors.json"
            with (
                patch.object(mcp_bridge, "_CURSORS_FILE", path),
                patch.object(mcp_bridge, "_cursors", {}),
                patch.object(mcp_bridge, "_save_cursors", side_effect=save_with_signal),
                patch.object(os, "replace", side_effect=replace_with_delay),
                ThreadPoolExecutor(max_workers=2) as workers,
            ):
                first = workers.submit(mcp_bridge._update_cursor, "c1", [{"id": 1}], "general")
                try:
                    self.assertTrue(old_at_replace.wait(5))
                    second = workers.submit(mcp_bridge._update_cursor, "c2", [{"id": 2}], "general")
                    self.assertTrue(new_at_save.wait(5))
                    self.assertFalse(new_at_replace.wait(0.2))
                finally:
                    release_old.set()
                first.result(timeout=5)
                second.result(timeout=5)
                self.assertTrue(new_at_replace.is_set())
                self.assertEqual(json.loads(path.read_text()), {"c1": {"general": 1}, "c2": {"general": 2}})
