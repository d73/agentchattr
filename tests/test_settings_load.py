import asyncio
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from fastapi import WebSocketDisconnect

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app
from store import MessageStore


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

    def test_chat_font_size_is_restored_or_defaults(self):
        for stored, expected in (
            ({}, 14),
            ({"font_size": 18}, 18),
            ({"font_size": 33}, 14),
            ({"font_size": "18"}, 14),
            ({"font_size": True}, 14),
        ):
            with self.subTest(stored=stored):
                app.room_settings.pop("font_size", None)
                self.path.write_text(json.dumps(stored), "utf-8")
                app._load_settings()
                self.assertEqual(app.room_settings["font_size"], expected)

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

    def test_attention_modes_survive_save_and_reload(self):
        for mode in ("highlight", "full-highlight", "flash"):
            with self.subTest(mode=mode):
                prefs = {"attention_style": mode, "channel_attention": "off"}
                app.room_settings["ui_prefs"] = app._clean_ui_prefs(prefs)
                app._save_settings()
                app.room_settings.pop("ui_prefs")
                app._load_settings()
                self.assertEqual(app.room_settings["ui_prefs"], prefs)

    def test_per_agent_updates_keep_other_changes_and_ignore_stale_seed(self):
        app.room_settings["agent_colors"] = {"claude": "#333333", "codex": "#222222"}
        colors = app._save_agent_color_changes(
            {"codex": "#444444", "invalid": "red"}, {"claude": "#111111"},
        )
        self.assertEqual(colors, {"claude": "#333333", "codex": "#444444"})
        self.assertEqual(app._save_agent_color_changes({"codex": None}, None), {"claude": "#333333"})
        self.assertEqual(json.loads(self.path.read_text("utf-8"))["agent_colors"], {"claude": "#333333"})

    def test_uninitialized_colors_seed_once_and_apply_pending_reset(self):
        app.room_settings.pop("agent_colors", None)
        self.assertEqual(app._save_agent_color_changes(
            {"codex": None}, {"claude": "#333333", "codex": "#222222"},
        ), {"claude": "#333333"})

    def test_failed_color_write_keeps_previous_settings(self):
        previous = {"codex": "#222222"}
        app.room_settings["agent_colors"] = previous
        with mock.patch.object(app, "_save_settings", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                app._save_agent_color_changes({"codex": "#444444"}, None)
        self.assertIs(app.room_settings["agent_colors"], previous)

    def test_color_delta_is_acknowledged_and_broadcast_over_the_socket(self):
        app.room_settings.clear()
        app.room_settings.update(channels=["general"], agent_colors={"claude": "#333333"})
        frames = []
        incoming = iter([{"type": "agent_color_changes", "request_id": 1,
                          "changes": {"codex": "#444444"}}])

        class Socket:
            query_params = {"token": "test"}
            client = SimpleNamespace(host="127.0.0.1")

            async def accept(self):
                pass

            async def receive_text(self):
                event = next(incoming, None)
                if event is None:
                    raise WebSocketDisconnect()
                return json.dumps(event)

            async def send_text(self, data):
                frames.append(json.loads(data))

        empty = mock.Mock()
        empty.channel_activity.return_value = {}
        empty.list_all.return_value = []
        agents = mock.Mock()
        agents.get_status.return_value = {}
        router = mock.Mock()
        router.is_paused.return_value = False
        with mock.patch.multiple(
            app, session_token="test", ws_clients=set(), registry=None, config={},
            store=MessageStore(str(self.path.with_name("messages.jsonl"))),
            rules=empty, jobs=empty, schedules=empty, agents=agents, router=router,
        ):
            asyncio.run(app.websocket_endpoint(Socket()))

        expected = {"claude": "#333333", "codex": "#444444"}
        self.assertIn({"type": "agent_colors_saved", "request_id": 1, "data": expected, "revision": 1}, frames)
        latest_settings = [f for f in frames if f["type"] == "settings"][-1]["data"]
        self.assertEqual(latest_settings["agent_colors"], expected)
        self.assertEqual(latest_settings["agent_colors_revision"], 1)


if __name__ == "__main__":
    unittest.main()
