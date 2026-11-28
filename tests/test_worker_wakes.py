from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from agents import AgentTrigger, take_trigger
import wrapper


class StopWatcher(BaseException):
    pass


class WorkerWakeTests(unittest.TestCase):
    def setUp(self):
        self.data_dir = Path(self.enterContext(TemporaryDirectory()))
        self.queue_file = self.data_dir / "worker_queue.jsonl"

    def test_distinct_job_and_channel_wakes_are_injected_separately(self):
        registry = Mock()
        registry.get_instance.return_value = {"identity_id": "fixture-identity"}
        sender = AgentTrigger(registry, str(self.data_dir))
        sender.trigger_sync("worker", channel="general", job_id=11)
        sender.trigger_sync("worker", channel="general", job_id=12)
        prompts = []

        def inject(prompt):
            prompts.append(prompt)
            if len(prompts) == 1:
                sender.trigger_sync("worker", channel="other")
            if len(prompts) == 3:
                raise StopWatcher()

        clock = Mock()
        clock.sleep.side_effect = [None] * 6 + [StopWatcher()]
        with patch.object(wrapper, "time", clock), \
             patch.object(wrapper, "_fetch_role", return_value=""), \
             patch.object(wrapper, "_fetch_active_rules", return_value=None), \
             self.assertRaises(StopWatcher):
            wrapper._queue_watcher(lambda: ("worker", self.queue_file), inject,
                                   get_identity_id_fn=lambda: "fixture-identity")

        self.assertEqual(3, len(prompts))
        self.assertIn("job_id=11", prompts[0])
        self.assertIn("job_id=12", prompts[1])
        self.assertIn("#other", prompts[2])
        self.assertIsNone(take_trigger(self.queue_file, "fixture-identity"))

    def test_legacy_append_during_drain_is_preserved(self):
        self.queue_file.write_text('{"job_id": 11}\n{"job_id": 12}\n', encoding="utf-8")
        rename = type(self.queue_file).rename

        def append_after_snapshot(path, target):
            result = rename(path, target)
            path.write_text('{"job_id": 13}\n', encoding="utf-8")
            return result

        with patch.object(type(self.queue_file), "rename", append_after_snapshot):
            self.assertEqual(11, take_trigger(self.queue_file)["job_id"])
        self.assertEqual(12, take_trigger(self.queue_file)["job_id"])
        self.assertEqual(13, take_trigger(self.queue_file)["job_id"])
        self.assertIsNone(take_trigger(self.queue_file))
