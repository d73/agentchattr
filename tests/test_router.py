import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from router import Router


class RouterMentionTests(unittest.TestCase):
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
