"""Run the chat reply identity checks when Node.js is available."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
class ReplyIdentityScriptTests(unittest.TestCase):
    def test_reply_identity(self):
        script = Path(__file__).with_name("reply_identity_smoke.js")
        result = subprocess.run(
            ["node", str(script)], capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
