import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app


class Socket:
    query_params = {"token": "fixture"}
    client = SimpleNamespace(host="127.0.0.1")

    def __init__(self, blocked=False):
        self.blocked = blocked
        self.frames = []
        self.sending = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_code = None

    async def accept(self):
        pass

    async def send_text(self, frame):
        self.sending.set()
        if self.blocked:
            await asyncio.Event().wait()
        self.frames.append(frame)

    async def close(self, code, reason):
        self.close_code = code
        self.closed.set()


class WebSocketFanoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_initial_snapshot_clears_browser_count(self):
        slow = Socket(blocked=True)
        store = Mock()
        store.log_status.return_value = {}
        with patch.multiple(app, store=store, session_token="fixture", ws_clients=set(),
                            _ws_writers={}, _WS_SEND_TIMEOUT=0.05):
            await asyncio.wait_for(app.websocket_endpoint(slow), 1)
            self.assertTrue(slow.sending.is_set())
            self.assertEqual(slow.close_code, 1013)
            self.assertEqual(await app.get_browser_clients(), {"count": 0})

    async def test_stalled_client_does_not_delay_other_clients_and_times_out(self):
        slow = Socket(blocked=True)
        fast = Socket()
        clients = {slow, fast}
        with patch.multiple(app, ws_clients=clients, _ws_writers={}, _WS_SEND_TIMEOUT=0.05):
            await asyncio.wait_for(app._broadcast("first"), 1)
            await asyncio.wait_for(app._broadcast("second"), 1)
            self.assertTrue(slow.sending.is_set())
            self.assertEqual(fast.frames, ["first", "second"])
            await asyncio.wait_for(slow.closed.wait(), 1)
            self.assertEqual(slow.close_code, 1013)
            self.assertEqual(clients, {fast})
            self.assertEqual(app._ws_writers, {})

    async def test_pending_frames_are_bounded_per_client(self):
        slow = Socket(blocked=True)
        fast = Socket()
        clients = {slow, fast}
        with patch.multiple(app, ws_clients=clients, _ws_writers={}, _WS_MAX_PENDING=2):
            for frame in ("one", "two", "three", "four"):
                await app._broadcast(frame)
            await asyncio.wait_for(slow.closed.wait(), 1)
            self.assertEqual(fast.frames, ["one", "two", "three", "four"])
            self.assertEqual(clients, {fast})
            self.assertEqual(app._ws_writers, {})
