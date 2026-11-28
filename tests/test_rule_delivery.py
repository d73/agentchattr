import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import wrapper


class StopWatcher(BaseException):
    pass


class RuleDeliveryTests(unittest.TestCase):
    def test_unconfirmed_delivery_keeps_rules_pending_until_success(self):
        for failure in (False, OSError("prompt delivery failed")):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                queue = Path(directory) / "worker_queue.jsonl"
                wake = '{"channel":"ops"}\n'
                queue.write_text(wake, encoding="utf-8")
                inject = Mock(side_effect=[failure, True, True])
                remaining_wakes = iter((wake, wake))

                def next_wake(seconds):
                    if seconds != 1:
                        return
                    pending_wake = next(remaining_wakes, None)
                    if pending_wake is None:
                        raise StopWatcher()
                    queue.write_text(pending_wake, encoding="utf-8")

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
