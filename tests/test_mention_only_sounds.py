import unittest
from pathlib import Path

STATIC = Path(__file__).parent.parent / "static"


class MentionOnlySoundTests(unittest.TestCase):
    def test_every_sound_call_site_is_gated_on_a_mention(self):
        chat = (STATIC / "chat.js").read_text(encoding="utf-8")
        jobs = (STATIC / "jobs.js").read_text(encoding="utf-8")
        self.assertIn("function mentionsUser(", chat)
        self.assertIn("window.mentionsUser = mentionsUser;", chat)
        # channel message sound, background-channel sound
        self.assertIn("soundEnabled && mentionsUser(event.data.text)", chat)
        self.assertIn("mentionsUser(msg.text)) {\n                playCrossChannelSound", chat.replace("\r\n", "\n"))
        # job-thread sounds
        self.assertEqual(jobs.count("window.mentionsUser(data.message.text)"), 2)


if __name__ == "__main__":
    unittest.main()
