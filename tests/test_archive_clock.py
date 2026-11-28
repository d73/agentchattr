from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import archive
from jobs import JobStore
from store import MessageStore


class ArchiveClockTests(unittest.TestCase):
    def test_only_clock_values_are_preserved_from_chat_and_job_archives(self):
        values = ["00:00:00", "23:59:59", "24:00:00", 123, "Before < noon & after"]
        recorded_at = time.mktime((2020, 9, 10, 7, 8, 9, 0, 0, -1))
        records = [
            {"uid": f"fixture-{index}", "text": f"Message {index}", "time": value}
            for index, value in enumerate(values)
        ]
        records[-1]["timestamp"] = recorded_at
        data = BytesIO()
        with ZipFile(data, "w") as output:
            output.writestr("manifest.json", json.dumps({"schema_version": archive.SCHEMA_VERSION}))
            output.writestr("messages.jsonl", "\n".join(map(json.dumps, records)))
            output.writestr("jobs.json", json.dumps([{"id": 1, "title": "Fixture", "messages": records}]))

        with TemporaryDirectory() as directory:
            root = Path(directory)
            messages = MessageStore(str(root / "messages.jsonl"))
            jobs = JobStore(str(root / "jobs.json"))
            strftime = time.strftime
            with patch("store.time.strftime", side_effect=lambda fmt, *args: strftime(fmt, *args) if args else "12:34:56"):
                result = archive.import_archive(data.getvalue(), messages, jobs, None, None, ["general"])

            self.assertTrue(result["ok"])
            expected = values[:2] + ["12:34:56", "12:34:56", "07:08:09"]
            self.assertEqual([record["time"] for record in messages.get_recent()], expected)
            self.assertEqual([record["time"] for record in jobs.list_all()[0]["messages"]], expected)
