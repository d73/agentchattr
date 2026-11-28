import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from router import Router
import app


class RoutingRefreshTests(unittest.TestCase):
    def test_older_registry_callback_cannot_overwrite_newer_mention_set(self):
        router = Router(["codex", "c1"])
        old_snapshot = threading.Event()
        continue_old = threading.Event()
        new_started = threading.Event()
        new_finished = threading.Event()
        registry = Mock()
        registry.get_bases.return_value = {"codex": {}}

        def active_names():
            if threading.current_thread() is old_refresh:
                old_snapshot.set()
                continue_old.wait(5)
                return []
            return ["c2"]

        def refresh_new():
            new_started.set()
            app._on_registry_change()
            new_finished.set()

        registry.get_active_names.side_effect = active_names
        old_refresh = threading.Thread(target=app._on_registry_change)
        new_refresh = threading.Thread(target=refresh_new)
        with patch.multiple(app, registry=registry, router=router, _event_loop=None):
            old_refresh.start()
            try:
                self.assertTrue(old_snapshot.wait(2))
                new_refresh.start()
                self.assertTrue(new_started.wait(2))
                self.assertFalse(new_finished.wait(0.2))
            finally:
                continue_old.set()
                old_refresh.join(5)
                if new_refresh.ident is not None:
                    new_refresh.join(5)

        self.assertTrue(new_finished.is_set())
        self.assertEqual(router.parse_mentions("@c2"), ["c2"])
        self.assertEqual(router.parse_mentions("@c1"), [])


class RouterMentionTests(unittest.TestCase):
    def test_one_message_uses_the_same_roster_for_explicit_and_all_mentions(self):
        def online_agents():
            router.update_agents(["new"])
            return {"old", "new"}

        router = Router(["old"], online_checker=online_agents)
        self.assertEqual(router.parse_mentions("@old @all"), ["old"])
        self.assertEqual(router.parse_mentions("@new @all"), ["new"])

    def test_summary_requires_explicit_target_even_when_default_is_all(self):
        router = Router(["codex", "claude", "agy"], default_mention="all",
                        online_checker=lambda: {"codex", "agy"})
        for command in ("/summary", "/summarise", "/summary please", "/summary @unknown"):
            with self.subTest(command=command):
                self.assertEqual(router.get_targets(command, sender_is_agent=False), [])
        self.assertEqual(set(router.get_targets("/summary @all", sender_is_agent=False)), {"codex", "agy"})
        self.assertEqual(router.get_targets("/summary @codex", sender_is_agent=False), ["codex"])

    def test_summary_targets_only_named_agent_even_when_default_is_all(self):
        router = Router(["agy", "c1", "c2"], default_mention="all")
        self.assertEqual(router.get_targets("/summary @agy", sender_is_agent=False), ["agy"])
        self.assertEqual(router.get_targets("/summarise @missing", sender_is_agent=False), [])

    def test_code_mentions_do_not_route_from_agent_message(self):
        router = Router(["agy", "c1", "c2"], default_mention="all")
        self.assertEqual(router.get_targets("@derek Saved summary; `@all` was updated.", sender_is_agent=True), [])
        self.assertEqual(router.get_targets("```text\n@all\n``` @c1", sender_is_agent=True), ["c1"])

    def test_hyphenated_agent_name_is_parsed_as_full_mention(self):
        router = Router(["telegram-bridge"], default_mention="none")

        self.assertEqual(
            set(router.parse_mentions("please ask @telegram-bridge to check")),
            {"telegram-bridge"},
        )

    def test_shorter_agent_name_does_not_match_prefix_of_hyphenated_unknown(self):
        router = Router(["telegram"], default_mention="none")

        self.assertEqual(router.parse_mentions("@telegram-bridge check"), [])
        self.assertEqual(router.get_targets("@telegram-bridge check", sender_is_agent=False), [])

    def test_longest_hyphenated_name_wins_when_prefix_agent_also_exists(self):
        router = Router(["telegram", "telegram-bridge"], default_mention="none")

        self.assertEqual(
            set(router.parse_mentions("@telegram-bridge check")),
            {"telegram-bridge"},
        )

    def test_unknown_exact_handle_still_does_not_route(self):
        router = Router(["telegram-bridge"], default_mention="none")

        self.assertEqual(router.parse_mentions("@telegram-bot check"), [])
        self.assertEqual(router.get_targets("@telegram-bot check", sender_is_agent=False), [])


if __name__ == "__main__":
    unittest.main()
