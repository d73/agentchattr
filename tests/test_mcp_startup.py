"""Exercise startup with both real MCP transports on isolated loopback ports."""

from contextlib import ExitStack, closing
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CHECK_WEB_START = """
import socket
import threading
from unittest.mock import patch

from config_loader import load_config
import run
import uvicorn

run_transport = uvicorn.Server.run

def observe_web_start(server):
    if threading.current_thread() is not threading.main_thread():
        return run_transport(server)
    config = load_config(run.ROOT)["mcp"]
    for port in (config["http_port"], config["sse_port"]):
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    print("WEB_START")

with patch.object(uvicorn.Server, "run", observe_web_start):
    run.main()
"""


class McpStartupTests(unittest.TestCase):
    def run_startup(self, blocked_transport=None):
        workspace = next(
            (parent for parent in ROOT.parents if parent.name.casefold() == "temp"),
            ROOT.parent / "Temp",
        )
        workspace.mkdir(exist_ok=True)
        with ExitStack() as stack:
            directory = Path(stack.enter_context(tempfile.TemporaryDirectory(dir=workspace)))
            ports = {}
            healthy_listeners = []
            for transport in ("web", "HTTP", "SSE"):
                listener = stack.enter_context(closing(socket.socket()))
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                listener.bind(("127.0.0.1", 0))
                ports[transport] = listener.getsockname()[1]
                if transport == blocked_transport:
                    listener.listen()
                else:
                    healthy_listeners.append(listener)
            # Release the healthy ports only after assigning unique numbers.
            for listener in healthy_listeners:
                listener.close()
            result = subprocess.run(
                [
                    sys.executable, "-c", CHECK_WEB_START,
                    "--port", str(ports["web"]),
                    "--mcp-http-port", str(ports["HTTP"]),
                    "--mcp-sse-port", str(ports["SSE"]),
                    "--data-dir", str(directory / "data"),
                    "--upload-dir", str(directory / "uploads"),
                ],
                cwd=ROOT, capture_output=True, text=True, timeout=20,
            )
        return result, ports

    def test_web_starts_when_both_transports_bind(self):
        result, _ = self.run_startup()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("WEB_START", result.stdout)

    def test_web_stays_down_if_either_transport_cannot_bind(self):
        for transport in ("HTTP", "SSE"):
            with self.subTest(transport=transport):
                result, ports = self.run_startup(blocked_transport=transport)
                self.assertEqual(1, result.returncode, result.stderr)
                self.assertIn(f"MCP failed to start on 127.0.0.1:{ports[transport]}", result.stderr)
                self.assertNotIn("WEB_START", result.stdout)
