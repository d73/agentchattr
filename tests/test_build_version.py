import re
import subprocess
import unittest
from unittest.mock import patch


class BuildVersionTests(unittest.TestCase):
    def test_build_version_is_local_datetime(self):
        import app
        self.assertRegex(app.BUILD_VERSION, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_build_endpoint(self):
        import asyncio
        import app
        self.assertEqual(asyncio.run(app.get_build()), {"build": app.BUILD_VERSION})

    def test_git_version_probes_suppress_console_windows(self):
        import app

        with patch.object(subprocess, "run") as run:
            run.return_value.stdout = "1700000000\n"
            self.assertRegex(app._compute_build_version(), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
            run.return_value.stdout = "https://github.com/bcurts/agentchattr.git\n"
            self.assertEqual(app._detect_install_kind(), "official_git")

        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["git", "log", "-1", "--format=%ct"],
            ["git", "remote", "get-url", "origin"],
        ])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))


if __name__ == "__main__":
    unittest.main()
