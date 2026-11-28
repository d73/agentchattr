import io
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, call, patch

from restart_policy import RestartPolicy


class RestartPolicyTests(unittest.TestCase):
    def test_long_run_resets_rapid_exit_backoff(self):
        policy = RestartPolicy()
        wait = Mock()
        with patch("restart_policy.time.monotonic", side_effect=[1, 2, 15, 1]), redirect_stdout(io.StringIO()):
            for _ in range(4):
                policy.wait_before_restart(0, "Worker exited.", wait)
        self.assertEqual(wait.call_args_list, [call(3), call(6), call(3), call(3)])

    @unittest.skipUnless(sys.platform == "win32", "Windows console wrapper")
    def test_repeated_rapid_exits_stop_the_console_wrapper_with_a_reason(self):
        import wrapper_windows

        process = Mock(pid=1, returncode=2)
        output = io.StringIO()
        with (
            patch.object(wrapper_windows, "enable_vt_mode"),
            patch.object(wrapper_windows, "_vt_keepalive_thread"),
            patch.object(wrapper_windows.subprocess, "Popen", return_value=process) as launch,
            patch.object(wrapper_windows.time, "monotonic", return_value=0),
            patch.object(wrapper_windows.time, "sleep") as sleep,
            redirect_stdout(output),
        ):
            with self.assertRaises(SystemExit) as stopped:
                wrapper_windows.run_agent("worker", [], ".", {}, None, "worker", False, Mock())
        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(launch.call_count, 5)
        self.assertEqual(sleep.call_args_list, [call(3), call(6), call(12), call(24)])
        self.assertIn("exited (code 2)", output.getvalue())
        self.assertIn("Automatic restart stopped", output.getvalue())
