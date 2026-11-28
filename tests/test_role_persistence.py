import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import app
import mcp_bridge


class RolePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_save_leaves_previous_role_and_reports_failure(self):
        request = Mock()
        request.json = AsyncMock(return_value={"role": "New role"})
        published_roles = []

        async def broadcast_status():
            published_roles.append(mcp_bridge.get_role("worker"))

        with tempfile.TemporaryDirectory() as directory:
            roles_file = Path(directory) / "roles.json"
            roles_file.write_text('{"worker": "Old role"}', encoding="utf-8")
            with (
                patch.object(mcp_bridge, "_ROLES_FILE", roles_file),
                patch.object(mcp_bridge, "_roles", {"worker": "Old role"}),
                patch.object(app, "broadcast_status", broadcast_status),
            ):
                with (
                    patch("atomic_io.os.replace", side_effect=OSError("write failed")),
                    self.assertLogs("app", level="ERROR"),
                ):
                    response = await app.set_agent_role("worker", request)
                self.assertEqual(response.status_code, 500)
                self.assertEqual(mcp_bridge.get_role("worker"), "Old role")
                self.assertEqual(json.loads(roles_file.read_text(encoding="utf-8")), {"worker": "Old role"})
                self.assertEqual(published_roles, ["Old role"])

                response = await app.set_agent_role("worker", request)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(mcp_bridge.get_role("worker"), "New role")
                self.assertEqual(json.loads(roles_file.read_text(encoding="utf-8")), {"worker": "New role"})
                self.assertEqual(published_roles, ["Old role", "New role"])
