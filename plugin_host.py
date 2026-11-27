"""Startup registration for trusted local AgentChattr plugins."""

from collections.abc import Callable
from copy import copy, deepcopy
from dataclasses import dataclass
from html import escape
from importlib.util import module_from_spec, spec_from_file_location
import logging
from pathlib import Path, PurePosixPath
import re
import sys
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response
from fastapi.routing import APIRoute
from mcp.server.fastmcp.tools import Tool
from starlette.routing import Match, compile_path


API_VERSION = 1
ASSET_PREFIX = "/plugin-assets/"
_ID = re.compile(r"[a-z][a-z0-9_]{0,39}")
_ASSET_PART = re.compile(r"[A-Za-z0-9_.-]+")


class RegistrationError(ValueError):
    """An actionable registration error whose message is safe to log."""


class _PluginFileHandler(logging.FileHandler):
    def emit(self, record):
        try:
            super().emit(record)
        except OSError:
            self.handleError(record)


@dataclass(frozen=True)
class Services:
    list_jobs: Callable
    get_job: Callable
    update_job_status: Callable
    registered_agents: Callable
    agent_status: Callable
    online_agents: Callable


@dataclass(frozen=True)
class Registration:
    routes: tuple[APIRoute, ...]
    service_auth: tuple[tuple[APIRoute, Callable], ...]
    tools: tuple[tuple[str, Callable], ...]
    assets: tuple[tuple[str, Path, str], ...]


def _route_keys(route):
    pattern = re.sub(r"\(\?P<[^>]+>", "(?:", route.path_regex.pattern)
    return {(method, pattern) for method in route.methods}


class PluginRegistration:
    """Collect declarations without changing the active HTTP or MCP server."""

    def __init__(self, plugin_id, root, settings, data_dir, services, logger):
        self.settings = deepcopy(settings)
        self.data_dir = data_dir
        self.services = services
        self.logger = logger
        self._plugin_id = plugin_id
        self._root = root
        self._routes = []
        self._service_auth = []
        self._tools = []
        self._assets = []

    def add_routes(self, router: APIRouter, *, service_auth=None):
        """Mount relative routes, with optional validators keyed by (method, path)."""
        if any(not isinstance(route, APIRoute) for route in router.routes):
            raise RegistrationError("only HTTP API routes may be registered")
        if router.on_startup or router.on_shutdown:
            raise RegistrationError("use the plugin startup hook for lifecycle callbacks")
        prefix = f"/api/plugins/{self._plugin_id}"
        checks = dict(service_auth or {})
        routes = []
        for declaration in router.routes:
            path = declaration.path
            if not path.startswith("/") or set(path.split("/")) & {".", ".."}:
                raise RegistrationError("declare routes below the plugin API prefix")
            if any(converter.regex == ".*" for converter in declaration.param_convertors.values()):
                raise RegistrationError("catch-all route parameters are unsupported")
            route = copy(declaration)
            route.path = prefix + path
            route.path_regex, route.path_format, route.param_convertors = compile_path(route.path)
            route.unique_id = f"{self._plugin_id}_{route.unique_id}"
            if route.operation_id:
                route.operation_id = f"{self._plugin_id}_{route.operation_id}"
            routes.append(route)
            matching = [(method, checks.pop((method, path))) for method in route.methods
                        if (method, path) in checks]
            if matching:
                if len(route.methods) != 1 or not callable(matching[0][1]):
                    raise RegistrationError("service authentication requires a single-method route and a validator")
                self._service_auth.append((route, matching[0][1]))
        if checks:
            raise RegistrationError("service authentication does not match a declared route")
        self._routes.extend(routes)

    def add_mcp_tools(self, tools):
        """Register a mapping of tool suffixes to callables."""
        for suffix, function in tools.items():
            if not _ID.fullmatch(suffix):
                raise RegistrationError("invalid MCP tool suffix")
            name = f"{self._plugin_id}_{suffix}"
            Tool.from_function(function, name=name)
            self._tools.append((name, function))

    def add_static(self, *, js=(), css=()):
        for asset_type, files in (("js", js), ("css", css)):
            for filename in files:
                path = PurePosixPath(filename)
                if (path.is_absolute() or not path.parts or path.suffix != f".{asset_type}"
                        or any(part in {".", ".."} or not _ASSET_PART.fullmatch(part)
                               for part in filename.split("/"))):
                    raise RegistrationError("declare a relative .js or .css asset inside the plugin")
                resolved = self._root.joinpath(*path.parts).resolve()
                if not resolved.is_relative_to(self._root) or not resolved.is_file():
                    raise RegistrationError("declared asset is missing or outside its plugin directory")
                self._assets.append((f"{self._plugin_id}/{path.as_posix()}", resolved, asset_type))

    def finalize(self):
        return Registration(tuple(self._routes), tuple(self._service_auth),
                            tuple(self._tools), tuple(self._assets))


class PluginHost:
    def __init__(self, data_dir: Path):
        self.log_path = data_dir.resolve() / "plugins.log"
        self._log = logging.getLogger(f"agentchattr.plugins.{uuid4().hex}")
        handler = _PluginFileHandler(self.log_path, encoding="utf-8", delay=True)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self._log.addHandler(handler)
        self._file_handler = handler
        self._registrations = []
        self._failed = []
        self._assets = {}
        self._validators = {}

    def close(self):
        self._log.removeHandler(self._file_handler)
        self._file_handler.close()

    def load(self, entries, services, *, core_routes=(), core_tools=()):
        known_routes = {key for route in core_routes if isinstance(route, APIRoute)
                        for key in _route_keys(route)}
        known_tools = set(core_tools)
        for plugin_id, entry in entries.items():
            phase = "configuration"
            module_name = f"_agentchattr_plugin_{uuid4().hex}"
            try:
                if not _ID.fullmatch(plugin_id):
                    raise RegistrationError("use a lowercase plugin ID with letters, digits, and underscores")
                if (not isinstance(entry, dict) or not isinstance(entry.get("path"), str)
                        or not entry["path"]):
                    raise RegistrationError("set the plugin path in config.local.toml")
                if not isinstance(entry.get("settings", {}), dict):
                    raise RegistrationError("plugin settings must be a table")
                root = Path(entry["path"]).resolve()
                if root.drive.startswith("\\\\"):
                    raise RegistrationError("the plugin directory must be on a local drive")
                init = (root / "__init__.py").resolve()
                if not init.is_relative_to(root) or not init.is_file():
                    raise RegistrationError("the configured directory needs a local __init__.py")
                staged = PluginRegistration(plugin_id, root, entry.get("settings", {}),
                                            self.log_path.parent, services, self._log.getChild(plugin_id))
                phase = "import"
                spec = spec_from_file_location(module_name, init, submodule_search_locations=[str(root)])
                module = module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                phase = "registration"
                if type(getattr(module, "HOST_API_VERSION", None)) is not int or module.HOST_API_VERSION != API_VERSION:
                    raise RegistrationError(f"the plugin must declare HOST_API_VERSION = {API_VERSION}")
                module.register(staged)
                registration = staged.finalize()
                route_keys = [key for route in registration.routes for key in _route_keys(route)]
                tool_names = [name for name, _ in registration.tools]
                asset_names = [name for name, _, _ in registration.assets]
                if len(set(route_keys)) != len(route_keys) or known_routes.intersection(route_keys):
                    raise RegistrationError("duplicate HTTP route")
                if len(set(tool_names)) != len(tool_names) or known_tools.intersection(tool_names):
                    raise RegistrationError("duplicate MCP tool name")
                if len(set(asset_names)) != len(asset_names) or self._assets.keys() & set(asset_names):
                    raise RegistrationError("duplicate static asset")
                phase = "startup"
                startup = getattr(module, "startup", None)
                if startup is not None:
                    startup(staged)
            except (Exception, SystemExit) as error:
                for name in tuple(sys.modules):
                    if name == module_name or name.startswith(f"{module_name}."):
                        sys.modules.pop(name, None)
                label = plugin_id if _ID.fullmatch(plugin_id) else "invalid plugin ID"
                self._failed.append(label)
                detail = str(error) if type(error) is RegistrationError else type(error).__name__
                self._log.error("%s plugin failed during %s: %s", label, phase, detail)
                continue
            known_routes.update(route_keys)
            known_tools.update(tool_names)
            self._assets.update((key, (path, asset_type)) for key, path, asset_type in registration.assets)
            self._validators.update((id(route), checker) for route, checker in registration.service_auth)
            self._registrations.append(registration)

    @property
    def tools(self):
        return tuple(tool for registration in self._registrations for tool in registration.tools)

    def install_http(self, app):
        for registration in self._registrations:
            app.router.routes.extend(registration.routes)
        if self._assets:
            app.add_api_route(f"{ASSET_PREFIX}{{name:path}}", self.asset_response,
                              methods=["GET", "HEAD"], include_in_schema=False)

    async def asset_response(self, name: str):
        asset = self._assets.get(name)
        if asset is None or not asset[0].is_file() or asset[0].resolve() != asset[0]:
            return Response(status_code=404)
        path, asset_type = asset
        media = "text/javascript" if asset_type == "js" else "text/css"
        return FileResponse(path, media_type=media, headers={"X-Content-Type-Options": "nosniff"})

    def service_validator(self, request: Request, routes):
        """Use the same ordered route match as FastAPI before bypassing session auth."""
        if not self._validators or not request.url.path.startswith("/api/plugins/"):
            return None
        for route in routes:
            if route.matches(request.scope)[0] is Match.FULL:
                return self._validators.get(id(route))
        return None

    def render_assets(self, html):
        styles = []
        scripts = []
        for key, (_, asset_type) in self._assets.items():
            url = escape(f"{ASSET_PREFIX}{key}", quote=True)
            if asset_type == "css":
                styles.append(f'<link rel="stylesheet" href="{url}">')
            else:
                scripts.append(f'<script src="{url}"></script>')
        if styles:
            html = html.replace("</head>", "\n".join(styles) + "\n</head>", 1)
        if scripts:
            html = html.replace("</body>", "\n".join(scripts) + "\n</body>", 1)
        if self._failed:
            names = ", ".join(self._failed)
            failure = (f"{names} plugin failed to load" if len(self._failed) == 1
                       else f"{len(self._failed)} plugins failed to load ({names})")
            label = escape(f"{failure} — see {self.log_path}", quote=True)
            warning = (f'<span class="settings-btn" tabindex="0" role="img" title="{label}" '
                       f'aria-label="{label}" style="color:#f59e0b">'
                       '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">'
                       '<path d="M8 2 15 14H1L8 2ZM8 6v4m0 1v1" stroke="currentColor" '
                       'stroke-width="1.3" stroke-linejoin="round"/></svg></span>')
            html = html.replace('<div class="header-right">', '<div class="header-right">' + warning, 1)
        return html
