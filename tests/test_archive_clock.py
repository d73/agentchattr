from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import archive
from jobs import JobStore
from store import MessageStore


class ArchiveClockTests(unittest.TestCase):
    def test_only_clock_values_are_preserved_from_chat_and_job_archives(self):
        values = ["00:00:00", "23:59:59", "24:00:00", 123, "Before < noon & after"]
        records = [
            {"uid": f"fixture-{index}", "text": f"Message {index}", "time": value}
            for index, value in enumerate(values)
        ]
        data = BytesIO()
        with ZipFile(data, "w") as output:
            output.writestr("manifest.json", json.dumps({"schema_version": archive.SCHEMA_VERSION}))
            output.writestr("messages.jsonl", "\n".join(map(json.dumps, records)))
            output.writestr("jobs.json", json.dumps([{"id": 1, "title": "Fixture", "messages": records}]))

        with TemporaryDirectory() as directory:
            root = Path(directory)
            messages = MessageStore(str(root / "messages.jsonl"))
            jobs = JobStore(str(root / "jobs.json"))
            with patch("store.time.strftime", return_value="12:34:56"):
                result = archive.import_archive(data.getvalue(), messages, jobs, None, None, ["general"])

            self.assertTrue(result["ok"])
            expected = values[:2] + ["12:34:56"] * 3
            self.assertEqual([record["time"] for record in messages.get_recent()], expected)
            self.assertEqual([record["time"] for record in jobs.list_all()[0]["messages"]], expected)
