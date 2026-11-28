import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app


class RoleWriteAccessTests(unittest.TestCase):
    def test_role_writes_require_a_local_client_and_browser_session(self):
        api = FastAPI()
        api.add_api_route("/api/roles", app.get_roles, methods=["GET"])
        api.add_api_route("/api/roles/{agent_name}", app.set_agent_role, methods=["POST"])

        with (
            patch.object(app, "app", api),
            patch.object(app, "session_token", None),
            patch.object(app, "broadcast_status") as broadcast,
            patch("mcp_bridge.get_all_roles", return_value={}),
            patch("mcp_bridge.set_role") as set_role,
        ):
            app._install_security_middleware("role-fixture", {})
            headers = {"X-Session-Token": "role-fixture"}
            body = {"role": "reviewer"}
            url = "/api/roles/fixture"

            with (
                TestClient(api, client=("192.0.2.2", 50000)) as remote,
                TestClient(api, client=("127.0.0.1", 50001)) as local,
            ):
                self.assertEqual(remote.get("/api/roles").status_code, 200)
                self.assertEqual(remote.post(url, json=body).status_code, 403)
                self.assertEqual(remote.post(url, json=body, headers=headers).status_code, 403)
                self.assertEqual(local.post(url, json=body).status_code, 403)
                cross_origin = {**headers, "Origin": "http://example.invalid:8300"}
                self.assertEqual(local.post(url, json=body, headers=cross_origin).status_code, 403)
                set_role.assert_not_called()

                self.assertEqual(local.post(url, json=body, headers=headers).status_code, 200)

            set_role.assert_called_once_with("fixture", "reviewer")
            broadcast.assert_awaited_once()
