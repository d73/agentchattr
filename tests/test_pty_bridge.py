import socket
import struct
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch, Mock

from pty_bridge import MAX_FRAME, BridgeClient, receive_frame, send_frame
import wrapper_pty


class FramingTests(unittest.TestCase):
    def test_shutdown_stops_child_and_returns_from_wrapper_loop(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        self.addCleanup(listener.close)
        ready, stop = threading.Event(), threading.Event()
        connections = []

        def server():
            try:
                for _ in range(2):
                    conn, _ = listener.accept()
                    connections.append(conn)
                    self.assertEqual(receive_frame(conn)['token'], 'secret')
                    send_frame(conn, {'type': 'ready'})
                self.assertEqual(receive_frame(connections[1]), {'type': 'wait_shutdown'})
                ready.set()
                stop.wait(10)
                send_frame(connections[1], {'type': 'shutdown'})
            finally:
                for conn in connections:
                    conn.close()

        server_thread = threading.Thread(target=server, daemon=True)
        server_thread.start()
        result = []
        child_pid = [None]
        env = os.environ | {'TERMGRID_BRIDGE_TOKEN': 'secret', 'TERMGRID_BRIDGE_PORT': str(listener.getsockname()[1])}

        def run():
            try:
                with patch.dict('os.environ', env):
                    wrapper_pty.run_agent(sys.executable, ['-c', 'import time; time.sleep(60)'], '.', env,
                                          None, 'test', False, lambda callback: None, pid_holder=child_pid)
                result.append('returned')
            except Exception as exc:
                result.append(exc)

        runner = threading.Thread(target=run, daemon=True)
        runner.start()
        try:
            self.assertTrue(ready.wait(5))
            deadline = time.monotonic() + 5
            while child_pid[0] is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIsNotNone(child_pid[0])
            stop.set()
            runner.join(5)
            self.assertFalse(runner.is_alive())
            self.assertEqual(result, ['returned'])
            self.assertIsNone(child_pid[0])
        finally:
            stop.set()
            runner.join(5)
            server_thread.join(5)

    def test_round_trip_and_length_limit(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        send_frame(left, {'type': 'hello', 'token': 'test'})
        self.assertEqual(receive_frame(right), {'type': 'hello', 'token': 'test'})
        left.sendall(struct.pack('!I', MAX_FRAME + 1))
        with self.assertRaises(ValueError):
            receive_frame(right)

    @patch('wrapper_pty.subprocess.Popen')
    @patch('wrapper_pty.BridgeClient')
    @patch('wrapper_pty.threading.Thread')
    def test_bridge_secret_is_not_passed_to_agent(self, thread, client_type, popen):
        process = Mock()
        process.poll.return_value = 0
        popen.return_value = process
        injection, screen, control = Mock(), Mock(), Mock()
        client_type.side_effect = [injection, screen, control]
        env = {'TERMGRID_BRIDGE_TOKEN': 'secret', 'TERMGRID_BRIDGE_PORT': '1234', 'OTHER': 'ok'}
        with patch.dict('os.environ', env, clear=True):
            wrapper_pty.run_agent('claude', [], '.', dict(env), None, 'claude', True, lambda callback: None)
        sent_env = popen.call_args.kwargs['env']
        self.assertNotIn('TERMGRID_BRIDGE_TOKEN', sent_env)
        self.assertNotIn('TERMGRID_BRIDGE_PORT', sent_env)
        self.assertEqual(sent_env['OTHER'], 'ok')
        injection.close.assert_called_once()
        screen.close.assert_called_once()
        control.close.assert_called_once()

    def test_screen_poll_rejects_invalid_or_oversized_snapshots(self):
        with patch.object(BridgeClient, '_connect'):
            client = BridgeClient(1234, 'secret')
        client.socket = Mock()
        with patch('pty_bridge.send_frame'), patch('pty_bridge.receive_frame', return_value={
            'type': 'screen', 'valid': True, 'rows': ['x' * 501],
        }):
            self.assertIsNone(client.screen())
        with patch('pty_bridge.send_frame'), patch('pty_bridge.receive_frame', return_value={
            'type': 'screen', 'valid': True, 'rows': ['ready'],
        }):
            self.assertEqual(client.screen(), ['ready'])

    def test_authentication_failure_is_rejected(self):
        left, right = socket.socketpair()
        self.addCleanup(right.close)
        with patch('pty_bridge.socket.create_connection', return_value=left), patch('pty_bridge.receive_frame', return_value={'type': 'denied'}):
            with self.assertRaises(ConnectionError):
                BridgeClient(1234, 'secret')

    def test_disconnected_bridge_retry_is_bounded(self):
        with patch.object(BridgeClient, '_connect'):
            client = BridgeClient(1234, 'secret', retry_timeout=2)
        now = [0.0]
        with patch.object(client, '_connect', side_effect=OSError('offline')), \
             patch('pty_bridge.time.monotonic', side_effect=lambda: now[0]), \
             patch('pty_bridge.time.sleep', side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)):
            with self.assertRaises(TimeoutError):
                client.inject('test')
        self.assertLessEqual(now[0], 2)
