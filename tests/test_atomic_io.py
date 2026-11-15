import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import atomic_io
from atomic_io import atomic_write_text


class AtomicWriteTests(unittest.TestCase):
    def test_writes_and_replaces_without_leftovers(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "settings.json"
            atomic_write_text(p, "one")
            atomic_write_text(p, "two\n")
            self.assertEqual(p.read_text("utf-8"), "two\n")
            self.assertEqual([x.name for x in Path(d).iterdir()], ["settings.json"])

    def test_failed_write_keeps_previous_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "settings.json"
            atomic_write_text(p, "good")
            with mock.patch.object(atomic_io.os, "replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    atomic_write_text(p, "bad")
            self.assertEqual(p.read_text("utf-8"), "good")
            self.assertEqual([x.name for x in Path(d).iterdir()], ["settings.json"])

    def test_retries_transient_permission_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            real = os.replace
            calls = {"n": 0}

            def flaky(src, dst):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise PermissionError("busy")
                return real(src, dst)

            with mock.patch.object(atomic_io.os, "replace", side_effect=flaky):
                atomic_write_text(p, "ok")
            self.assertEqual(p.read_text("utf-8"), "ok")
            self.assertEqual(calls["n"], 3)


if __name__ == "__main__":
    unittest.main()
