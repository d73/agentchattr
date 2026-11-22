import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobs import JobStore
from rules import RuleStore
from schedules import ScheduleStore
from session_store import SessionStore
from store import MessageStore


class ErrorHandlingTests(unittest.TestCase):
    def test_invalid_persisted_data_fails_with_path_without_overwriting(self):
        for cls in (JobStore, RuleStore, ScheduleStore, SessionStore, MessageStore):
            with self.subTest(store=cls.__name__), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'data.json'
                path.write_text('{invalid', encoding='utf-8')
                with self.assertRaises(ValueError) as caught:
                    cls(str(path))
                self.assertIn(str(path.resolve()), str(caught.exception))
                self.assertEqual(path.read_text(encoding='utf-8'), '{invalid')

    def test_bad_id_marker_includes_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.jsonl'
            marker = path.with_name(path.name + '.next-id')
            marker.write_text('broken', encoding='utf-8')
            with self.assertRaises(ValueError) as caught:
                MessageStore(str(path))
            self.assertIn(str(marker.resolve()), str(caught.exception))

    def test_failed_job_saves_roll_back_in_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'jobs.json'
            jobs = JobStore(str(path))
            with patch('jobs.atomic_write_text', side_effect=PermissionError(13, 'denied')):
                with self.assertRaises(OSError) as caught:
                    jobs.create('a', 'job', 'general', 'user')
                self.assertEqual(caught.exception.filename, str(path.resolve()))
                self.assertEqual(jobs.list_all(), [])
            job = jobs.create('a', 'job', 'general', 'user')
            with patch('jobs.atomic_write_text', side_effect=PermissionError(13, 'denied')):
                with self.assertRaises(OSError):
                    jobs.add_message(job['id'], 'user', 'hello')
            self.assertEqual(jobs.get(job['id'])['messages'], [])


if __name__ == '__main__':
    unittest.main()

