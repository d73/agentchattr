import unittest
from pathlib import Path

STATIC = Path(__file__).parent.parent / "static"


class HumanSettingsStaticTests(unittest.TestCase):
    def test_color_field_in_settings(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="setting-user-color"', html)
        self.assertIn('id="setting-user-color-reset"', html)

    def test_you_sound_row_and_color_wiring(self):
        js = (STATIC / "chat.js").read_text(encoding="utf-8")
        self.assertIn("'cross-channel', 'you'", js)
        self.assertIn("setupUserColorSetting();", js)
        self.assertIn("playOwnSendSound();", js)
        self.assertIn("agentchattr-user-color", js)


if __name__ == "__main__":
    unittest.main()
