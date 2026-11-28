import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from schedules import ScheduleStore

import idle_shutdown
from idle_shutdown import IdleShutdown


class IdleShutdownTests(unittest.TestCase):
    def test_never_fires_before_a_browser_has_connected(self):
        idle = IdleShutdown(30)
        self.assertFalse(idle.check(browsers=0, agents=0, now=0))
        self.assertFalse(idle.check(browsers=0, agents=0, now=10_000))

    def test_fires_after_grace_once_browser_closed_and_no_agents(self):
        idle = IdleShutdown(30)
        idle.check(browsers=1, agents=0, now=0)
        self.assertFalse(idle.check(browsers=0, agents=0, now=100))
        self.assertFalse(idle.check(browsers=0, agents=0, now=129))
        self.assertTrue(idle.check(browsers=0, agents=0, now=130))

    def test_connected_agent_keeps_server_up(self):
        idle = IdleShutdown(30)
        idle.check(browsers=1, agents=1, now=0)
        self.assertFalse(idle.check(browsers=0, agents=1, now=1_000))
        # Agent leaves later: grace starts from then.
        self.assertFalse(idle.check(browsers=0, agents=0, now=1_010))
        self.assertTrue(idle.check(browsers=0, agents=0, now=1_040))

    def test_active_one_shot_holds_server_until_it_fires(self):
        idle = IdleShutdown(30)
        with tempfile.TemporaryDirectory() as folder:
            schedules = ScheduleStore(str(Path(folder) / "schedules.json"))
            with patch("schedules.time.time", return_value=100):
                item = schedules.create("ping", ["codex"], one_shot=True, send_at=200)

            idle.check(browsers=1, agents=0, now=100)
            self.assertFalse(idle.check(0, 0, 110, scheduled_work=schedules.has_active()))
            self.assertFalse(idle.check(0, 0, 141, scheduled_work=schedules.has_active()))
            with patch("schedules.time.time", return_value=200):
                self.assertEqual(schedules.run_due()[0]["id"], item["id"])
                schedules.delete(item["id"])

            self.assertFalse(idle.check(0, 0, 200, scheduled_work=schedules.has_active()))
            self.assertTrue(idle.check(0, 0, 230, scheduled_work=schedules.has_active()))

    def test_refresh_within_grace_resets_timer(self):
        idle = IdleShutdown(30)
        idle.check(browsers=1, agents=0, now=0)
        idle.check(browsers=0, agents=0, now=10)
        idle.check(browsers=1, agents=0, now=15)
        self.assertFalse(idle.check(browsers=0, agents=0, now=40))
        self.assertTrue(idle.check(browsers=0, agents=0, now=70))

    def test_config_toggle_and_grace(self):
        self.assertIsNone(idle_shutdown.from_config({"server": {"auto_shutdown": False}}))
        self.assertEqual(idle_shutdown.from_config({}).grace_seconds, 30)
        self.assertEqual(
            idle_shutdown.from_config({"server": {"idle_shutdown_seconds": 5}}).grace_seconds, 5)


if __name__ == "__main__":
    unittest.main()
