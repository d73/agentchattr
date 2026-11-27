"""Check the service-scoped closeout route using a disposable local Job."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as chat


class OrchestratorBridgeTests(unittest.TestCase):
    def test_closeout_scope_persistence_and_idempotent_response(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as directory:
            config = {"server": {"data_dir": directory}, "orchestrator": {"closeout_job_id": 1}}
            token = "job-closeout-fixture-" * 2
            browser = "browser-session-fixture"
            with (patch.object(chat.app, "user_middleware", []),
                  patch.object(chat.app, "middleware_stack", None)):
                chat.configure(config, session_token=browser)
                credential_file = Path(directory) / ".orchestrator.env"
                self.assertIn("ORCH_CLOSEOUT_TOKEN=\n", credential_file.read_text(encoding="utf-8"))
                credential_file.write_text(f"ORCH_CLOSEOUT_TOKEN={token}\n", encoding="utf-8")
                job = chat.jobs.create("Assigned fixture", "job", "general", "fixture", status="open")
                other = chat.jobs.create("Outside scope", "job", "general", "fixture", status="open")
                client = TestClient(chat.app, client=("127.0.0.1", 50300), raise_server_exceptions=False)
                path = f"/api/orchestrator/jobs/{job['id']}/close"
                headers = {"Authorization": f"Bearer {token}"}
                self.assertEqual(403, client.post(path, headers={"X-Session-Token": browser}).status_code)
                self.assertEqual(404, client.post(f"/api/orchestrator/jobs/{other['id']}/close",
                                                 headers=headers).status_code)
                self.assertEqual(403, client.post(path, headers={**headers, "Origin": "http://example.com"}).status_code)
                with patch.object(chat.jobs, "_save", side_effect=OSError("fixture write failure")):
                    self.assertEqual(500, client.post(path, headers=headers).status_code)
                self.assertEqual("open", chat.jobs.get(job["id"])["status"])
                for _ in range(2):
                    response = client.post(path, headers=headers)
                    self.assertEqual(200, response.status_code)
                    self.assertEqual({"id": job["id"], "status": "done"}, response.json())
                self.assertEqual("open", chat.jobs.get(other["id"])["status"])
                self.assertEqual(403, client.get("/api/orchestrator/status").status_code)
                state = client.get("/api/orchestrator/status", headers={"X-Session-Token": browser}).json()
                self.assertEqual("offline", state["daemon"]["state"])
