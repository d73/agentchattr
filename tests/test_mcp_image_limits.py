from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app
import mcp_bridge
from jobs import JobStore
from store import MessageStore


class MCPImageLimitTests(unittest.TestCase):
    def test_http_channel_and_job_uploads_use_the_same_limit(self):
        with TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            uploads = root / "uploads"
            config = {"images": {"upload_dir": str(uploads), "max_size_mb": 2 / 1024**2}}
            store = MessageStore(str(root / "chat.jsonl"))
            jobs = JobStore(str(root / "jobs.json"))
            job = jobs.create("Fixture", "job", "general", "fixture")
            small = root / "small.png"
            large = root / "large.png"
            small.write_bytes(b"ab")
            large.write_bytes(b"abc")

            for name, value in {
                "store": store, "jobs": jobs, "config": config, "registry": None,
                "router": None, "agents": None, "_presence": {}, "_update_cursor": lambda *args: None,
            }.items():
                stack.enter_context(patch.object(mcp_bridge, name, value))
            stack.enter_context(patch.object(app, "config", config))
            api = FastAPI()
            api.add_api_route("/api/upload", app.upload_image, methods=["POST"])
            client = stack.enter_context(TestClient(api))

            self.assertEqual(client.post("/api/upload", files={"file": (large.name, b"abc")}).status_code, 413)
            for target in ({"channel": "general"}, {"job_id": job["id"]}):
                result = mcp_bridge.chat_send("fixture", "large", image_path=str(large), **target)
                self.assertIn("file too large", result)
            self.assertEqual(store.get_recent(), [])
            self.assertEqual(jobs.get(job["id"])["messages"], [])
            self.assertEqual(list(uploads.iterdir()), [])

            self.assertEqual(client.post("/api/upload", files={"file": (small.name, b"ab")}).status_code, 200)
            for target in ({"channel": "general"}, {"job_id": job["id"]}):
                result = mcp_bridge.chat_send("fixture", "small", image_path=str(small), **target)
                self.assertTrue(result.startswith("Sent"), result)
            self.assertEqual(store.get_recent()[0]["attachments"][0]["name"], small.name)
            self.assertEqual(jobs.get(job["id"])["messages"][0]["attachments"][0]["name"], small.name)
            self.assertEqual([file.read_bytes() for file in uploads.iterdir()], [b"ab"] * 3)
