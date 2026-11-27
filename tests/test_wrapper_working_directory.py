"""A per-launch directory should reach the agent without becoming a provider argument."""

import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import wrapper


class WorkingDirectoryTests(unittest.TestCase):
    def test_launch_directory_preserves_default_and_consumes_override(self):
        config = {"agents": {"codex": {"cwd": "..", "mcp_inject": "flag"}}}
        provider = "wrapper_windows" if sys.platform == "win32" else "wrapper_unix"
        runner = SimpleNamespace(run_agent=Mock(), get_activity_checker=Mock())
        selected = str(Path(__file__).parent.resolve())
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for flags, expected in (([], ".."), (["--working-directory", selected], selected)):
            with self.subTest(flags=flags), ExitStack() as stack:
                stack.enter_context(patch.dict("os.environ", {}, clear=True))
                stack.enter_context(patch.dict("sys.modules", {provider: runner}))
                stack.enter_context(patch.object(sys, "argv", ["wrapper.py", "codex", *flags, "--", "--provider-flag"]))
                stack.enter_context(patch.object(wrapper, "ROOT", root))
                stack.enter_context(patch("config_loader.load_config", return_value=config))
                stack.enter_context(patch.object(wrapper, "_register_at_startup", return_value={"name": "c1", "token": "test"}))
                stack.enter_context(patch.object(wrapper.threading, "Thread"))
                stack.enter_context(patch.object(wrapper.shutil, "which", return_value="codex"))
                stack.enter_context(patch("urllib.request.urlopen"))
                launch = stack.enter_context(patch.object(wrapper, "_build_provider_launch", return_value=([], {}, {}, None)))
                wrapper.main()
                self.assertEqual(launch.call_args.kwargs["extra_args"], ["--provider-flag"])
                self.assertEqual(runner.run_agent.call_args.kwargs["cwd"], expected)
