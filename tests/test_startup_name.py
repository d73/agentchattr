import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import URLError

import app
import wrapper
from registry import RuntimeRegistry


class StartupNameTests(unittest.TestCase):
    def test_registration_retries_refused_connection_and_occupied_name(self):
        registration = {'name': 'c2'}
        refused = URLError(ConnectionRefusedError())
        occupied = wrapper.NameInUseError('Name already in use: c2')
        with (
            patch.object(wrapper, '_register_instance', side_effect=[refused, occupied, registration]) as register,
            patch.object(wrapper.time, 'monotonic', side_effect=[0, 0, 1]),
            patch.object(wrapper.time, 'sleep') as sleep,
        ):
            self.assertEqual(wrapper._register_at_startup(8300, 'codex', None, 'c2'), registration)
        self.assertEqual(register.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_registration_stops_retrying_if_server_stays_unavailable(self):
        refused = URLError(ConnectionRefusedError())
        with (
            patch.object(wrapper, '_register_instance', side_effect=refused) as register,
            patch.object(wrapper.time, 'monotonic', side_effect=[0, 0, 1]),
            patch.object(wrapper.time, 'sleep') as sleep,
            self.assertRaises(URLError),
        ):
            wrapper._register_at_startup(8300, 'codex', None, 'c2', timeout=1)
        self.assertEqual(register.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_registration_does_not_retry_an_ambiguous_request_timeout(self):
        with (
            patch.object(wrapper, '_register_instance', side_effect=URLError(TimeoutError())) as register,
            patch.object(wrapper.time, 'sleep') as sleep,
            self.assertRaises(URLError),
        ):
            wrapper._register_at_startup(8300, 'codex', None, 'c2')
        register.assert_called_once()
        sleep.assert_not_called()

    def test_registration_endpoint_uses_requested_identity_and_rejects_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = RuntimeRegistry(tmp)
            registry.seed({'codex': {'label': 'Codex'}})
            async def body():
                return {'base': 'codex', 'name': 'c2'}
            request = SimpleNamespace(json=body)
            with patch.object(app, 'registry', registry), patch.object(app, '_event_loop', None):
                first = asyncio.run(app.register_agent(request))
                self.assertEqual(first.status_code, 200)
                data = json.loads(first.body)
                self.assertEqual(data['name'], 'c2')
                self.assertEqual(registry.resolve_token(data['token'])['name'], 'c2')
                duplicate = asyncio.run(app.register_agent(request))
                self.assertEqual(duplicate.status_code, 409)
                self.assertIn('already in use', json.loads(duplicate.body)['error'])
                self.assertEqual(registry.resolve_token(data['token'])['name'], 'c2')

    @unittest.skipUnless(os.name == 'nt', 'Windows batch launchers')
    def test_batch_launchers_forward_names_before_cli_separator(self):
        source = Path(__file__).resolve().parents[1] / 'windows'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            windows = root / 'windows'
            windows.mkdir()
            (windows / 'ensure_venv.bat').write_text(f'@set "VENV_PY={sys.executable}"\n')
            for cli in ('codex', 'claude', 'qwen', 'kilo'):
                (windows / f'{cli}.cmd').write_text('@exit /b 0\n')
            fake = "import json,sys; open('args.json','w').write(json.dumps(sys.argv[1:]))\n"
            (root / 'wrapper.py').write_text(fake)
            (root / 'wrapper_api.py').write_text(fake)
            env = dict(os.environ, PATH=f'{windows}{os.pathsep}{os.environ["PATH"]}')
            cases = [
                ('start_codex.bat', ['--name', 'c2'], ['codex', '--name', 'c2', '--', '--no-daemon', '--approve-for-me']),
                ('start_claude.bat', ['--name', 'c2'], ['claude', '--name', 'c2']),
                ('start_kilo.bat', ['vendor/model', '--name', 'c2'], ['kilo', '--name', 'c2', '--', '-m', 'vendor/model']),
                ('start_kilo.bat', ['--name', 'c2'], ['kilo', '--name', 'c2']),
                ('start_api_agent.bat', ['qwen', '--name', 'c2'], ['qwen', '--name', 'c2']),
            ]
            for filename, args, expected in cases:
                with self.subTest(filename=filename, args=args):
                    text = (source / filename).read_text(encoding='utf-8')
                    # Keep the launcher logic, replacing only its live server probe with a no-op.
                    text = text.replace(
                        "import socket; from config_loader import load_config; "
                        "socket.create_connection(('127.0.0.1', load_config()['server']['port']), 1)",
                        'pass',
                    )
                    for cli in ('codex', 'claude', 'qwen', 'kilo'):
                        text = text.replace(f'where {cli} >nul 2>&1', 'ver >nul')
                    script = windows / filename
                    script.write_text(text, encoding='utf-8')
                    result = subprocess.run(['cmd.exe', '/d', '/c', str(script), *args], input='\n', text=True,
                                            capture_output=True, env=env, timeout=15,
                                            creationflags=subprocess.CREATE_NO_WINDOW)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn('agentchattr server is already listening.', result.stdout)
                    self.assertIn('Launching ', result.stdout)
                    self.assertEqual(json.loads((root / 'args.json').read_text()), expected)


if __name__ == '__main__':
    unittest.main()
