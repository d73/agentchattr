import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agents import AgentTrigger
import wrapper


class StopWatcher(BaseException):
    pass


class RuleDeliveryTests(unittest.TestCase):
    def test_unconfirmed_delivery_keeps_rules_pending_until_success(self):
        for failure in (False, OSError("prompt delivery failed")):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                queue = Path(directory) / "worker_queue.jsonl"
                enqueue = AgentTrigger(None, directory)
                enqueue.trigger_sync("worker", channel="ops")
                inject = Mock(side_effect=[failure, True, True])
                remaining_wakes = iter(("ops", "ops"))

                def next_wake(seconds):
                    if seconds != 1:
                        return
                    channel = next(remaining_wakes, None)
                    if channel is None:
                        raise StopWatcher()
                    enqueue.trigger_sync("worker", channel=channel)

                with (
                    patch.object(wrapper, "_fetch_role", return_value=""),
                    patch.object(wrapper, "_fetch_active_rules", return_value={"epoch": 4, "rules": ["fixture rule"]}),
                    patch.object(wrapper, "_report_rule_sync") as report,
                    patch.object(wrapper.time, "sleep", side_effect=next_wake),
                    self.assertRaises(StopWatcher),
                ):
                    wrapper._queue_watcher(lambda: ("worker", queue), inject)

                prompts = [call.args[0] for call in inject.call_args_list]
                self.assertIn("fixture rule", prompts[0])
                self.assertIn("fixture rule", prompts[1])
                self.assertNotIn("fixture rule", prompts[2])
                report.assert_called_once_with(8300, "worker", 4, "")

    @unittest.skipUnless(sys.platform == "win32", "Windows console injector")
    def test_windows_injector_surfaces_an_incomplete_enter_write(self):
        import wrapper_windows

        def write_input(handle, records, count, written):
            if count > 1:
                written._obj.value = count
            return True

        with (
            patch.object(wrapper_windows, "_wait_for_input_drain"),
            patch.object(wrapper_windows.time, "sleep"),
            patch.object(wrapper_windows.kernel32, "GetStdHandle", return_value=1),
            patch.object(wrapper_windows.kernel32, "WriteConsoleInputW", side_effect=write_input),
            self.assertRaisesRegex(OSError, "Console Enter delivery incomplete"),
        ):
            wrapper_windows.inject("test prompt")
