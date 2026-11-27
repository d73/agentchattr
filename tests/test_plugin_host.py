import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import textwrap
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app as chat
from config_loader import load_config
import mcp_bridge
from plugin_host import PluginHost, Services


FIXTURE = '''
from fastapi import APIRouter

HOST_API_VERSION = 1

def register(host):
    router = APIRouter()

    @router.get('/status')
    def status():
        return {'jobs': host.services.list_jobs(), 'mode': host.settings['mode']}

    @router.post('/jobs/{job_id:int}/close')
    def close_job(job_id: int):
        return {'closed': job_id}

    async def authorized(request):
        return request.headers.get('authorization') == 'Bearer fixture-token'

    host.add_routes(router, service_auth={('POST', '/jobs/{job_id:int}/close'): authorized})

    def list_jobs() -> str:
        """List fixture jobs."""
        return str(host.services.list_jobs())

    host.add_mcp_tools({'list_jobs': list_jobs})
    host.add_static(js=['ui.js'], css=['ui.css'])
'''

PAGE = '<head></head><body><div class="header-right"></div></body>'


class PluginHostTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.services = Services(lambda: [42], lambda job_id: None, lambda: None,
                                 dict, dict, list)

    def plugin(self, name, source=FIXTURE):
        folder = self.root / name
        folder.mkdir()
        (folder / '__init__.py').write_text(textwrap.dedent(source), encoding='utf-8')
        (folder / 'ui.js').write_text('window.fixture = true;', encoding='utf-8')
        (folder / 'ui.css').write_text('body {}', encoding='utf-8')
        return {'path': str(folder), 'settings': {'mode': 'fixture'}}

    def host(self):
        host = PluginHost(self.root)
        self.addCleanup(host.close)
        return host

    def web(self, host):
        from unittest.mock import patch

        web = FastAPI()
        with patch.object(chat, 'app', web), patch.object(chat, 'session_token', ''):
            chat._install_security_middleware('browser-token', {})
        # Middleware reads the live global token via the app module.
        patcher = patch.object(chat, 'session_token', 'browser-token')
        patcher.start()
        self.addCleanup(patcher.stop)
        if host is not None:
            web.state.plugins = host
            host.install_http(web)
        return web

    def test_local_config_resolves_plugin_paths_and_ignores_base_entries(self):
        (self.root / 'config.toml').write_text('[plugins.untrusted]\npath = "elsewhere"\n', encoding='utf-8')
        (self.root / 'config.local.toml').write_text(
            '[plugins.fixture]\npath = "relative/plugin"\n[plugins.fixture.settings]\nmode = "test"\n',
            encoding='utf-8',
        )
        config = load_config(self.root)
        self.assertEqual(set(config['plugins']), {'fixture'})
        self.assertEqual(Path(config['plugins']['fixture']['path']), self.root / 'relative' / 'plugin')
        self.assertEqual(config['plugins']['fixture']['settings'], {'mode': 'test'})

    def test_installed_http_assets_and_auth(self):
        host = self.host()
        host.load({'fixture': self.plugin('fixture')}, self.services)
        web = self.web(host)
        local = TestClient(web, client=('127.0.0.1', 50300))
        remote = TestClient(web, client=('192.168.0.20', 50300))
        session = {'x-session-token': 'browser-token'}
        service = {'authorization': 'Bearer fixture-token'}
        status = '/api/plugins/fixture/status'
        close = '/api/plugins/fixture/jobs/42/close'
        self.assertEqual(local.get(status).status_code, 403)
        self.assertEqual(local.get(status, headers=session).json(), {'jobs': [42], 'mode': 'fixture'})
        self.assertEqual(local.get(status, headers=service).status_code, 403)
        self.assertEqual(local.post(close, headers=session).status_code, 403)
        self.assertEqual(local.post(close, headers=service).json(), {'closed': 42})
        self.assertEqual(local.post(close, headers={**service, 'origin': 'https://other.invalid'}).status_code, 403)
        self.assertEqual(remote.post(close, headers=service).status_code, 403)
        self.assertEqual(local.get(close, headers=service).status_code, 403)
        self.assertEqual(local.post(close + '/extra', headers=service).status_code, 403)
        self.assertEqual(local.get('/plugin-assets/fixture/ui.js').text, 'window.fixture = true;')
        self.assertEqual(local.get('/plugin-assets/fixture/__init__.py').status_code, 404)
        self.assertEqual(remote.get('/plugin-assets/fixture/ui.js').status_code, 403)
        self.assertIn('<script src="/plugin-assets/fixture/ui.js">', host.render_assets(PAGE))
        self.assertIn('href="/plugin-assets/fixture/ui.css"', host.render_assets(PAGE))

    def test_failed_plugins_publish_no_capabilities(self):
        startup_error = FIXTURE + '\ndef startup(host):\n    raise RuntimeError("credential-marker")\n'
        outside_asset = FIXTURE + '\noriginal = register\ndef register(host):\n    original(host)\n    host.add_static(js=["../outside.js"])\n'
        host = self.host()
        host.load({'broken': self.plugin('broken', startup_error),
                   'escape': self.plugin('escape', outside_asset),
                   'collision': self.plugin('collision'), 'fixture': self.plugin('fixture')},
                  self.services, core_tools=['collision_list_jobs'])
        web = self.web(host)
        client = TestClient(web, client=('127.0.0.1', 50300))
        session = {'x-session-token': 'browser-token'}
        for plugin in ('broken', 'escape', 'collision'):
            self.assertEqual(client.get(f'/api/plugins/{plugin}/status', headers=session).status_code, 404)
            self.assertEqual(client.get(f'/plugin-assets/{plugin}/ui.js').status_code, 404)
        self.assertEqual(client.get('/api/plugins/fixture/status', headers=session).status_code, 200)
        self.assertEqual([name for name, _ in host.tools], ['fixture_list_jobs'])
        log = host.log_path.read_text(encoding='utf-8')
        self.assertEqual(log.count('broken plugin failed during startup'), 1)
        self.assertIn('duplicate MCP tool name', log)
        self.assertNotIn('credential-marker', log)
        html = host.render_assets(PAGE)
        self.assertIn(str(host.log_path), html)
        self.assertNotIn('/plugin-assets/broken/', html)

    def test_no_plugins_leaves_html_and_transports_unchanged(self):
        empty = self.host()
        empty.load({}, self.services)
        self.assertEqual(empty.render_assets(PAGE), PAGE)
        self.assertFalse(empty.log_path.exists())
        self.assertEqual(empty.tools, ())
        self.assertFalse(any(route.path.startswith('/plugin-assets/') for route in self.web(empty).routes))

    def test_mcp_tool_is_available_on_both_server_instances(self):
        host = self.host()
        host.load({'fixture': self.plugin('fixture')}, self.services)

        async def exercise_tools():
            for port in (8200, 8201):
                server = mcp_bridge._create_server(port, host.tools)
                self.assertIn('fixture_list_jobs', [tool.name for tool in await server.list_tools()])
                result = await server.call_tool('fixture_list_jobs', {})
                content = result[0] if isinstance(result, tuple) else result
                self.assertEqual(content[0].text, '[42]')

        asyncio.run(exercise_tools())
