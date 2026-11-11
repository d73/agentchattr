"""Live-screen classifiers: busy markers and needs-input prompts."""
import unittest

from screen_state import has_busy_marker, needs_user_input


class BusyTests(unittest.TestCase):
    def test_live_status_lines(self):
        for line in ("✻ Thinking… (12s · esc to interrupt)",
                     "• Working (1m 3s • esc to interrupt)",
                     "⠋ Thinking (esc to cancel, 5s)"):
            self.assertTrue(has_busy_marker(["old", line, "> "]), line)

    def test_quoted_history_is_not_busy(self):
        history = ["I added esc to interrupt handling", "The footer says esc to interrupt."]
        self.assertFalse(has_busy_marker(history + ["", "› Ask Codex"]))

    def test_marker_scrolled_out_of_live_area_is_not_busy(self):
        rows = ["(3s · esc to interrupt)"] + [f"line {i}" for i in range(30)]
        self.assertFalse(has_busy_marker(rows))


class NeedsInputTests(unittest.TestCase):
    def test_permission_prompts(self):
        for text in ("Do you want to proceed?\n❯ 1. Yes\n  2. No",
                     "Would you like to run the following command?",
                     "Apply changes? [y/n]",
                     "Press enter to confirm or esc to cancel"):
            self.assertTrue(needs_user_input(text), text)

    def test_normal_screen_is_not_input(self):
        self.assertFalse(needs_user_input("Done.\n\n› Ask Codex to do anything"))

    def test_old_prompt_scrolled_away_is_ignored(self):
        text = "Do you want to proceed?\n" + "\n".join(f"line {i}" for i in range(40))
        self.assertFalse(needs_user_input(text))


if __name__ == "__main__":
    unittest.main()
