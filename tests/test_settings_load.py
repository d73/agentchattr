import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app


class SettingsLoadTests(unittest.TestCase):
    def setUp(self):
        self._saved = copy.deepcopy(app.room_settings)
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "settings.json"
        patcher = mock.patch.object(app, "_settings_path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        app.room_settings.clear()
        app.room_settings.update(self._saved)
        self._tmp.cleanup()

    def corrupt_copies(self):
        return sorted(p.name for p in Path(self._tmp.name).glob("settings.json.corrupt-*"))

    def test_corrupt_file_is_kept_and_defaults_are_used(self):
        self.path.write_text('{"title": "mine", "channels": ["general", "x"', "utf-8")
        with self.assertLogs(app.log, level="WARNING"):
            app._load_settings()
        self.assertEqual(len(self.corrupt_copies()), 1)
        self.assertEqual(self.path.read_text("utf-8"), '{"title": "mine", "channels": ["general", "x"')
        self.assertEqual(app.room_settings["channels"][0], "general")

    def test_non_object_json_is_quarantined(self):
        self.path.write_text("[1, 2]", "utf-8")
        with self.assertLogs(app.log, level="WARNING"):
            app._load_settings()
        self.assertEqual(len(self.corrupt_copies()), 1)

    def test_valid_file_is_loaded_without_a_backup(self):
        self.path.write_text(json.dumps({"title": "mine", "channels": ["general", "b"]}), "utf-8")
        app._load_settings()
        self.assertEqual(app.room_settings["title"], "mine")
        self.assertEqual(self.corrupt_copies(), [])

    def test_synced_options_are_validated_on_load(self):
        self.path.write_text(json.dumps({
            "agent_colors": {"claude": "#ff0000", "bad": "red", "worse": 5},
            "ui_prefs": {"attention_style": "nope", "sidebar_mode": "sidebar", "user_color": "#12345"},
        }), "utf-8")
        app._load_settings()
        self.assertEqual(app.room_settings["agent_colors"], {"claude": "#ff0000"})
        self.assertEqual(app.room_settings["ui_prefs"], {"sidebar_mode": "sidebar"})

    def test_wrong_typed_synced_options_return_to_uninitialized(self):
        self.path.write_text(json.dumps({"agent_colors": "x", "ui_prefs": []}), "utf-8")
        app._load_settings()
        self.assertNotIn("agent_colors", app.room_settings)
        self.assertNotIn("ui_prefs", app.room_settings)


if __name__ == "__main__":
    unittest.main()
