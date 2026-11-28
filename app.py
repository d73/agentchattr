"""agentchattr — FastAPI web UI + agent auto-trigger."""

import asyncio
from collections import deque
from inspect import isawaitable
import json
import re as _re
from atomic_io import atomic_write_text
import sys
import threading
import uuid
import logging
import time
import shutil
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.requests import Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from attachment_storage import DEFAULT_IMAGE_LIMIT_MB, IMAGE_UPLOAD_EXTS, image_upload_limit
from store import MessageStore
from rules import RuleStore
from summaries import SummaryStore
from jobs import JobStore
from job_reopen import ReopenConflict
from schedules import ScheduleStore, parse_schedule_spec
from router import Router, without_code
from agents import AgentTrigger, PROPOSAL_WRITER_BASES
from registry import RuntimeRegistry, sanitize_chat_id
from session_store import SessionStore, validate_session_template
from session_engine import SessionEngine
from plugin_host import ASSET_PREFIX
from operator_auth import verify_operator
from orch_workflow import history as orch_history, history_context as orch_history_context
from orch_workflow import record_reply as record_orch_reply, reply_metadata as orch_reply_metadata

log = logging.getLogger(__name__)

app = FastAPI(title="agentchattr")


@app.exception_handler(OSError)
async def handle_file_error(request: Request, exc: OSError):
    path = str(Path(exc.filename).resolve()) if exc.filename else None
    log.exception("File operation failed: %s", exc)
    return JSONResponse({"error": str(exc), "path": path}, status_code=500)

# --- globals (set by configure()) ---
store: MessageStore | None = None
rules: RuleStore | None = None
summaries: SummaryStore | None = None
jobs: JobStore | None = None
schedules: ScheduleStore | None = None
router: Router | None = None
agents: AgentTrigger | None = None
registry: RuntimeRegistry | None = None
session_store: SessionStore | None = None
session_engine: SessionEngine | None = None
config: dict = {}
ws_clients: set[WebSocket] = set()
_routing_refresh_lock = threading.Lock()

# --- Security: session token (set by configure()) ---
session_token: str = ""

DEFAULT_CHAT_FONT_SIZE = 14

# Room settings (persisted to data/settings.json)
room_settings: dict = {
    "title": "agentchattr",
    # Shown beside the title, so several servers can be told apart without
    # renaming the product itself. "" hides it.
    "subtitle": "",
    "username": "user",
    "font": "sans",
    "font_size": DEFAULT_CHAT_FONT_SIZE,
    "channels": ["general"],
    # Manually archived channels (hidden in a collapsed "Archived" group) and the
    # idle threshold, in hours, after which a channel moves to "Inactive" (0 = off).
    "archived_channels": [],
    "inactive_hours": 24,
    "history_limit": 50,
    "contrast": "normal",
    "custom_roles": [],
}
# "ui_prefs" follows the same rule as "agent_colors" below.
# "agent_colors" ({name: "#rrggbb"}) is deliberately absent until a client first
# sets it. Absent = never initialized (a client may seed it from its legacy
# localStorage); present, even as {}, = authoritative (an explicit Reset).

def _clean_ui_prefs(raw: dict) -> dict:
    """Whitelist + validate synced per-user UI options (absent key = unset)."""
    out: dict = {}
    sounds = raw.get("sounds")
    if isinstance(sounds, dict):
        out["sounds"] = {
            str(k)[:64]: v for k, v in sounds.items()
            if isinstance(v, str) and _re.fullmatch(r"[a-z0-9-]{0,40}", v)
        }
    if isinstance(raw.get("user_color"), str) and _HEX_COLOR_RE.fullmatch(raw["user_color"]):
        out["user_color"] = raw["user_color"]
    if raw.get("attention_style") in ("highlight", "full-highlight", "flash"):
        out["attention_style"] = raw["attention_style"]
    att = raw.get("attention_sound")
    if isinstance(att, str) and _re.fullmatch(r"[a-z0-9-]{1,40}", att):  # sound name, "none", or legacy on/off
        out["attention_sound"] = att
    if raw.get("sidebar_mode") in ("top", "sidebar"):
        out["sidebar_mode"] = raw["sidebar_mode"]
    if raw.get("channel_attention") in ("off", "highlight", "flash"):
        out["channel_attention"] = raw["channel_attention"]
    return out


# Channel validation
_HEX_COLOR_RE = _re.compile(r'#[0-9a-fA-F]{6}')  # module-level: a later local `import re as _re` shadows _re inside websocket_endpoint
_CHANNEL_NAME_RE = _re.compile(r'^[a-z0-9][a-z0-9\-]{0,19}$')

# Agent hats (persisted to data/hats.json)
agent_hats: dict[str, str] = {}  # { agent_name: svg_string }


def _hats_path() -> Path:
    data_dir = config.get("server", {}).get("data_dir", "./data")
    return Path(data_dir) / "hats.json"


def _clean_agent_colors(raw) -> dict:
    """Validate a {name: "#rrggbb"} map (live updates and file loads share this)."""
    if not isinstance(raw, dict):
        return {}
    return {
        str(k)[:64]: v for k, v in raw.items()
        if isinstance(v, str) and _HEX_COLOR_RE.fullmatch(v)
    }


def _save_agent_color_changes(changes: dict, seed) -> dict:
    """Persist one client's edits without replacing other clients' overrides."""
    previous = room_settings.get("agent_colors")
    previous_revision = room_settings.get("agent_colors_revision")
    colors = dict(previous) if previous is not None else _clean_agent_colors(seed)
    colors.update(_clean_agent_colors(changes))
    for name, color in changes.items():
        if color is None and len(name) <= 64:
            colors.pop(name, None)
    room_settings["agent_colors"] = colors
    room_settings["agent_colors_revision"] = (previous_revision or 0) + 1
    try:
        _save_settings()
    except OSError:
        if previous is None:
            room_settings.pop("agent_colors", None)
        else:
            room_settings["agent_colors"] = previous
        if previous_revision is None:
            room_settings.pop("agent_colors_revision", None)
        else:
            room_settings["agent_colors_revision"] = previous_revision
        raise
    return colors


def _quarantine_corrupt(p: Path, reason) -> None:
    """Keep an unreadable state file for recovery instead of silently dropping it.

    The next save would otherwise overwrite it with defaults and lose the
    original contents for good.
    """
    try:
        dest = p.with_name(f"{p.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(p, dest)
        log.warning("Could not read %s (%s); kept a copy at %s and using defaults", p, reason, dest)
    except Exception as exc:  # never let backup trouble block startup
        log.warning("Could not read %s (%s); backup failed too: %s", p, reason, exc)


def _load_hats():
    global agent_hats
    p = _hats_path()
    if p.exists():
        try:
            loaded = json.loads(p.read_text("utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("expected a JSON object")
            agent_hats = loaded
        except Exception as exc:
            _quarantine_corrupt(p, exc)
            agent_hats = {}


def _save_hats():
    p = _hats_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(agent_hats))


def _sanitize_svg(svg: str) -> str:
    """Strip dangerous content from SVG string."""
    svg = _re.sub(r'<script[^>]*>.*?</script>', '', svg, flags=_re.DOTALL | _re.IGNORECASE)
    svg = _re.sub(r'\bon\w+\s*=', '', svg, flags=_re.IGNORECASE)
    svg = _re.sub(r'javascript\s*:', '', svg, flags=_re.IGNORECASE)
    return svg


def set_agent_hat(agent: str, svg: str) -> str | None:
    """Validate, sanitize, and store a hat SVG. Returns error string or None."""
    svg = svg.strip()
    if not svg.lower().startswith("<svg"):
        return "Hat must be an SVG element (starts with <svg)."
    if len(svg) > 5120:
        return "Hat SVG too large (max 5KB)."
    svg = _sanitize_svg(svg)
    agent_hats[agent.lower()] = svg
    _save_hats()
    if _event_loop:
        asyncio.run_coroutine_threadsafe(broadcast_hats(), _event_loop)
    return None


def clear_agent_hat(agent: str):
    """Remove an agent's hat."""
    key = agent.lower()
    if key in agent_hats:
        del agent_hats[key]
        _save_hats()
        if _event_loop:
            asyncio.run_coroutine_threadsafe(broadcast_hats(), _event_loop)


def _settings_path() -> Path:
    data_dir = config.get("server", {}).get("data_dir", "./data")
    return Path(data_dir) / "settings.json"


ROOM_SUBTITLE_MAX = 40


def _valid_chat_font_size(value):
    return type(value) is int and 10 <= value <= 32


def normalize_room_subtitle(value):
    """Clean a room subtitle, or return None if it is not usable.

    Collapses any internal whitespace so a pasted newline cannot break the
    header across two lines, and caps the length so a long name cannot crowd
    out the rest of the header.
    """
    if not isinstance(value, str):
        return None
    return " ".join(value.split())[:ROOM_SUBTITLE_MAX]


def apply_room_subtitle(new: dict):
    """Apply a subtitle from a settings payload, if present and usable."""
    if "subtitle" not in new:
        return
    cleaned = normalize_room_subtitle(new["subtitle"])
    if cleaned is not None:
        room_settings["subtitle"] = cleaned


def _load_settings():
    global room_settings
    p = _settings_path()
    if p.exists():
        try:
            saved = json.loads(p.read_text("utf-8"))
            if not isinstance(saved, dict):
                raise ValueError("expected a JSON object")
            room_settings.update(saved)
        except Exception as exc:
            _quarantine_corrupt(p, exc)
    # One-time move off the old default ("all"): loading every message per channel on
    # connect grows without bound. Users can still pick All in Settings afterwards.
    if not room_settings.get("history_default_v2"):
        if room_settings.get("history_limit") == "all":
            room_settings["history_limit"] = 50
        room_settings["history_default_v2"] = True
    # The file is hand-editable, so run synced options through the same
    # validators as live updates; drop them (back to "uninitialized") if invalid.
    if "ui_prefs" in room_settings:
        if isinstance(room_settings["ui_prefs"], dict):
            room_settings["ui_prefs"] = _clean_ui_prefs(room_settings["ui_prefs"])
        else:
            del room_settings["ui_prefs"]
    if "agent_colors" in room_settings:
        if isinstance(room_settings["agent_colors"], dict):
            room_settings["agent_colors"] = _clean_agent_colors(room_settings["agent_colors"])
        else:
            del room_settings["agent_colors"]
    revision = room_settings.get("agent_colors_revision")
    if type(revision) is not int or revision < 0:
        room_settings.pop("agent_colors_revision", None)
    # settings.json is hand-editable, so the file bypasses every check the
    # live update path applies. Re-normalise on load rather than trust it.
    room_settings["subtitle"] = normalize_room_subtitle(room_settings.get("subtitle")) or ""
    if not _valid_chat_font_size(room_settings.get("font_size")):
        room_settings["font_size"] = DEFAULT_CHAT_FONT_SIZE
    # Ensure "general" always exists and is first
    if "channels" not in room_settings or not room_settings["channels"]:
        room_settings["channels"] = ["general"]
    elif "general" not in room_settings["channels"]:
        room_settings["channels"].insert(0, "general")
    _normalise_channel_prefs()


def _clean_inactive_hours(value) -> int | None:
    """0 turns the Inactive group off; otherwise 1 hour to one year. None = invalid."""
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, min(hours, 24 * 365))


def _normalise_channel_prefs():
    """Keep archive/inactive settings valid: only real, non-general channels can be archived."""
    channels = room_settings.get("channels", [])
    archived = room_settings.get("archived_channels")
    if not isinstance(archived, list):
        archived = []
    room_settings["archived_channels"] = [
        c for i, c in enumerate(archived)
        if isinstance(c, str) and c != "general" and c in channels and c not in archived[:i]
    ]
    hours = _clean_inactive_hours(room_settings.get("inactive_hours", 24))
    room_settings["inactive_hours"] = 24 if hours is None else hours


def _unarchive_for_activity(channel: str, msg_type: str, sender: str = "") -> bool:
    """A message from a person or agent in an archived channel brings it back. Automated notices
    (join/leave/system types, or anything sent by "system") don't count, so an agent disconnecting
    doesn't pull an archived channel out of the Archived group."""
    if msg_type in ("system", "leave", "join") or sender == "system":
        return False
    archived = room_settings.get("archived_channels", [])
    if channel not in archived:
        return False
    room_settings["archived_channels"] = [c for c in archived if c != channel]
    _save_settings()
    return True


def _settings_payload() -> dict:
    """Settings as sent to clients: persisted values plus live per-channel activity."""
    return {**room_settings, "channel_activity": _channel_activity()}


def _channel_activity() -> dict[str, float]:
    """Newest activity per channel: main-chat messages and real job-thread replies."""
    latest = store.channel_activity()
    if jobs is not None:
        for channel, ts in jobs.channel_activity().items():
            if ts > latest.get(channel, 0):
                latest[channel] = ts
    return latest


def _save_settings():
    p = _settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(room_settings, indent=2))


def _rename_channel_references(old_name: str, new_name: str):
    # Keep the old name available for retry until both dependent stores are saved.
    schedules.rename_channel(old_name, new_name)
    jobs.rename_channel(old_name, new_name)
    for job in jobs.list_all(channel=new_name):
        _on_job_change("update", job)
    for schedule in schedules.list_all():
        if schedule.get("channel") == new_name:
            _on_schedule_change("update", schedule)


def _extract_agent_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-agent-token", "").strip()


def _resolve_authenticated_agent(request: Request) -> dict | None:
    if not registry:
        return None
    token = _extract_agent_token(request)
    if not token:
        return None
    return registry.resolve_token(token)


# --- Local vs. LAN clients ---
# The server may listen on the LAN (server.allow_lan). Only requests from this PC
# (loopback or one of its own interface addresses) get the session token and full
# access; other devices on the network are refused everywhere except static assets.
_local_ips_cache: tuple[float, frozenset] = (0.0, frozenset())


def _local_ips() -> frozenset:
    global _local_ips_cache
    now = time.time()
    if now - _local_ips_cache[0] < 30 and _local_ips_cache[1]:
        return _local_ips_cache[1]
    import socket
    ips = {"127.0.0.1", "::1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ips.add(info[4][0].split("%")[0])
    except OSError:
        pass
    try:  # primary outbound interface (no packets are sent for a UDP connect)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            ips.add(probe.getsockname()[0])
    except OSError:
        pass
    _local_ips_cache = (now, frozenset(ips))
    return _local_ips_cache[1]


def is_local_client(client_ip: str) -> bool:
    ip = (client_ip or "").split("%")[0]
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    return ip in ("localhost",) or ip.startswith("127.") or ip in _local_ips()


def _origin_allowed(origin: str, port: int) -> bool:
    """Allow only this server's own origin: localhost, loopback, or this PC's own IPs."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(origin)
        host, origin_port = parts.hostname, parts.port or 80
    except ValueError:
        return False
    if parts.scheme != "http" or origin_port != port or not host:
        return False
    return host == "localhost" or host.startswith("127.") or host in _local_ips()


# --- Security middleware ---
# Paths that don't require the session token (public assets).
_PUBLIC_PREFIXES = ("/", "/static/")


def _install_security_middleware(token: str, cfg: dict):
    """Add token validation and origin checking middleware to the app."""
    import app as _self
    _self.session_token = token
    port = cfg.get("server", {}).get("port", 8300)

    class SecurityMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            path = request.url.path

            # Static assets, index page, and uploaded images are public.
            # The index page injects the token client-side via same-origin script.
            # Images keep their public preview URLs. Other attachments require
            # the browser session token, like the rest of the API.
            public_image = path.startswith("/uploads/") and Path(path).suffix.lower() in IMAGE_UPLOAD_EXTS
            client_ip = request.client.host if request.client else ""
            public_roles = request.method == "GET" and path == "/api/roles"
            if path == "/" or path.startswith("/static/") or public_roles or public_image:
                return await call_next(request)

            # Devices on the LAN never get API access, even with a token.
            if not is_local_client(client_ip):
                return JSONResponse(
                    {"error": "forbidden: this server only accepts requests from the PC it runs on"},
                    status_code=403,
                )

            # Agent registration/heartbeat: loopback only (no remote agent minting).
            if path.startswith(("/api/register", "/api/deregister/", "/api/heartbeat/")):
                if not is_local_client(client_ip):
                    return JSONResponse(
                        {"error": f"forbidden: agent registration is restricted to local loopback. Source {client_ip} is not allowed."},
                        status_code=403,
                    )
                return await call_next(request)

            # --- Origin check (blocks cross-origin / DNS-rebinding attacks) ---
            origin = request.headers.get("origin")
            if origin and not _origin_allowed(origin, port):
                return JSONResponse(
                    {"error": "forbidden: origin not allowed"},
                    status_code=403,
                )

            plugins = getattr(request.app.state, "plugins", None)
            if plugins is not None:
                if request.method in {"GET", "HEAD"} and path.startswith(ASSET_PREFIX):
                    return await call_next(request)
                validator = plugins.service_validator(request, request.app.router.routes)
                if validator is not None:
                    try:
                        authorized = validator(request)
                        if isawaitable(authorized):
                            authorized = await authorized
                    except Exception:
                        log.warning("Plugin service credential validator failed")
                        authorized = False
                    if authorized is not True:
                        return JSONResponse({"error": "plugin service credential required"}, status_code=403)
                    return await call_next(request)

            # --- Token check ---
            # Allow registered agents to authenticate via Bearer token
            # for /api/messages and /api/send (no browser session needed).
            auth_header = request.headers.get("authorization", "")
            if auth_header.lower().startswith("bearer ") and (path in ("/api/messages", "/api/send") or path.startswith("/api/rules/")):
                bearer = auth_header[7:].strip()
                if _self.registry and _self.registry.resolve_token(bearer):
                    return await call_next(request)

            req_token = (
                request.headers.get("x-session-token")
                or request.query_params.get("token")
            )
            if req_token != _self.session_token:
                return JSONResponse(
                    {"error": "forbidden: invalid or missing session token"},
                    status_code=403,
                )

            return await call_next(request)

    app.add_middleware(SecurityMiddleware)


def configure(cfg: dict, session_token: str = ""):
    global store, rules, summaries, jobs, schedules, router, agents, registry, session_store, session_engine, config
    config = cfg
    # --- Security: store the session token and install middleware ---
    _install_security_middleware(session_token, cfg)

    data_dir = cfg.get("server", {}).get("data_dir", "./data")
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    log_path = Path(data_dir) / "agentchattr_log.jsonl"
    legacy_log_path = Path(data_dir) / "room_log.jsonl"
    if not log_path.exists() and legacy_log_path.exists():
        # Backward compatibility for existing installs.
        log_path = legacy_log_path

    store = MessageStore(str(log_path))
    # Initialize store upload dir from config
    raw_upload_dir = cfg.get("images", {}).get("upload_dir", "./uploads")
    store.upload_dir = Path(raw_upload_dir)
    
    # Rules store — migrates from legacy decisions.json automatically
    rules_path = Path(data_dir) / "rules.json"
    legacy_decisions = Path(data_dir) / "decisions.json"
    if not rules_path.exists() and legacy_decisions.exists():
        legacy_decisions.rename(rules_path)
    rules = RuleStore(str(rules_path))
    rules.on_change(_on_rule_change)

    summaries = SummaryStore(str(Path(data_dir) / "summaries.json"))

    # Migrate legacy activities.json → jobs.json
    jobs_path = Path(data_dir) / "jobs.json"
    legacy_activities = Path(data_dir) / "activities.json"
    if not jobs_path.exists() and legacy_activities.exists():
        legacy_activities.rename(jobs_path)

    jobs = JobStore(str(jobs_path), history_next_id=store.next_job_id_from_history)
    jobs.on_change(_on_job_change)

    schedules = ScheduleStore(str(Path(data_dir) / "schedules.json"))
    schedules.on_change(_on_schedule_change)

    max_hops = cfg.get("routing", {}).get("max_agent_hops", 4)

    # Registry: single source of truth for all live agent state
    registry = RuntimeRegistry(data_dir=data_dir)
    registry.seed(cfg.get("agents", {}))
    registry.on_change(_on_registry_change)

    # Router starts with base agent names (backward compat for direct MCP users),
    # registry.on_change updates it dynamically when instances register/deregister
    agent_names = list(cfg.get("agents", {}).keys())
    router = Router(
        agent_names=agent_names,
        default_mention=cfg.get("routing", {}).get("default", "none"),
        max_hops=max_hops,
        online_checker=lambda: set(registry.get_active_names()) if registry else set(),
    )
    agents = AgentTrigger(registry, data_dir=data_dir)

    # Sessions
    ROOT = Path(__file__).parent
    session_store = SessionStore(
        str(Path(data_dir) / "session_runs.json"),
        templates_dir=str(ROOT / "session_templates"),
    )
    session_engine = SessionEngine(session_store, store, agents, registry)
    session_store.on_change(_on_session_change)

    # Bridge: when ANY message is added to store (including via MCP),
    # broadcast to all WebSocket clients
    store.on_message(_on_store_message)
    store.on_log_status(_on_log_status)

    _load_settings()
    _load_hats()

    # Apply saved loop guard setting
    if "max_agent_hops" in room_settings:
        router.max_hops = room_settings["max_agent_hops"]

    # Background thread: check for wrapper recovery flag files
    _data_dir = Path(data_dir)

    _known_online: set[str] = set()  # agents we've seen join — track for leave messages
    _posted_leave: set[str] = set()  # agents we've already posted a leave for — debounce

    _known_active = set()
    _known_needs: set[str] = set()

    import idle_shutdown
    _idle = idle_shutdown.from_config(cfg)

    def _background_checks():
        import time as _time
        import mcp_bridge

        while True:
            _time.sleep(3)
            # Recovery flags
            try:
                for flag in _data_dir.glob("*_recovered"):
                    agent_name = flag.read_text("utf-8").strip()
                    flag.unlink()
                    store.add(
                        "system",
                        f"Agent routing for {agent_name} interrupted — auto-recovered. "
                        "If agents aren't responding, try sending your message again."
                    )
            except Exception:
                pass

            # Pending instances (slot 2+) wait for human naming or agent claim.
            # No auto-confirm — identity must be explicitly resolved.

            # Presence expiry — post leave messages (but do NOT deregister).
            # Deregistration only happens via /api/deregister (wrapper shutdown)
            # OR the 60s crash timeout below.
            # Short timeout (10s) prevents slot theft when MCP tool calls are intermittent.
            try:
                now = _time.monotonic()
                with mcp_bridge._presence_lock:
                    currently_online = {
                        name for name, ts in mcp_bridge._presence.items()
                        if now - ts < mcp_bridge.PRESENCE_TIMEOUT
                    }
                    currently_active = set()
                    for name, active in mcp_bridge._activity.items():
                        if active:
                            if now - mcp_bridge._activity_ts.get(name, 0) < mcp_bridge.ACTIVITY_TIMEOUT:
                                currently_active.add(name)
                            else:
                                mcp_bridge._activity[name] = False  # auto-expire

                _posted_leave.update(_expire_crashed_agents(now))
                # A heartbeat may have refreshed both registration and presence
                # while the earlier snapshot was being checked for timeouts.
                with mcp_bridge._presence_lock:
                    currently_online = {
                        name for name, ts in mcp_bridge._presence.items()
                        if now - ts < mcp_bridge.PRESENCE_TIMEOUT
                    }
                registered = set(registry.get_all_names())

                # Detect registered instances going offline (leave message only)
                timed_out = registered - currently_online
                for name in timed_out:
                    inst = registry.get_instance(name)
                    if not inst:
                        continue
                    # Skip names that were just renamed (not actually offline)
                    with mcp_bridge._presence_lock:
                        was_renamed = name in mcp_bridge._renamed_from
                        if was_renamed:
                            mcp_bridge._renamed_from.discard(name)
                    if was_renamed:
                        continue
                    # Post leave message ONCE per offline transition (debounced)
                    if name not in _posted_leave:
                        _posted_leave.add(name)
                        store.add(name, f"{name} disconnected", msg_type="leave", channel=_agent_last_channel.get(name, _last_active_channel))

                # Clear leave debounce for agents that came back online
                _posted_leave.difference_update(currently_online)

                # Detect other agents (non-registered) going offline
                went_offline = (_known_online - currently_online) - timed_out
                for name in went_offline:
                    # Skip leave messages for names that were just renamed
                    with mcp_bridge._presence_lock:
                        was_renamed = name in mcp_bridge._renamed_from
                        if was_renamed:
                            mcp_bridge._renamed_from.discard(name)
                    if was_renamed:
                        continue
                    if not registry.is_registered(name) and name not in _posted_leave:
                        _posted_leave.add(name)
                        store.add(name, f"{name} disconnected", msg_type="leave", channel=_agent_last_channel.get(name, _last_active_channel))

                if _known_online != currently_online and _event_loop:
                    asyncio.run_coroutine_threadsafe(broadcast_status(), _event_loop)

                # Clear stale activity for agents that went offline
                with mcp_bridge._presence_lock:
                    stale_active = [n for n in mcp_bridge._activity
                                    if mcp_bridge._activity.get(n) and n not in currently_online]
                    for n in stale_active:
                        mcp_bridge._activity[n] = False
                    if stale_active:
                        currently_active -= set(stale_active)

                currently_needs = {n for n in currently_online if mcp_bridge.is_needs_input(n)}
                if currently_needs != _known_needs:
                    _known_needs.clear()
                    _known_needs.update(currently_needs)
                    if _event_loop:
                        asyncio.run_coroutine_threadsafe(broadcast_status(), _event_loop)

                # Broadcast status on any change (online set or activity set)
                if currently_active != _known_active or _known_online != currently_online:
                    _known_active.clear()
                    _known_active.update(currently_active)
                    if _event_loop:
                        asyncio.run_coroutine_threadsafe(broadcast_status(), _event_loop)
                _known_online.clear()
                _known_online.update(currently_online)

                # Idle auto-shutdown: no browser tabs and no agents connected.
                if _idle and _shutdown_hook:
                    agents_connected = len(set(registry.get_all_names()) | currently_online)
                    scheduled_work = schedules is not None and schedules.has_active()
                    if _idle.check(len(ws_clients), agents_connected, now, scheduled_work=scheduled_work):
                        log.info("Idle auto-shutdown: no browser or agents connected for %ss",
                                 int(_idle.grace_seconds))
                        _shutdown_hook()
                        return
            except Exception:
                pass

            try:
                agents.dispatch_proposal_writers()
            except OSError:
                log.exception("Could not dispatch pending proposal requests")

    threading.Thread(target=_background_checks, daemon=True).start()

    # --- Schedule runner: fires due scheduled prompts every 30s ---
    def _schedule_runner():
        import time as _time
        while True:
            _time.sleep(30)
            try:
                _dispatch_due_schedules()
            except Exception:
                log.exception("schedule runner error")

    threading.Thread(target=_schedule_runner, daemon=True).start()


def _expire_crashed_agents(now: float) -> set[str]:
    """Remove crashed registrations only if their identity and lease are still stale."""
    import mcp_bridge

    expired = set()
    cutoff = now - registry.HEARTBEAT_TIMEOUT
    for name, instance in registry.get_all().items():
        with mcp_bridge._presence_lock:
            last_presence = mcp_bridge._presence.get(name, 0)
        if not 0 < last_presence < cutoff:
            continue
        if not registry.deregister(name, reclaimable=True, identity_id=instance["identity_id"],
                                   last_seen_before=cutoff):
            continue
        if not mcp_bridge.purge_identity(name, require_unregistered=True):
            continue
        if registry.is_registered(name):
            continue
        registry.clean_renames_for(name)
        log.info("Crash timeout: deregistered %s (no heartbeat for %ss)",
                 name, registry.HEARTBEAT_TIMEOUT)
        store.add(name, f"{name} disconnected (timeout)", msg_type="leave",
                  channel=_agent_last_channel.get(name, _last_active_channel))
        expired.add(name)
    return expired


# --- Store → WebSocket bridge ---

_event_loop = None  # set by run.py after starting the event loop
_last_active_channel: str = "general"  # last channel any message was sent in
# Per-agent last channel: where each sender was most recently active. Used to
# route leave/disconnect messages to the channel that agent was talking in,
# instead of the global last-active channel (which is usually #general and made
# leave spam land in the wrong place).
_agent_last_channel: dict[str, str] = {}


def _migrate_agent_last_channel(old_name: str, new_name: str):
    """Re-key _agent_last_channel across an agent rename.

    Without this, a disconnect for the post-rename name misses the map and
    falls back to the global last-active channel — putting the leave message
    back in the wrong channel. Call this beside every migrate_identity /
    rename_sender path (slot-1 rename, renamed-back, websocket rename_agent,
    /api/label)."""
    if old_name == new_name:
        return
    ch = _agent_last_channel.pop(old_name, None)
    if ch is not None:
        _agent_last_channel[new_name] = ch


def set_event_loop(loop):
    global _event_loop
    _event_loop = loop


_shutdown_hook = None  # set by run.py; called once when the room goes idle


def set_shutdown_hook(fn):
    global _shutdown_hook
    _shutdown_hook = fn


def _on_log_status():
    if _event_loop is not None:
        asyncio.run_coroutine_threadsafe(_broadcast_log_status(), _event_loop)


async def _broadcast_log_status():
    await _broadcast(json.dumps({"type": "log_status", "data": store.log_status()}))


def _dispatch_due_schedules() -> None:
    if schedules is None:
        return
    for candidate in schedules.run_due():
        schedule_id = candidate["id"]
        try:
            with schedules.dispatch_due(schedule_id) as schedule:
                if schedule is None:
                    continue
                sender = schedule.get("created_by", "user")
                mentions = " ".join(f"@{target}" for target in schedule["targets"] if target)
                channel = schedule.get("channel", "general")
                _post_scheduled_message(schedule, sender, f"{mentions} {schedule['prompt']}", channel)
            if schedule.get("one_shot"):
                schedules.delete(schedule_id)
            else:
                schedules.mark_run(schedule_id)
        except Exception:
            log.exception("Could not dispatch schedule %s", schedule_id)


def _post_scheduled_message(schedule: dict, sender: str, text: str, channel: str) -> None:
    if schedule.get("one_shot"):
        delivery_uid = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"agentchattr:one-shot:{schedule['id']}:{schedule.get('created_at')}",
        ))
        store.add(sender, text, channel=channel, sender_kind="human", uid=delivery_uid, once=True)
    else:
        store.add(sender, text, channel=channel, sender_kind="human")


def _log_message_dispatch_result(future):
    if future.cancelled():
        return
    try:
        future.result()
    except Exception:
        log.exception("Chat message dispatch failed")


def _on_store_message(msg: dict):
    """Called from any thread when a message is added to the store."""
    if _event_loop is None:
        return
    try:
        # If called from the event loop thread (e.g. WebSocket handler),
        # schedule directly as a task
        loop = asyncio.get_running_loop()
        if loop is _event_loop:
            task = asyncio.ensure_future(_handle_new_message(msg))
            task.add_done_callback(_log_message_dispatch_result)
            return
    except RuntimeError:
        pass  # No running loop — we're in a different thread (MCP)
    task = asyncio.run_coroutine_threadsafe(_handle_new_message(msg), _event_loop)
    task.add_done_callback(_log_message_dispatch_result)


def _on_rule_change(action: str, rule: dict):
    """Called from any thread when a rule changes."""
    if _event_loop is None:
        return
    try:
        loop = asyncio.get_running_loop()
        if loop is _event_loop:
            asyncio.ensure_future(broadcast_rule(action, rule))
            return
    except RuntimeError:
        pass
    asyncio.run_coroutine_threadsafe(broadcast_rule(action, rule), _event_loop)


def _on_job_change(action: str, data: dict):
    """Called from any thread when a job changes."""
    if _event_loop is None:
        return
    try:
        loop = asyncio.get_running_loop()
        if loop is _event_loop:
            asyncio.ensure_future(broadcast_job(action, data))
            return
    except RuntimeError:
        pass
    asyncio.run_coroutine_threadsafe(broadcast_job(action, data), _event_loop)


def _on_schedule_change(action: str, schedule: dict):
    """Called from any thread when a schedule changes."""
    if _event_loop is None:
        return
    try:
        loop = asyncio.get_running_loop()
        if loop is _event_loop:
            asyncio.ensure_future(broadcast_schedule(action, schedule))
            return
    except RuntimeError:
        pass
    asyncio.run_coroutine_threadsafe(broadcast_schedule(action, schedule), _event_loop)


def _on_session_change(action: str, session: dict):
    """Called from any thread when a session changes."""
    if _event_loop is None:
        return
    # Enrich with computed fields so the frontend gets phase_name, current_agent, etc.
    if session_engine:
        session = session_engine._enrich(dict(session))

    # Add completion/interruption banners to chat timeline
    if action == "complete" and store:
        output_id = session.get("output_message_id")
        # Tag the output message so it renders highlighted on reload
        if output_id:
            msg = store.get_by_id(output_id)
            if msg:
                meta = msg.get("metadata") or {}
                meta["session_output"] = True
                store.update_message(output_id, {"metadata": meta})
        store.add(
            sender="system",
            text=f"Session complete: {session.get('template_name', '?')}",
            msg_type="session_end",
            channel=session.get("channel", "general"),
            metadata={"session_id": session.get("id"), "output_message_id": output_id},
        )
    elif action == "interrupt" and store:
        reason = session.get("interrupt_reason", "interrupted")
        store.add(
            sender="system",
            text=f"Session ended: {session.get('template_name', '?')} ({reason})",
            msg_type="session_end",
            channel=session.get("channel", "general"),
            metadata={"session_id": session.get("id"), "reason": reason},
        )

    try:
        loop = asyncio.get_running_loop()
        if loop is _event_loop:
            asyncio.ensure_future(broadcast_session(action, session))
            return
    except RuntimeError:
        pass
    asyncio.run_coroutine_threadsafe(broadcast_session(action, session), _event_loop)


_draft_ref_re = _re.compile(r'\[([a-f0-9]{8})\]')

def _resolve_draft_lineage(text: str, channel: str) -> tuple[str, int]:
    """Check if a session draft block is a revision of an existing draft.

    Looks at the agent's own message text for a [draft_id] reference, and also
    scans recent channel messages for "revise session draft [XXXX]" requests.
    Returns (draft_id, revision). New drafts get a fresh id and revision=1.
    """
    # Check the message text itself for a draft_id reference
    ref_match = _draft_ref_re.search(text)
    ref_id = ref_match.group(1) if ref_match else None

    if not ref_id:
        # Also check recent messages for a "revise session draft [XXXX]" request
        recent = store.get_recent(count=20, channel=channel)
        for m in reversed(recent):
            m_text = m.get("text", "")
            if "revise session draft" in m_text.lower():
                ref_match = _draft_ref_re.search(m_text)
                if ref_match:
                    ref_id = ref_match.group(1)
                    break

    if ref_id:
        # Find the highest revision for this draft_id in existing messages
        max_rev = 0
        recent = store.get_recent(count=100, channel=channel)
        for m in recent:
            meta = m.get("metadata") or {}
            if meta.get("draft_id") == ref_id:
                max_rev = max(max_rev, meta.get("revision", 1))
        if max_rev > 0:
            return ref_id, max_rev + 1

    return str(uuid.uuid4())[:8], 1


# Active ping requests are scoped by message ID; only an explicit OK reply
# from the requested agent in the same channel satisfies a request.
_pending_pings: dict[int, tuple[str, set[str]]] = {}
_ping_tasks: set[asyncio.Task] = set()


def _record_ping_reply(msg: dict):
    pending = _pending_pings.get(msg.get("reply_to"))
    if not pending or msg.get("type", "chat") != "chat" or msg.get("sender_kind") != "agent":
        return
    channel, targets = pending
    if msg.get("channel", "general") == channel and msg.get("text", "").strip() == "OK":
        targets.discard(msg.get("sender", "").lower())


async def _ping_timeout(message_id: int, delay: float = 30):
    try:
        await asyncio.sleep(delay)
        pending = _pending_pings.pop(message_id, None)
        if pending:
            channel, targets = pending
            for target in sorted(targets):
                store.add("system", f"{target} is not available (no reply within 30 seconds).",
                          msg_type="system", channel=channel, reply_to=message_id)
    finally:
        _pending_pings.pop(message_id, None)


async def _handle_new_message(msg: dict):
    """Broadcast message to web clients + check for @mention triggers."""
    # For broadcast slash commands, suppress the raw message — only the expanded
    # version should appear. Delete from store if it was persisted (MCP path),
    # and skip broadcasting the raw text.
    text = msg.get("text", "")
    msg_type = msg.get("type", "chat")
    sender = msg.get("sender", "")
    channel = msg.get("channel", "general")
    sender_kind = msg.get("sender_kind")
    sender_identity = msg.get("sender_identity")
    sender_base = msg.get("sender_base")
    sender_is_agent = sender_kind == "agent"
    if sender_kind not in ("human", "agent"):
        await broadcast(msg)
        return

    _record_ping_reply(msg)

    # Track last active channel for leave/join messages (skip system messages)
    global _last_active_channel
    if _unarchive_for_activity(channel, msg_type):
        await broadcast_settings()
    if msg_type not in ("system", "leave", "join"):
        _last_active_channel = channel
        # Remember where this specific sender was last active so their
        # disconnect message follows them to the right channel.
        if sender_is_agent:
            _agent_last_channel[sender] = channel
    # Strip @mentions to find the slash command (e.g. "@claude @codex /hatmaking")
    stripped = _re.sub(r"@[\w-]+\s*", "", text).strip().lower()
    _broadcast_cmds = ("/hatmaking", "/artchallenge", "/roastreview", "/poetry")
    cmd_word = stripped.split()[0] if stripped else ""
    is_broadcast_cmd = cmd_word in _broadcast_cmds
    _session_draft_re = _re.compile(r'```session\s*\n(.*?)\n```', _re.DOTALL)
    draft_match = _session_draft_re.search(text) if sender_is_agent else None
    is_hidden_session_request = msg_type == "session_request"

    is_agent_continue = stripped == "/continue" and sender_is_agent
    suppress_broadcast = (
        is_broadcast_cmd
        or is_hidden_session_request
        or draft_match is not None
        or is_agent_continue
    )

    if not suppress_broadcast:
        await broadcast(msg)

    # If the raw slash command was persisted (MCP path), silently remove it.
    # It was never broadcast to WebSocket clients, so no delete event needed.
    if suppress_broadcast and msg.get("id"):
        store.delete([msg["id"]])

    # Summary cards are channel history, not new requests to their mentions.
    if msg_type == "summary":
        return
    workflow = (msg.get("metadata") or {}).get("orch_workflow")
    if workflow and workflow.get("closed"):
        return

    # Check for slash commands — use stripped text (sans @mentions)
    if stripped == "/continue":
        if sender_is_agent:
            store.add("system", f"Loop guard: only humans can /continue. {sender} tried to self-resume.", channel=channel)
            return
        router.continue_routing(channel)
        store.add("system", f"Routing resumed by {sender}.", channel=channel)
        await broadcast_status()
        return

    if stripped == "/roastreview":
        agent_names = registry.get_all_names() if registry else list(config.get("agents", {}).keys())
        mentions = " ".join(f"@{a}" for a in agent_names)
        store.add(sender, f"{mentions} Time for a roast review! Inspect each other's work and constructively roast it.",
                  channel=channel, sender_kind=sender_kind, sender_identity=sender_identity,
                  sender_base=sender_base)
        return

    if stripped.startswith("/artchallenge"):
        parts = stripped.split(None, 1)
        theme = parts[1] if len(parts) > 1 else "anything you like"
        agent_names = registry.get_all_names() if registry else list(config.get("agents", {}).keys())
        mentions = " ".join(f"@{a}" for a in agent_names)
        store.add(
            sender,
            f"{mentions} Art challenge! Create an SVG artwork with the theme: **{theme}**. "
            "Write your SVG code to a .svg file, then attach it using chat_send(image_path=...). "
            "Make it creative, keep it under 5KB. Let's see what you've got!",
            channel=channel,
            sender_kind=sender_kind,
            sender_identity=sender_identity,
            sender_base=sender_base,
        )
        return

    if stripped == "/hatmaking":
        agent_names = registry.get_all_names() if registry else list(config.get("agents", {}).keys())
        mentions = " ".join(f"@{a}" for a in agent_names)
        all_instances = registry.get_all() if registry else {}
        agents_cfg = config.get("agents", {})
        color_parts = ", ".join(
            f"{a}={all_instances[a]['color']}" if a in all_instances
            else f"{a}={agents_cfg.get(a, {}).get('color', '#888')}"
            for a in agent_names
        )
        store.add(
            sender,
            f"{mentions} Hat making time! Design a new hat for your avatar using SVG. "
            "Use viewBox=\"0 0 32 16\" so it fits on top of a 32px avatar circle. "
            f"Background is dark (#0f0f17). Avatar colors: {color_parts}. Design for good contrast! "
            "Call chat_set_hat(sender=your_name, svg='<svg ...>...</svg>') to wear it. "
            "Be creative — top hats, party hats, crowns, propeller beanies, whatever you want!",
            channel=channel,
            sender_kind=sender_kind,
            sender_identity=sender_identity,
            sender_base=sender_base,
        )
        return

    if stripped.startswith("/poetry"):
        parts = stripped.split(None, 1)
        form = parts[1] if len(parts) > 1 else "haiku"
        if form not in ("haiku", "limerick", "sonnet"):
            form = "haiku"
        agent_names = registry.get_all_names() if registry else list(config.get("agents", {}).keys())
        mentions = " ".join(f"@{a}" for a in agent_names)
        prompts = {
            "haiku": "Write a haiku about the current state of this codebase.",
            "limerick": "Write a limerick about the current state of this codebase.",
            "sonnet": "Write a sonnet about the current state of this codebase.",
        }
        store.add(sender, f"{mentions} {prompts[form]}", channel=channel,
                  sender_kind=sender_kind, sender_identity=sender_identity, sender_base=sender_base)
        return

    # Detect session draft blocks from agents only.
    # The session request prompt contains an example ```session block,
    # so treating every non-system sender as a draft source creates a false
    # invalid-draft card the moment the user asks for a custom session.
    if draft_match:
        # Check if this is a revision of an existing draft
        draft_id, revision = _resolve_draft_lineage(text, channel)

        try:
            draft_json = json.loads(draft_match.group(1))
            errors = validate_session_template(draft_json)
            if errors:
                store.add(
                    "system",
                    f"Session draft from {sender} has errors:\n" + "\n".join(f"- {e}" for e in errors),
                    msg_type="session_draft",
                    channel=channel,
                    metadata={"draft_id": draft_id, "revision": revision, "proposed_by": sender,
                              "template": draft_json, "errors": errors, "valid": False},
                )
            else:
                draft_json.setdefault("id", f"draft-{draft_id}")
                store.add(
                    "system",
                    f"Session draft from {sender}: **{draft_json.get('name', '?')}**",
                    msg_type="session_draft",
                    channel=channel,
                    metadata={"draft_id": draft_id, "revision": revision, "proposed_by": sender,
                              "template": draft_json, "errors": [], "valid": True},
                )
        except json.JSONDecodeError:
            store.add(
                "system",
                f"Session draft from {sender} contains invalid JSON.",
                msg_type="session_draft",
                channel=channel,
                metadata={"draft_id": draft_id, "revision": revision, "proposed_by": sender,
                           "errors": ["Invalid JSON in session block"], "valid": False},
            )

    recipient = workflow and workflow.get("recipient")
    if recipient and (not sender_is_agent or workflow["prompt_id"] == msg.get("id")):
        raw_targets = [recipient]
    else:
        raw_targets = router.get_targets(
            text, channel, sender_is_agent=sender_is_agent,
            allow_default=not workflow and not (msg_type == "chat" and _has_orch_mention(text)),
        )
    # Resolve base family names to actual registered instances
    # e.g. 'claude' → 'claude-prime' when slot-1 was renamed
    targets = []
    for t in raw_targets:
        if t == "orch":
            continue
        if registry:
            targets.extend(registry.resolve_to_instances(t))
        else:
            targets.append(t)
    targets = list(dict.fromkeys(targets))  # dedupe, preserve order
    decision_author_id = (msg.get("metadata") or {}).get("decision_author_identity")
    if decision_author_id and registry:
        author = registry.get_active_instance_by_identity(decision_author_id)
        targets = [author["name"]] if author else []

    if _re.match(r"^\s*/ping(?:\s|$)", text, _re.IGNORECASE) and not sender_is_agent:
        if not targets:
            store.add("system", "Ping requires an available agent target or @all.",
                      msg_type="system", channel=channel, reply_to=msg.get("id"))
            return
        if msg.get("id") is not None:
            _pending_pings[msg["id"]] = (channel, {t.lower() for t in targets})
            task = asyncio.create_task(_ping_timeout(msg["id"]))
            _ping_tasks.add(task)
            task.add_done_callback(_ping_tasks.discard)

    if router.is_paused(channel):
        # Only emit the loop guard notice once per pause
        if not router.is_guard_emitted(channel):
            router.set_guard_emitted(channel)
            store.add(
                "system",
                f"Loop guard: {router.max_hops} agent-to-agent hops reached. "
                "Type /continue to resume.",
                channel=channel
            )
        return

    # Build a readable message string for the wake prompt
    chat_msg = f"{sender}: {text}" if text else ""
    custom_prompt = text if is_hidden_session_request else ""

    # Session turn guard: if a session is active on this channel and the sender
    # is an agent, only allow triggering the agent whose turn it is.
    # Human @mentions are always allowed (the session engine handles pausing).
    allowed = session_engine.get_allowed_wake_target(channel) if session_engine and sender_is_agent else None
    target_identity = decision_author_id or (allowed[1] if allowed else None)

    import mcp_bridge
    for target in targets:
        # Skip pending instances — they haven't been named/claimed yet
        if registry:
            inst = registry.get_instance(target)
            if inst and inst.get("state") == "pending":
                continue
            if inst and inst["identity_id"] == sender_identity:
                continue
            if target_identity and (not inst or inst["identity_id"] != target_identity):
                continue
        # Session guard: suppress out-of-turn agent triggers
        if allowed is not None and target != allowed[0]:
            continue
        if not agents.is_available(target):
            continue
        try:
            await agents.trigger(target, message=chat_msg, channel=channel, prompt=custom_prompt,
                                 **({"identity_id": target_identity} if target_identity else {}))
        except OSError:
            log.exception("Failed to queue wake for %s in #%s", target, channel)
            store.add("system", f"Could not queue a wake for @{target}.",
                      msg_type="system", channel=channel, reply_to=msg.get("id"))
            continue
        if not mcp_bridge.is_online(target):
            store.add("system", f"{target} appears offline — message queued.", msg_type="system", channel=channel)


# --- broadcasting ---

_WS_SEND_TIMEOUT = 5
_WS_MAX_PENDING = 64
_ws_writers: dict[WebSocket, tuple[deque[str], asyncio.Task]] = {}


class WebSocketSendTimeout(TimeoutError):
    pass


async def _send_ws_frame(client: WebSocket, payload: str):
    try:
        async with asyncio.timeout(_WS_SEND_TIMEOUT):
            await client.send_text(payload)
    except TimeoutError as exc:
        raise WebSocketSendTimeout from exc


async def _close_slow_websocket(client: WebSocket):
    try:
        async with asyncio.timeout(_WS_SEND_TIMEOUT):
            await client.close(code=1013, reason="WebSocket send backlog")
    except (TimeoutError, WebSocketDisconnect, OSError, RuntimeError):
        pass


async def _write_ws_frames(client: WebSocket, pending: deque[str]):
    try:
        while pending and client in ws_clients:
            await _send_ws_frame(client, pending.popleft())
    except WebSocketSendTimeout:
        ws_clients.discard(client)
        await _close_slow_websocket(client)
    except Exception:
        ws_clients.discard(client)
    finally:
        _ws_writers.pop(client, None)


async def _broadcast(raw_json: str):
    """Queue an ordered broadcast independently for each connected client."""
    for client in tuple(ws_clients):
        writer = _ws_writers.get(client)
        if writer is None:
            pending = deque([raw_json])
            _ws_writers[client] = (pending, asyncio.create_task(_write_ws_frames(client, pending)))
            continue
        pending, task = writer
        if len(pending) >= _WS_MAX_PENDING:
            ws_clients.discard(client)
            _ws_writers.pop(client)
            task.cancel()
            asyncio.create_task(_close_slow_websocket(client))
        else:
            pending.append(raw_json)
    await asyncio.sleep(0)


async def broadcast(msg: dict):
    data = json.dumps({"type": "message", "data": msg})
    await _broadcast(data)


async def broadcast_status():
    status = agents.get_status()
    status["paused"] = any(router.is_paused(ch) for ch in room_settings.get("channels", ["general"]))
    data = json.dumps({"type": "status", "data": status})
    await _broadcast(data)


async def broadcast_typing(agent_name: str, is_typing: bool):
    data = json.dumps({"type": "typing", "agent": agent_name, "active": is_typing})
    await _broadcast(data)


async def broadcast_clear(channel: str | None = None):
    payload = {"type": "clear"}
    if channel:
        payload["channel"] = channel
    data = json.dumps(payload)
    await _broadcast(data)


async def broadcast_todo_update(msg_id: int, status: str | None):
    payload = {"type": "todo_update", "data": {"id": msg_id, "status": status}}
    if status is not None:
        summary = store.get_todo_summary(msg_id)
        if summary:
            payload["message"] = summary
    data = json.dumps(payload)
    await _broadcast(data)


async def broadcast_settings():
    data = json.dumps({"type": "settings", "data": _settings_payload()})
    await _broadcast(data)


async def broadcast_rule(action: str, rule: dict):
    data = json.dumps({"type": "rule", "action": action, "data": rule})
    await _broadcast(data)


async def broadcast_job(action: str, data: dict):
    if action == "message" and jobs is not None:
        # Job-thread messages never pass through _handle_new_message, but they are activity too. Every
        # path (web UI, MCP, API) fires this callback, so un-archive the job's channel here.
        job = jobs.get(data.get("job_id"))
        message = data.get("message") or {}
        if job and _unarchive_for_activity(
            job.get("channel", ""), message.get("type", "chat"), message.get("sender", "")
        ):
            await broadcast_settings()
    payload = json.dumps({"type": "job", "action": action, "data": data})
    await _broadcast(payload)


async def broadcast_schedule(action: str, schedule: dict):
    payload = json.dumps({"type": "schedule", "action": action, "data": schedule})
    await _broadcast(payload)


async def broadcast_session(action: str, session: dict):
    payload = json.dumps({"type": "session", "action": action, "data": session})
    await _broadcast(payload)


async def broadcast_hats():
    data = json.dumps({"type": "hats", "data": agent_hats})
    await _broadcast(data)


async def broadcast_agents():
    """Send updated agent config (from registry) to all WebSocket clients."""
    agent_cfg = registry.get_agent_config() if registry else {}
    data = json.dumps({"type": "agents", "data": agent_cfg})
    await _broadcast(data)


def _on_registry_change():
    """Called from registry (any thread) when instances register/deregister/claim/rename."""
    # Update router with current agent names (base names + registered instances)
    with _routing_refresh_lock:
        if router and registry:
            base_names = list(registry.get_bases())
            # Only include active instances in routing (pending ones are inert).
            instance_names = registry.get_active_names()
            router.update_agents(base_names + instance_names)
    # Broadcast to WebSocket clients
    if _event_loop:
        asyncio.run_coroutine_threadsafe(broadcast_agents(), _event_loop)
        asyncio.run_coroutine_threadsafe(broadcast_status(), _event_loop)


# --- WebSocket ---

async def _initialize_websocket(websocket: WebSocket):
    await _send_ws_frame(websocket, json.dumps({"type": "log_status", "data": store.log_status()}))

    # Send settings
    await _send_ws_frame(websocket, json.dumps({"type": "settings", "data": _settings_payload()}))

    # Send registered instances (used for pills/mentions)
    agent_cfg = registry.get_agent_config() if registry else {}
    await _send_ws_frame(websocket, json.dumps({"type": "agents", "data": agent_cfg}))

    # Send base agent colors (used for message coloring, no pills)
    base_colors = {}
    for name, cfg in config.get("agents", {}).items():
        base_colors[name] = {"color": cfg.get("color", "#888"), "label": cfg.get("label", name)}
    await _send_ws_frame(websocket, json.dumps({"type": "base_colors", "data": base_colors}))

    # Send todos {msg_id: status}
    # "data" keeps the {id: status} shape older clients expect; "messages" carries the
    # pinned messages' text/sender/time/channel so the panel doesn't depend on the DOM
    # (messages outside the loaded history window aren't rendered there).
    await _send_ws_frame(websocket, json.dumps({
        "type": "todos", "data": store.get_todos(), "messages": store.get_todo_summaries(),
    }))

    # Send rules
    await _send_ws_frame(websocket, json.dumps({"type": "rules", "data": rules.list_all()}))

    # Send hats
    await _send_ws_frame(websocket, json.dumps({"type": "hats", "data": agent_hats}))

    # Send jobs
    await _send_ws_frame(websocket, json.dumps({"type": "jobs", "data": jobs.list_all()}))

    # Send schedules
    await _send_ws_frame(websocket, json.dumps({"type": "schedules", "data": schedules.list_all()}))

    # Send pending instances (so late-connecting browsers still see the naming lightbox)
    if registry:
        for inst in registry.get_all().values():
            if inst.get("state") == "pending":
                await _send_ws_frame(websocket, json.dumps({
                    "type": "pending_instance",
                    "name": inst["name"],
                    "base": inst.get("base", ""),
                    "label": inst.get("label", inst["name"]),
                    "color": inst.get("color", "#888"),
                }))

    # Send history (per channel based on history_limit)
    limit_val = room_settings.get("history_limit", 50)
    count = 10000 if limit_val == "all" else int(limit_val)
    
    history = []
    has_more = {}
    for ch in room_settings["channels"]:
        recent = store.get_recent(count, channel=ch)
        history.extend(recent)
        # More exists if the channel holds more messages than were just sent.
        has_more[ch] = store.count_channel(ch) > len(recent)
    
    # Sort history by timestamp to interleave messages from different channels correctly
    history.sort(key=lambda m: m.get("timestamp", 0))
    
    # Saved messages have their own boundary; presence updates can arrive at
    # any time and must never make old history look like new chat.
    for offset in range(0, len(history), 100):
        await _send_ws_frame(websocket, json.dumps({
            "type": "history", "messages": history[offset:offset + 100],
        }))
        await asyncio.sleep(0)
    await _send_ws_frame(websocket, json.dumps({"type": "history_complete", "has_more": has_more}))

    # Send status
    await broadcast_status()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # --- Security: validate session token on WebSocket connect ---
    token = websocket.query_params.get("token", "")
    if token != session_token or not is_local_client(websocket.client.host if websocket.client else ""):
        # Must accept before closing so the browser receives the close frame.
        # Code 4003 triggers an auto-reload in the client to pick up the new token.
        await websocket.accept()
        await websocket.close(code=4003, reason="forbidden: invalid session token")
        return

    await websocket.accept()
    ws_clients.add(websocket)
    try:
        await _initialize_websocket(websocket)
        while True:
            raw = await websocket.receive_text()
            event = json.loads(raw)

            if event.get("type") == "message":
                text = event.get("text", "").strip()
                attachments = event.get("attachments", [])
                sender = event.get("sender") or room_settings.get("username", "user")
                channel = event.get("channel", "general")

                if not text and not attachments:
                    continue

                # Command handling
                if text.startswith("/"):
                    cmd_parts = text.split()
                    cmd = cmd_parts[0].lower()
                    if cmd == "/clear":
                        store.clear(channel=channel)
                        await broadcast_clear(channel=channel)
                        continue
                    if cmd == "/continue":
                        router.continue_routing(channel)
                        store.add("system", "Resuming agent conversation...", msg_type="system", channel=channel)
                        await broadcast_status()
                        continue
                    # Broadcast slash commands — expand without storing the raw command.
                    # _handle_new_message will store the expanded version.
                    if cmd in ("/hatmaking", "/artchallenge", "/roastreview", "/poetry"):
                        await _handle_new_message({"sender": sender, "text": text, "channel": channel,
                                                   "sender_kind": "human"})
                        continue

                # Store message — the on_message callback handles broadcast + triggers
                reply_to = event.get("reply_to")
                if reply_to is not None:
                    reply_to = int(reply_to)

                try:
                    metadata = orch_reply_metadata(jobs, store, channel, reply_to)
                except ValueError as exc:
                    await _send_ws_frame(websocket, json.dumps({"type": "message_error", "error": str(exc)}))
                    continue
                if metadata:
                    sender = room_settings.get("username", "user")
                message = store.add(sender, text, attachments=attachments, reply_to=reply_to,
                                    channel=channel, metadata=metadata, sender_kind="human")
                try:
                    record_orch_reply(jobs, message)
                    _offer_orch_intake(message)
                except OSError:
                    log.exception("Could not persist Orch history or intake for message %s", message["id"])

            elif event.get("type") == "delete":
                ids = event.get("ids", [])
                if ids:
                    deleted = store.delete([int(i) for i in ids])
                    if deleted:
                        data = json.dumps({"type": "delete", "ids": deleted})
                        await _broadcast(data)
                continue

            elif event.get("type") == "todo_add":
                msg_id = event.get("id")
                if msg_id is not None:
                    store.add_todo(int(msg_id))
                    await broadcast_todo_update(int(msg_id), "todo")
                continue

            elif event.get("type") == "todo_toggle":
                msg_id = event.get("id")
                if msg_id is not None:
                    mid = int(msg_id)
                    status = store.get_todo_status(mid)
                    if status == "todo":
                        store.complete_todo(mid)
                        await broadcast_todo_update(mid, "done")
                    elif status == "done":
                        store.reopen_todo(mid)
                        await broadcast_todo_update(mid, "todo")
                continue

            elif event.get("type") == "todo_remove":
                msg_id = event.get("id")
                if msg_id is not None:
                    store.remove_todo(int(msg_id))
                    await broadcast_todo_update(int(msg_id), None)
                continue

            elif event.get("type") in ("decision_propose", "rule_propose"):
                text = event.get("text") or event.get("decision", "")
                text = text.strip()
                author = event.get("author") or event.get("owner") or room_settings.get("username", "user")
                reason = event.get("reason", "")
                is_human = author.lower() == room_settings.get("username", "user").lower()
                if text:
                    rule = rules.propose(text, author, reason)
                    if rule:
                        if is_human:
                            # Human-created rules go straight to draft, no card
                            rules.make_draft(rule["id"])
                        else:
                            # Agent proposals get a card in the timeline
                            channel = event.get("channel", "general")
                            msg = store.add(
                                author, f"Rule proposal: {text}",
                                msg_type="rule_proposal",
                                channel=channel,
                                metadata={"rule_id": rule["id"], "text": text, "status": "pending"},
                                sender_kind="human",
                            )
                            # store.add() fires _on_store_message → broadcast already.
                            # Do NOT call broadcast(msg) again here.
                continue

            elif event.get("type") in ("decision_approve", "rule_activate"):
                rid = event.get("id")
                if rid is not None:
                    if not rules.activate(int(rid)):
                        await _send_ws_frame(websocket, json.dumps({
                            "type": "rule_error", "error": "Rule no longer exists. Refresh the page.",
                        }))
                continue

            elif event.get("type") in ("decision_unapprove", "rule_deactivate"):
                rid = event.get("id")
                if rid is not None:
                    rules.deactivate(int(rid))
                continue

            elif event.get("type") == "rule_make_draft":
                rid = event.get("id")
                if rid is not None:
                    rules.make_draft(int(rid))
                continue

            elif event.get("type") in ("decision_edit", "rule_edit"):
                rid = event.get("id")
                if rid is not None:
                    rules.edit(
                        int(rid),
                        text=event.get("text") or event.get("decision"),
                        reason=event.get("reason"),
                    )
                continue

            elif event.get("type") in ("decision_delete", "rule_delete"):
                rid = event.get("id")
                if rid is not None:
                    rules.delete(int(rid))
                continue

            elif event.get("type") == "rule_remind":
                rules.set_remind()
                remind_data = json.dumps({"type": "rules_remind", "data": {}})
                await _broadcast(remind_data)
                continue

            elif event.get("type") == "load_earlier":
                # Page older messages for one channel: {channel, before_id, limit}.
                ch = event.get("channel")
                try:
                    before_id = int(event.get("before_id"))
                    limit = max(1, min(int(event.get("limit") or 50), 200))
                except (TypeError, ValueError):
                    continue
                if ch not in room_settings["channels"]:
                    continue
                older, more = store.get_before(before_id, limit, channel=ch)
                await _send_ws_frame(websocket, json.dumps({
                    "type": "history_earlier", "channel": ch, "messages": older, "has_more": more,
                }))

            elif event.get("type") == "agent_color_changes":
                changes = event.get("changes")
                request_id = event.get("request_id")
                if not isinstance(changes, dict) or type(request_id) is not int:
                    continue
                ack = {"type": "agent_colors_saved", "request_id": request_id}
                try:
                    colors = _save_agent_color_changes(changes, event.get("seed"))
                except OSError:
                    log.exception("Could not save agent colors")
                    await websocket.send_text(json.dumps(ack))
                    continue
                await websocket.send_text(json.dumps({
                    **ack, "data": colors, "revision": room_settings["agent_colors_revision"],
                }))
                await broadcast_settings()

            elif event.get("type") == "update_settings":
                new = event.get("data", {})
                if "title" in new and isinstance(new["title"], str):
                    room_settings["title"] = new["title"].strip() or "agentchattr"
                apply_room_subtitle(new)
                if "username" in new and isinstance(new["username"], str):
                    room_settings["username"] = new["username"].strip() or "user"
                if "font" in new and new["font"] in ("mono", "serif", "sans"):
                    room_settings["font"] = new["font"]
                if _valid_chat_font_size(new.get("font_size")):
                    room_settings["font_size"] = new["font_size"]
                if "max_agent_hops" in new:
                    try:
                        hops = int(new["max_agent_hops"])
                        hops = max(1, min(hops, 50))
                        room_settings["max_agent_hops"] = hops
                        router.max_hops = hops
                    except (ValueError, TypeError):
                        pass
                if "contrast" in new and new["contrast"] in ("normal", "high"):
                    room_settings["contrast"] = new["contrast"]
                if isinstance(new.get("ui_prefs"), dict):
                    room_settings["ui_prefs"] = _clean_ui_prefs(new["ui_prefs"])
                if "agent_colors" not in room_settings and isinstance(new.get("agent_colors"), dict):
                    room_settings["agent_colors"] = _clean_agent_colors(new["agent_colors"])
                    room_settings["agent_colors_revision"] = room_settings.get("agent_colors_revision", 0) + 1
                if "rules_refresh_interval" in new:
                    try:
                        ri = int(new["rules_refresh_interval"])
                        room_settings["rules_refresh_interval"] = max(0, min(ri, 100))
                    except (ValueError, TypeError):
                        pass
                if "history_limit" in new:
                    val = str(new["history_limit"]).strip().lower()
                    if val == "all":
                        room_settings["history_limit"] = "all"
                    else:
                        try:
                            val_int = int(val)
                            room_settings["history_limit"] = max(1, min(val_int, 10000))
                        except (ValueError, TypeError):
                            pass
                if "inactive_hours" in new:
                    hours = _clean_inactive_hours(new["inactive_hours"])
                    if hours is not None:
                        room_settings["inactive_hours"] = hours
                if "custom_roles" in new and isinstance(new["custom_roles"], list):
                    room_settings["custom_roles"] = [
                        str(r).strip()[:20] for r in new["custom_roles"]
                        if isinstance(r, str) and r.strip()
                    ][:20]
                _save_settings()
                await broadcast_settings()

            elif event.get("type") == "rename_agent":
                agent_name = (event.get("name") or "").strip()
                new_label = (event.get("label") or "").strip()
                if agent_name and new_label and registry:
                    new_id = sanitize_chat_id(new_label) or agent_name
                    if new_id == agent_name:
                        # Same ID — label-only change
                        registry.set_label(agent_name, new_label)
                    else:
                        result = registry.rename(agent_name, new_id, new_label)
                        if isinstance(result, str):
                            # Rename failed (collision etc.) — fall back to label-only
                            registry.set_label(agent_name, new_label)
                        else:
                            # Migrate presence + cursors to new name
                            import mcp_bridge
                            mcp_bridge.migrate_identity(agent_name, new_id)
                            # Update sender on all historical messages
                            store.rename_sender(agent_name, new_id, result["identity_id"])
                            _migrate_agent_last_channel(agent_name, new_id)
                            # Notify clients so they can update sender in DOM
                            rename_event = json.dumps({
                                "type": "agent_renamed",
                                "old_name": agent_name,
                                "new_name": new_id,
                            })
                            await _broadcast(rename_event)
                continue

            elif event.get("type") == "name_pending":
                # Human names a pending instance (from lightbox)
                agent_name = (event.get("name") or "").strip()
                new_label = (event.get("label") or "").strip()
                if agent_name and registry:
                    if not new_label:
                        # Accept default name
                        registry.confirm_pending(agent_name)
                    else:
                        new_id = sanitize_chat_id(new_label) or agent_name
                        if new_id == agent_name:
                            # Same ID — just update label and confirm
                            registry.set_label(agent_name, new_label)
                            registry.confirm_pending(agent_name)
                        else:
                            result = registry.rename(agent_name, new_id, new_label)
                            if isinstance(result, str):
                                # Rename failed — just confirm with label
                                registry.set_label(agent_name, new_label)
                                registry.confirm_pending(agent_name)
                            else:
                                # Rename succeeded — confirm new name
                                registry.confirm_pending(new_id)
                                import mcp_bridge
                                mcp_bridge.migrate_identity(agent_name, new_id)
                                # Update sender on all historical messages
                                store.rename_sender(agent_name, new_id, result["identity_id"])
                                _migrate_agent_last_channel(agent_name, new_id)
                                rename_event = json.dumps({
                                    "type": "agent_renamed",
                                    "old_name": agent_name,
                                    "new_name": new_id,
                                })
                                await _broadcast(rename_event)
                continue

            elif event.get("type") == "channel_create":
                name = (event.get("name") or "").strip().lower()
                if not name or not _CHANNEL_NAME_RE.match(name):
                    continue
                if name in room_settings["channels"]:
                    continue
                room_settings["channels"].append(name)
                _save_settings()
                await broadcast_settings()

            elif event.get("type") == "channel_rename":
                old_name = (event.get("old_name") or "").strip().lower()
                new_name = (event.get("new_name") or "").strip().lower()
                if old_name == "general":
                    continue
                if not new_name or not _CHANNEL_NAME_RE.match(new_name):
                    continue
                if old_name not in room_settings["channels"]:
                    continue
                if new_name in room_settings["channels"]:
                    continue
                _rename_channel_references(old_name, new_name)
                idx = room_settings["channels"].index(old_name)
                room_settings["channels"][idx] = new_name
                room_settings["archived_channels"] = [
                    new_name if c == old_name else c
                    for c in room_settings.get("archived_channels", [])
                ]
                store.rename_channel(old_name, new_name)
                import mcp_bridge
                mcp_bridge.migrate_cursors_rename(old_name, new_name)
                _save_settings()
                # Keep the rename and its channel list in one event, including during history replay.
                await _broadcast(json.dumps({
                    "type": "channel_renamed",
                    "old_name": old_name,
                    "new_name": new_name,
                    "data": _settings_payload(),
                }))

            elif event.get("type") == "channel_delete":
                name = (event.get("name") or "").strip().lower()
                if name == "general":
                    continue
                if name not in room_settings["channels"]:
                    continue
                room_settings["channels"].remove(name)
                room_settings["archived_channels"] = [
                    c for c in room_settings.get("archived_channels", []) if c != name
                ]
                store.delete_channel(name)
                import mcp_bridge
                mcp_bridge.migrate_cursors_delete(name)
                _save_settings()
                await broadcast_settings()

            elif event.get("type") == "channel_archive":
                # Archive (or restore) a channel: it moves to the collapsed "Archived"
                # group. Messages, agents and cursors are untouched, unlike delete.
                name = (event.get("name") or "").strip().lower()
                if name == "general" or name not in room_settings["channels"]:
                    continue
                archived = [c for c in room_settings.get("archived_channels", []) if c != name]
                if event.get("archived", True):
                    archived.append(name)
                room_settings["archived_channels"] = archived
                _save_settings()
                await broadcast_settings()

    except WebSocketSendTimeout:
        ws_clients.discard(websocket)
        await _close_slow_websocket(websocket)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("WebSocket error")
    finally:
        ws_clients.discard(websocket)
        writer = _ws_writers.pop(websocket, None)
        if writer:
            writer[1].cancel()


# --- REST endpoints ---

ALLOWED_UPLOAD_EXTS = IMAGE_UPLOAD_EXTS  # backwards-compatible image policy
MAX_UPLOAD_BYTES = DEFAULT_IMAGE_LIMIT_MB * 1024 * 1024


@app.post("/api/upload")
async def upload_image(file: UploadFile = File(...), display_name: str | None = Form(None)):
    upload_dir = Path(config.get("images", {}).get("upload_dir", "./uploads"))
    upload_dir.mkdir(parents=True, exist_ok=True)

    # Uploaded names are display labels only; never use them as disk paths.
    name = (display_name or file.filename or "file").replace("\\", "/").rsplit("/", 1)[-1]
    ext = Path(name).suffix.lower()
    file_cfg = config.get("attachments", {})
    allowed_files = {str(value).lower() for value in file_cfg.get("allowed_extensions", ["*"])}
    is_image = ext in IMAGE_UPLOAD_EXTS
    if not is_image and "*" not in allowed_files and ext not in allowed_files:
        return JSONResponse({"error": f"unsupported file type: {ext or '(no extension)'}"}, status_code=400)

    if is_image:
        max_mb, max_bytes = image_upload_limit(config)
    else:
        max_mb = file_cfg.get("max_size_mb", 25)
        max_bytes = int(max_mb * 1024 * 1024)
    # Keep storage URLs simple even for names with unusual suffixes (e.g. .c++).
    storage_ext = ext if not ext or _re.fullmatch(r"\.[a-z0-9_-]{1,32}", ext) else ".bin"
    filename = f"{uuid.uuid4().hex}{storage_ext}"
    filepath = upload_dir / filename
    size = 0
    try:
        with filepath.open("xb") as destination:
            while chunk := await file.read(64 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(f"file too large (max {max_mb} MB)")
                destination.write(chunk)
    except ValueError as exc:
        filepath.unlink(missing_ok=True)
        return JSONResponse({"error": str(exc)}, status_code=413)
    except BaseException:
        filepath.unlink(missing_ok=True)
        raise

    import mimetypes

    return JSONResponse({
        "name": name,
        "url": f"/uploads/{filename}",
        "size": size,
        "media_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
        "kind": "image" if is_image else "file",
    })


# --- Export / Import ---

@app.get("/api/export")
async def export_history():
    """Download a zip archive of project history."""
    import archive as _archive
    import time as _time
    try:
        zip_bytes = _archive.build_export(
            store, jobs, rules, summaries,
            app_version=config.get("server", {}).get("version", ""),
        )
    except Exception as exc:
        return JSONResponse({"error": f"export failed: {exc}"}, status_code=500)
    filename = f"agentchattr-export-{_time.strftime('%Y%m%d-%H%M%S')}.zip"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/import")
async def import_history(file: UploadFile = File(...)):
    """Upload a zip archive and merge it into current stores."""
    import archive as _archive
    if not file.filename or not file.filename.lower().endswith(".zip"):
        return JSONResponse({"error": "unsupported file type: expected .zip"}, status_code=400)
    content = await file.read()
    if len(content) > _archive.MAX_IMPORT_SIZE:
        return JSONResponse(
            {"error": f"file too large (max {_archive.MAX_IMPORT_SIZE // 1024 // 1024}MB)"},
            status_code=400,
        )
    channel_list = list(room_settings.get("channels", ["general"]))
    max_ch = room_settings.get("max_channels", 8)
    report = _archive.import_archive(
        content, store, jobs, rules, summaries,
        channel_list, max_channels=max_ch,
    )
    if not report.get("ok"):
        error = report.get("error", "import failed")
        status = 409 if "already running" in error else 400
        return JSONResponse({"error": error}, status_code=status)
    # Update channel list if new channels were created
    if report["channels"]["created"]:
        room_settings["channels"] = channel_list
        _save_settings()
        await broadcast_settings()
    # Tell all connected clients to reload (picks up imported messages)
    data = json.dumps({"type": "reload"})
    await _broadcast(data)
    return JSONResponse(report)


@app.get("/api/messages")
async def get_messages(since_id: int = 0, limit: int = 50, channel: str = ""):
    ch = channel if channel else None
    if since_id:
        return store.get_since(since_id, channel=ch)
    return store.get_recent(limit, channel=ch)


@app.post("/api/send")
async def api_send(request: Request):
    """REST endpoint for API agents to send messages without WebSocket.

    Authenticated via Bearer registration token. Sender is resolved from
    the token — the agent cannot impersonate another identity.
    """
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return JSONResponse({"error": "missing Authorization: Bearer <token>"}, status_code=401)
    token = auth[7:].strip()
    inst = registry.resolve_token(token) if registry else None
    if not inst:
        return JSONResponse({"error": "invalid or expired token"}, status_code=403)

    sender = inst["name"]
    body = await request.json()
    text = body.get("text", "").strip()
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)
    channel = body.get("channel", "general")

    msg = store.add(sender, text, channel=channel, sender_kind="agent",
                    sender_identity=inst["identity_id"], sender_base=inst["base"])
    return JSONResponse(msg)


@app.get("/api/status")
async def get_status():
    status = agents.get_status()
    status["paused"] = any(router.is_paused(ch) for ch in room_settings.get("channels", ["general"]))
    return status


@app.get("/api/launcher/registrations")
async def get_launcher_registrations():
    """Return configured agent types with active registrations on this server."""
    return {"registered_bases": sorted({
        inst["base"] for inst in registry.get_all().values() if inst["state"] == "active"
    })}


@app.get("/api/browser-clients")
async def get_browser_clients():
    """Number of open chat pages (WebSocket clients); lets launchers avoid opening a duplicate tab."""
    return {"count": len(ws_clients)}


@app.get("/api/settings")
async def get_settings():
    return room_settings


@app.delete("/api/hat/{agent_name}")
async def delete_hat(agent_name: str):
    """Remove an agent's hat (called by the trash-can UI)."""
    clear_agent_hat(agent_name)
    return JSONResponse({"ok": True})


# --- Jobs API ---

@app.get("/api/schedules")
async def get_schedules():
    return schedules.list_all()


@app.post("/api/schedules")
async def create_schedule(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "")
    targets = body.get("targets", [])
    channel = body.get("channel", "general")
    spec = body.get("spec", "")
    one_shot = bool(body.get("one_shot", False))
    send_at_date = body.get("send_at_date", "")  # "YYYY-MM-DD" for one-shot
    created_by = body.get("created_by", "user")
    if not prompt or not targets or not spec:
        return JSONResponse({"error": "prompt, targets, and spec are required"}, status_code=400)
    # A relative send ("in 2h 30m") is resolved by the client, which posts the
    # resulting moment directly; there is no recurrence to parse out of it.
    # A send_at names a single moment, so it only means anything for a one-shot.
    # A recurring request ignores it, exactly as it did before the field
    # existed; that keeps an unparseable spec falling through to the 400 below
    # instead of being stored as an unasked-for daily repeat.
    explicit_send_at = None
    if one_shot and body.get("send_at") is not None:
        try:
            explicit_send_at = float(body["send_at"])
        except (TypeError, ValueError, OverflowError):
            return JSONResponse(
                {"error": "send_at must be an epoch timestamp"}, status_code=400
            )
    try:
        interval_sec, daily_at = parse_schedule_spec(spec)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if interval_sec is None and explicit_send_at is None:
        return JSONResponse({"error": f"Invalid schedule spec: {spec}"}, status_code=400)
    # For one-shot, compute exact send_at timestamp from date + daily_at time
    send_at = explicit_send_at
    if send_at is None and one_shot and daily_at and send_at_date:
        import datetime as _dt
        try:
            dt = _dt.datetime.strptime(f"{send_at_date} {daily_at}", "%Y-%m-%d %H:%M")
            send_at = dt.timestamp()
        except ValueError:
            pass
    try:
        s = schedules.create(
            prompt=prompt, targets=targets, channel=channel,
            interval_seconds=interval_sec, daily_at=daily_at,
            one_shot=one_shot, send_at=send_at,
            created_by=created_by,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(s)


@app.delete("/api/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str):
    removed = schedules.delete(schedule_id)
    if not removed:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})


@app.patch("/api/schedules/{schedule_id}/toggle")
async def toggle_schedule(schedule_id: str):
    result = schedules.toggle(schedule_id)
    if not result:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(result)


@app.get("/api/jobs")
async def get_jobs(channel: str = "", status: str = ""):
    """List jobs, optionally filtered."""
    ch = channel if channel else None
    st = status if status else None
    return jobs.list_all(channel=ch, status=st)


@app.post("/api/messages/{msg_id}/demote")
async def demote_proposal(msg_id: int):
    """Demote a proposal-style message back to a regular chat message."""
    msg = store.get_by_id(msg_id)
    if not msg:
        return JSONResponse({"error": "message not found"}, status_code=404)
    msg_type = msg.get("type")
    if msg_type not in {"job_proposal", "session_draft"}:
        return JSONResponse({"error": "not a proposal"}, status_code=400)
    meta = msg.get("metadata", {})
    updated_fields = {"type": "chat", "metadata": {}}

    if msg_type == "job_proposal":
        body_text = meta.get("body", "")
        title = meta.get("title", "")
        plain_text = f"**{title}**\n\n{body_text}" if title else body_text or msg.get("text", "")
        updated_fields["text"] = plain_text
    else:
        tmpl = meta.get("template")
        errors = meta.get("errors", []) or []
        proposed_by = meta.get("proposed_by") or msg.get("sender", "system")
        parts = []

        if isinstance(tmpl, dict):
            name = str(tmpl.get("name", "")).strip()
            desc = str(tmpl.get("description", "")).strip()
            if name:
                parts.append(f"**{name}**")
            if desc:
                parts.append(desc)
            phases = tmpl.get("phases") or []
            if phases:
                lines = []
                for i, ph in enumerate(phases, 1):
                    ph_name = ph.get("name", f"Round {i}")
                    participants = ", ".join(ph.get("participants", []))
                    line = f"{i}. {ph_name}"
                    if participants:
                        line += f" -- {participants}"
                    prompt = (ph.get("prompt") or "").strip()
                    if prompt:
                        line += f"\n   {prompt}"
                    lines.append(line)
                parts.append("\n".join(lines))
        else:
            label = str(msg.get("text", "")).strip() or "Session draft"
            parts.append(label)
            if errors:
                parts.append("\n".join(f"- {e}" for e in errors))

        updated_fields["sender"] = proposed_by
        updated_fields["text"] = "\n\n".join(p for p in parts if p).strip()

    updated = store.update_message(msg_id, updated_fields)
    if updated:
        # Broadcast the updated message to all clients
        payload = json.dumps({"type": "edit", "message": updated})
        await _broadcast(payload)
    return updated or {"ok": True}


@app.post("/api/messages/{msg_id}/resolve_decision")
async def resolve_decision(msg_id: int, request: Request):
    """Resolve an inline decision card by recording the chosen option."""
    body = await request.json()
    chosen = body.get("choice", "")
    if not chosen:
        return JSONResponse({"error": "choice is required"}, status_code=400)
    # Atomic check + resolve under lock to prevent double-click race
    error = None
    channel = "general"
    sender = ""
    sender_identity = None
    with store._lock:
        position, msg = store._message_row(msg_id)
        if not msg:
            error = ("message not found", 404)
        elif msg.get("type") != "decision":
            error = ("not a decision message", 400)
        else:
            meta = msg.get("metadata") or {}
            if "orch_gate" in meta:
                error = ("Orch gate decisions require operator approval", 403)
            elif "job_reopen" in meta:
                error = ("use the bound Job reopen decision endpoint", 409)
            elif "orch_intake" in meta:
                error = ("use the Orch intake endpoint for this card", 409)
            elif meta.get("resolved"):
                error = ("already resolved", 400)
            else:
                valid_choices = meta.get("choices", [])
                if valid_choices and chosen not in valid_choices:
                    error = (f"invalid choice. Valid: {valid_choices}", 400)
                else:
                    previous = dict(msg)
                    meta = dict(meta)
                    meta["resolved"] = True
                    meta["chosen"] = chosen
                    msg["metadata"] = meta
                    channel = msg.get("channel", "general")
                    sender = msg.get("sender", "")
                    if msg.get("sender_kind") == "agent":
                        sender_identity = msg.get("sender_identity")
                    try:
                        store._save_message(position, msg)
                        store._rewrite()
                    except OSError as exc:
                        store._save_message(position, previous)
                        raise OSError(exc.errno, f"Could not save decision: {exc}", str(store._path.resolve())) from exc
    if error:
        return JSONResponse({"error": error[0]}, status_code=error[1])
    # Post the chosen answer as a regular chat message tagged @sender
    username = room_settings.get("username", "user")
    metadata = None
    if sender_identity and registry:
        instance = registry.get_active_instance_by_identity(sender_identity)
        sender = instance["name"] if instance else ""
        metadata = {"decision_author_identity": sender_identity}
    reply_text = f"@{sender} {chosen}" if sender else chosen
    try:
        bound = orch_reply_metadata(jobs, store, channel, msg_id)
        if bound:
            metadata = {**(metadata or {}), **bound}
        reply = store.add(username, reply_text, reply_to=msg_id, channel=channel,
                          sender_kind="human", metadata=metadata)
        record_orch_reply(jobs, reply)
    except Exception:
        import traceback; traceback.print_exc()
    # Broadcast updated decision card so the UI swaps buttons to resolved state
    updated = store.get_by_id(msg_id)
    if updated:
        await _broadcast(json.dumps({"type": "message_update", "message": updated}))
    return {"ok": True, "chosen": chosen}


@app.post("/api/jobs/{job_uid}/orch-gates/{decision_id}/resolve")
async def resolve_orch_gate(job_uid: str, decision_id: int, request: Request):
    """Record a local browser choice for the exact current gate."""
    actor = verify_operator(
        request, config, session_token,
        room_username=room_settings.get("username", "user"),
        is_host_interface=is_local_client,
    )
    if actor is None:
        return JSONResponse({"error": "local browser session required"}, status_code=403)
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid gate decision"}, status_code=400)
    gate, revision, choice = (body.get(key) for key in ("gate", "revision", "choice"))
    if (not isinstance(gate, str) or not gate or type(revision) is not int or revision < 1
            or not isinstance(choice, str) or not choice or decision_id < 0):
        return JSONResponse({"error": "invalid gate decision"}, status_code=400)

    decision = jobs.resolve_orch_gate(job_uid, decision_id=decision_id, gate=gate,
                                     revision=revision, choice=choice, actor=actor)
    if decision is None:
        return JSONResponse({"error": "gate decision is stale, conflicting, or out of scope"}, status_code=409)
    try:
        updated = store.project_orch_gate_decision(decision)
    except OSError:
        # The Job record is authoritative. A retry can repair the display without reapplying the choice.
        log.exception("Could not update the Orch gate card %s", decision_id)
    else:
        if updated:
            await _broadcast(json.dumps({"type": "message_update", "message": updated}))
    return decision


@app.post("/api/messages/{msg_id}/resolve_rule_proposal")
async def resolve_rule_proposal(msg_id: int, request: Request):
    """Activate or dismiss a rule proposal."""
    msg = store.get_by_id(msg_id)
    if not msg:
        return JSONResponse({"error": "message not found"}, status_code=404)
    if msg.get("type") != "rule_proposal":
        return JSONResponse({"error": "not a rule proposal"}, status_code=400)
    body = await request.json()
    action = body.get("action", "")
    meta = dict(msg.get("metadata") or {})
    rule_id = meta.get("rule_id")

    if action == "activate" and rule_id is not None:
        if not rules.activate(int(rule_id)):
            return JSONResponse({"error": "Rule no longer exists."}, status_code=409)
        meta["status"] = "activated"
    elif action == "draft" and rule_id is not None:
        if not rules.make_draft(int(rule_id)):
            return JSONResponse({"error": "Rule no longer exists."}, status_code=409)
        meta["status"] = "drafted"
    elif action == "dismiss" and rule_id is not None:
        rules.delete(int(rule_id))
        meta["status"] = "dismissed"
    else:
        return JSONResponse({"error": "invalid action"}, status_code=400)

    updated = store.update_message(msg_id, {"metadata": meta})
    if updated:
        # Broadcast the updated message so all clients re-render the card
        payload = json.dumps({"type": "edit", "message": updated})
        await _broadcast(payload)
    return updated or {"ok": True}


@app.post("/api/messages/{msg_id}/demote_rule_proposal")
async def demote_rule_proposal(msg_id: int):
    """Demote a rule_proposal message back to a regular chat message and delete the rule."""
    msg = store.get_by_id(msg_id)
    if not msg:
        return JSONResponse({"error": "message not found"}, status_code=404)
    if msg.get("type") != "rule_proposal":
        return JSONResponse({"error": "not a rule proposal"}, status_code=400)
    meta = msg.get("metadata", {})
    rule_id = meta.get("rule_id")
    if rule_id is not None:
        rules.delete(int(rule_id))
    text = meta.get("text", msg.get("text", ""))
    updated = store.update_message(msg_id, {
        "type": "chat",
        "text": text,
        "metadata": {},
    })
    if updated:
        payload = json.dumps({"type": "edit", "message": updated})
        await _broadcast(payload)
    return updated or {"ok": True}


@app.post("/api/jobs/proposal-requests")
async def request_job_proposal(request: Request):
    """Request a proposal from one instance of the chosen writer type."""
    body = await request.json()
    if not isinstance(body, dict) or body.get("writer_base") not in PROPOSAL_WRITER_BASES:
        return JSONResponse({"error": "unknown proposal writer type"}, status_code=400)
    source_id = body.get("source_msg_id")
    source = store.get_by_id(source_id) if type(source_id) is int and source_id >= 0 else None
    if not source:
        return JSONResponse({"error": "source message not found"}, status_code=404)
    channel = source.get("channel", "general")
    prompt = (
        f"use mcp to read #{channel} - you're mentioned, take appropriate action and respond "
        f"- conversion request: read message #{source_id} and call chat_propose_job "
        f"with channel={channel!r}, source_msg_id={source_id}, title (max 80 chars) "
        "and body (max 500 chars)."
    )
    try:
        dispatched = agents.queue_proposal_writer(
            base=body["writer_base"], channel=channel, prompt=prompt, source_msg_id=source_id,
        )
    except OSError:
        log.exception("Could not queue proposal for message %s", source_id)
        return JSONResponse({"error": "could not queue proposal request"}, status_code=503)
    return {"status": "dispatched" if dispatched else "queued"}


@app.post("/api/trigger-agent")
async def trigger_agent_silent(request: Request):
    """Silently trigger an agent with a message (no chat message posted)."""
    body = await request.json()
    agent_name = body.get("agent", "").strip()
    message = body.get("message", "").strip()
    channel = body.get("channel", "general")
    source_msg_id = body.get("source_msg_id")
    if not agent_name or not message:
        return JSONResponse({"error": "agent and message required"}, status_code=400)

    custom_prompt = body.get("prompt", "").strip()
    if source_msg_id is not None:
        source = _job_source_message(source_msg_id)
        if source is None or source.get("channel", "general") != channel:
            return JSONResponse({"error": "source message not found in this channel"}, status_code=400)
        if not custom_prompt:
            custom_prompt = f"use mcp to read #{channel} - you're mentioned, take appropriate action and respond"
        custom_prompt += (
            f" - conversion request: read message #{source_msg_id} and call chat_propose_job "
            f"with channel={channel!r}, source_msg_id={source_msg_id}, title (max 80 chars) "
            "and body (max 500 chars)."
        )
    elif not custom_prompt:
        custom_prompt = (
            f"use mcp to read #{channel} - you're mentioned, take appropriate action and respond "
            "- conversion request: use chat_propose_job to propose a job from the referenced message."
        )
    # Resolve to instances if multi-instance
    targets = [agent_name]
    if registry:
        resolved = registry.resolve_to_instances(agent_name)
        if resolved:
            targets = resolved
    for target in targets:
        if agents.is_available(target):
            await agents.trigger(target, message=message, channel=channel, prompt=custom_prompt)
    return {"ok": True, "triggered": targets}


_ORCH_INTAKE_CHOICE = "Create Orch Job"
_ORCH_MENTION = _re.compile(r"(?<![\w@])@orch(?![\w-]|\.[\w-])", _re.IGNORECASE)


def _has_orch_mention(text):
    return _ORCH_MENTION.search(without_code(text)) is not None


def _job_source_message(msg_id):
    return store.get_by_id(msg_id) if type(msg_id) is int and msg_id >= 0 else None


def _get_source_job(source):
    source_id = source["id"]
    job = jobs.get_for_source(source_id)
    if job is not None:
        return job

    # Read legacy links only from a stored direct anchor or a proposal's
    # explicit source reference. A bare proposal-card ID has no source link.
    channel = source.get("channel", "general")
    for job in jobs.list_all(channel=channel):
        if job.get("source_msg_id") is not None:
            continue
        anchor = _job_source_message(job.get("anchor_msg_id"))
        if anchor is None or anchor.get("channel", "general") != channel:
            continue
        if anchor.get("type") == "job_proposal":
            metadata = anchor.get("metadata") or {}
            if metadata.get("source_msg_uid", source["uid"]) != source["uid"]:
                continue
            saved_source = metadata.get("source_msg_id")
        else:
            saved_source = anchor["id"]
        if type(saved_source) is int and saved_source == source_id:
            return jobs.bind_existing_source(job["id"], job["uid"], source_id)
    return None


def _offer_orch_intake(message):
    """Offer intake only for a message received through authenticated browser chat."""
    if (message.get("sender_kind") != "human" or message.get("type") != "chat"
            or (message.get("metadata") or {}).get("orch_workflow")
            or not _has_orch_mention(message.get("text", ""))):
        return
    channel = message["channel"]
    source = _job_source_message(message.get("reply_to"))
    if source is None or source.get("channel", "general") != channel:
        source = message
    store.add(
        "system", "Start an Orch Job from this conversation?",
        msg_type="decision", channel=channel, reply_to=source["id"], sender_kind="system",
        metadata={
            "choices": [_ORCH_INTAKE_CHOICE],
            "resolved": False,
            "orch_intake": {"source_msg_id": source["id"], "source_msg_uid": source["uid"]},
        },
        uid=str(uuid.uuid5(uuid.NAMESPACE_URL, f"agentchattr:orch-intake:{message['uid']}")),
        once=True,
    )


def _post_job_breadcrumb(job):
    store.add(job["created_by"], f"Job created: {job['title']}", msg_type="job_created",
              channel=job["channel"], metadata={"job_id": job["id"]}, sender_kind="human")


@app.post("/api/jobs/orch-intake")
async def confirm_orch_intake(request: Request):
    """Create or open the Job for a source message, through either browser entry point."""
    if _extract_agent_token(request):
        return JSONResponse({"error": "a browser session is required"}, status_code=403)
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid intake request"}, status_code=400)
    if body.get("proposal_writer", "orch") != "orch":
        return JSONResponse({"error": "named proposal writers use the manual proposal flow"}, status_code=400)
    source = _job_source_message(body.get("source_msg_id"))
    if source is None:
        return JSONResponse({"error": "source message not found"}, status_code=404)
    channel = source.get("channel", "general")
    if channel not in room_settings.get("channels", ["general"]):
        return JSONResponse({"error": "source channel not found"}, status_code=404)

    card = None
    if "card_msg_id" in body:
        card = _job_source_message(body["card_msg_id"])
        meta = (card.get("metadata") or {}) if card else {}
        binding = meta.get("orch_intake")
        if (not card or card.get("type") != "decision" or card.get("sender_kind") != "system"
                or card.get("channel") != channel or not isinstance(binding, dict)
                or binding.get("source_msg_id") != source["id"]
                or binding.get("source_msg_uid") != source["uid"]
                or meta.get("choices") != [_ORCH_INTAKE_CHOICE]):
            return JSONResponse({"error": "invalid Orch intake card"}, status_code=409)

    job = _get_source_job(source)
    if card and meta.get("resolved") and (not job or meta.get("job_uid") != job["uid"]):
        return JSONResponse({"error": "the Job linked to this card is no longer available"}, status_code=409)
    created = False
    if job is None:
        text = source.get("text", "").strip()
        title = _ORCH_MENTION.sub("", text.splitlines()[0]).strip() if text else ""
        candidate_uid = str(uuid.uuid4())
        job = jobs.create(
            title or f"Conversation message #{source['id']}", "job", channel,
            room_settings.get("username", "user"), anchor_msg_id=source["id"], body=text,
            uid=candidate_uid, source_msg_id=source["id"], managed_by="orch",
            proposal_writer="orch",
        )
        created = job["uid"] == candidate_uid
        if created:
            _post_job_breadcrumb(job)
    if card and not meta.get("resolved"):
        updated = store.update_message(card["id"], {"metadata": {
            **meta, "chosen": _ORCH_INTAKE_CHOICE, "resolved": True,
            "job_id": job["id"], "job_uid": job["uid"],
        }})
        if updated:
            await _broadcast(json.dumps({"type": "message_update", "message": updated}))
    return {"job": job, "created": created}


@app.post("/api/jobs")
async def create_job(request: Request):
    """Create a new job."""
    body = await request.json()
    title = body.get("title", "").strip()
    if not title:
        return JSONResponse({"error": "title required"}, status_code=400)
    job_type = body.get("type", "job")
    channel = body.get("channel", "general")
    created_by = body.get("created_by", "user")
    anchor_msg_id = body.get("anchor_msg_id")
    assignee = body.get("assignee", "")
    job_body = body.get("body", "")
    anchor = store.get_by_id(anchor_msg_id) if type(anchor_msg_id) is int and anchor_msg_id >= 0 else None
    proposal = anchor if isinstance(anchor, dict) and anchor.get("type") == "job_proposal" else None
    if proposal and proposal.get("channel", "general") != channel:
        return JSONResponse({"error": "proposal is in a different channel"}, status_code=409)
    meta = dict(proposal.get("metadata") or {}) if proposal else {}
    if proposal:
        author_identity = proposal.get("sender_identity")
        if author_identity:
            writer = registry.get_active_instance_by_identity(author_identity) if registry else None
            assignee = writer["name"] if writer else ""
        else:
            assignee = proposal.get("sender", "")
    if meta.get("status") == "accepted":
        linked_id = meta.get("job_id")
        existing = jobs.get(linked_id) if type(linked_id) is int else None
        if existing is not None and existing["uid"] == meta.get("job_uid"):
            return existing
        existing = jobs.get_for_proposal(anchor_msg_id)
        if existing is not None:
            return existing
        return JSONResponse({"error": "the proposal's Job was already accepted"}, status_code=409)

    source = None
    if proposal and "source_msg_id" in meta:
        source = _job_source_message(meta["source_msg_id"])
        if (source is None or source.get("channel", "general") != channel
                or meta.get("source_msg_uid", source["uid"]) != source["uid"]):
            return JSONResponse({"error": "proposal source message not found in this channel"}, status_code=409)
    candidate_uid = str(uuid.uuid4())
    result = _get_source_job(source) if source else None
    if result is None:
        result = jobs.create(
            title=title, job_type=job_type, channel=channel,
            created_by=created_by, anchor_msg_id=anchor_msg_id,
            assignee=assignee, body=job_body, uid=candidate_uid,
            from_proposal=bool(proposal),
            source_msg_id=source["id"] if source else None,
            proposal_author=proposal.get("sender") if proposal else None,
            proposal_author_identity=proposal.get("sender_identity") if proposal else None,
        )
    updated_msg = None
    if proposal:
        meta.update(status="accepted", job_id=result["id"], job_uid=result["uid"])
        updated_msg = store.update_message(anchor_msg_id, {"metadata": meta})
    if result["uid"] == candidate_uid:
        _post_job_breadcrumb(result)
    if updated_msg:
        await _broadcast(json.dumps({"type": "edit", "message": updated_msg}))
    return result


@app.patch("/api/jobs/{job_id}")
async def update_job(job_id: int, request: Request):
    """Update a job's status, title, or assignee."""
    body = await request.json()
    if _read_only_orch_job(job_id):
        return _orch_history_read_only()
    result = None
    if "status" in body:
        result = jobs.update_status(job_id, body["status"])
        if result is None:
            return JSONResponse({"error": "not found or invalid transition"}, status_code=404)
    if "title" in body:
        result = jobs.update_title(job_id, body["title"])
    if "assignee" in body:
        result = jobs.update_assignee(job_id, body["assignee"])
    if result is None:
        return JSONResponse({"error": "not found or invalid"}, status_code=404)
    return result


@app.post("/api/jobs/{job_id}/reopen-requests")
async def create_job_reopen_request(job_id: int, request: Request):
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "Expected JSON for the reopen request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Expected a reopen request object"}, status_code=400)
    try:
        result = jobs.request_reopen(job_id, expected_uid=body.get("job_uid"),
                                     expected_version=body.get("expected_version"),
                                     requester="local-browser", reason=body.get("reason"))
    except ReopenConflict as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    reopen = result["request"]
    job = result["job"]
    parent_id = None
    if job.get("orch_workflow_active"):
        latest = orch_history(jobs, store, job["uid"])
        candidates = [entry["message_id"] for entry in latest["entries"]
                      if entry["kind"] == "closeout" and entry["message_id"] is not None]
        parent_id = next(
            (message_id for message_id in reversed(candidates) if store.get_by_id(message_id)),
            job.get("source_msg_id"),
        )
    card = store.add(
        "system", f"Job #{job_id} reopen request: {reopen['reason']}",
        msg_type="decision", channel=job["channel"], sender_kind="system", reply_to=parent_id,
        metadata={"choices": ["Approve", "Decline", "Needs clarification"],
                  "job_reopen": {"job_id": job_id, "job_uid": reopen["job_uid"],
                                 "expected_version": reopen["job_version"], "request_id": reopen["id"]}},
        uid=f"job-reopen-{reopen['id']}", once=True,
    )
    return {**result, "card_msg_id": card["id"]}


@app.post("/api/jobs/{job_id}/reopen-requests/{request_id}/decision")
async def decide_job_reopen_request(job_id: int, request_id: str, request: Request):
    actor = verify_operator(request, config, session_token, room_username=room_settings.get("username", "user"))
    if actor is None:
        return JSONResponse({"error": "Local browser session required"}, status_code=403)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "Expected JSON for the reopen decision"}, status_code=400)
    if not isinstance(body, dict) or body.get("decision") not in ("approve", "decline", "clarify"):
        return JSONResponse({"error": "Invalid reopen decision"}, status_code=400)
    try:
        result = jobs.decide_reopen(job_id, request_id, expected_uid=body.get("job_uid"),
                                    expected_version=body.get("expected_version"), actor=actor,
                                    role="owner", decision=body["decision"], source="local-browser")
    except ReopenConflict as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    reopen = result["request"]
    if reopen["status"] != "pending" and result["job"].get("orch_workflow_active"):
        card = store.get_by_uid(f"job-reopen-{request_id}")
        if card:
            metadata = card["metadata"]
            if not metadata.get("resolved"):
                metadata.update(resolved=True, chosen={"approved": "Approve", "declined": "Decline"}[reopen["status"]])
                try:
                    updated = store.update_message(card["id"], {"metadata": metadata})
                except OSError:
                    log.exception("Could not update Orch reopen card %s", card["id"])
                else:
                    if updated:
                        await _broadcast(json.dumps({"type": "message_update", "message": updated}))
    return result


@app.post("/api/jobs/reorder")
async def reorder_jobs(request: Request):
    """Reorder jobs within a status group (globally, not per-channel)."""
    body = await request.json()
    status = body.get("status", "open")
    ordered_ids = body.get("ordered_ids", [])
    if not isinstance(ordered_ids, list) or len(ordered_ids) == 0:
        return JSONResponse({"error": "ordered_ids required"}, status_code=400)
    updated = jobs.reorder(status=status, ordered_ids=ordered_ids)
    return {"ok": True, "updated": len(updated)}


@app.get("/api/jobs/{job_id}/messages")
async def get_job_messages(job_id: int):
    """Get all messages in a job."""
    msgs = jobs.get_messages(job_id)
    if msgs is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return msgs


@app.get("/api/jobs/{job_uid}/orch-history")
async def get_orch_history(job_uid: str):
    result = orch_history(jobs, store, job_uid)
    return result if result is not None else JSONResponse({"error": "Orch Job not found"}, status_code=404)


@app.get("/api/jobs/{job_uid}/orch-history/{message_id}")
async def get_orch_history_context(job_uid: str, message_id: int):
    result = orch_history_context(jobs, store, job_uid, message_id)
    return result if result is not None else JSONResponse({"error": "Orch message not found"}, status_code=404)


@app.post("/api/jobs/{job_id}/messages")
async def post_job_message(job_id: int, request: Request):
    """Post a message to a job."""
    body = await request.json()
    if _read_only_orch_job(job_id):
        return _orch_history_read_only()
    text = body.get("text", "").strip()
    sender = body.get("sender", "user")
    attachments = body.get("attachments", [])
    if not text and not attachments:
        return JSONResponse({"error": "text or attachments required"}, status_code=400)
    msg_type = body.get("type", "chat")
    msg = jobs.add_message(job_id, sender, text,
                           attachments=attachments, msg_type=msg_type)
    if msg is None:
        return JSONResponse({"error": "job not found"}, status_code=404)

    # Route @mentions in job messages to agents (with job_id context)
    job = jobs.get(job_id)
    if job:
        channel = job.get("channel", "general")
        raw_targets = router.get_targets(text, channel, sender_is_agent=False)
        targets = []
        for t in raw_targets:
            if registry:
                targets.extend(registry.resolve_to_instances(t))
            else:
                targets.append(t)
        targets = list(dict.fromkeys(targets))

        import mcp_bridge
        chat_msg = f"{sender}: {text}" if text else ""
        for target in targets:
            if registry:
                inst = registry.get_instance(target)
                if inst and inst.get("state") == "pending":
                    continue
            if agents.is_available(target):
                await agents.trigger(target, message=chat_msg, channel=channel,
                                     job_id=job_id)

    return msg


@app.delete("/api/jobs/{job_id}/messages/{msg_id}")
async def delete_job_message(job_id: int, msg_id: int):
    """Soft-delete a message in a job thread."""
    if _read_only_orch_job(job_id):
        return _orch_history_read_only()
    result = jobs.delete_message(job_id, msg_id)
    if result is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True, **result}


@app.post("/api/jobs/{job_id}/messages/{msg_index}/resolve")
async def resolve_job_message(job_id: int, msg_index: int, request: Request):
    """Resolve a suggestion message (accept/dismiss)."""
    body = await request.json()
    resolution = body.get("resolution", "dismissed")
    job = jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    if job.get("orch_workflow_active"):
        return _orch_history_read_only()
    msgs = job.get("messages", [])
    if msg_index < 0 or msg_index >= len(msgs):
        return JSONResponse({"error": "invalid message index"}, status_code=400)
    msg = msgs[msg_index]
    msg["resolved"] = resolution
    jobs._save()

    author_identity = msg.get("sender_identity")
    if resolution == "accepted" and author_identity:
        agents.notify_suggestion_accepted(author_identity, job.get("channel", "general"), job_id)

    return {"ok": True, "resolution": resolution}


def _read_only_orch_job(job_id):
    return bool((jobs.get(job_id) or {}).get("orch_workflow_active"))


def _orch_history_read_only():
    return JSONResponse({"error": "Use the Job's main-channel conversation"}, status_code=409)


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: int, request: Request):
    """Delete or archive a job. ?permanent=true for real delete."""
    if _read_only_orch_job(job_id):
        return _orch_history_read_only()
    permanent = request.query_params.get("permanent", "").lower() == "true"
    if permanent:
        result = jobs.delete(job_id)
    else:
        result = jobs.update_status(job_id, "archived", cancel=True)
    if result is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return result


@app.get("/api/roles")
async def get_roles():
    """Get all agent roles."""
    import mcp_bridge
    return mcp_bridge.get_all_roles()


@app.post("/api/roles/{agent_name}")
async def set_agent_role(agent_name: str, request: Request):
    """Set or clear an agent's role."""
    import mcp_bridge
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    role = body.get("role", "").strip()
    try:
        mcp_bridge.set_role(agent_name, role)
    except OSError:
        log.exception("Failed to save role for %s", agent_name)
        # The browser updates the role optimistically; restore the saved value.
        await broadcast_status()
        return JSONResponse({"error": "Could not save agent role"}, status_code=500)
    await broadcast_status()
    return JSONResponse({"ok": True, "role": role})


# --- Rules API ---

@app.get("/api/rules")
async def get_rules():
    """Get all rules (all states)."""
    return JSONResponse(rules.list_all())


@app.get("/api/rules/active")
async def get_active_rules():
    """Get compact active rules for agent injection."""
    data = rules.active_list()
    data["refresh_interval"] = room_settings.get("rules_refresh_interval", 10)
    return JSONResponse(data)


@app.post("/api/rules/remind")
async def remind_agents():
    """Set remind flag — agents get rules on next trigger."""
    rules.set_remind()
    remind_data = json.dumps({"type": "rules_remind", "data": {}})
    await _broadcast(remind_data)
    return JSONResponse({"ok": True})


@app.post("/api/rules/agent_sync/{agent_name}")
async def report_rule_sync(agent_name: str, request: Request):
    """Wrapper reports that an agent has seen rules at a given epoch."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    epoch = body.get("epoch", 0)
    rules.report_agent_sync(agent_name, epoch)
    # Clear remind flag once any agent has seen the updated rules
    rules.clear_remind()
    return JSONResponse({"ok": True})


@app.get("/api/rules/freshness")
async def get_rules_freshness():
    """Get per-agent sync status."""
    return JSONResponse(rules.agent_freshness())


@app.post("/api/register")
async def register_agent(request: Request):
    """Wrapper calls this to register a new agent instance."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    base = body.get("base", "")
    label = body.get("label")
    if not base:
        return JSONResponse({"error": "base is required"}, status_code=400)
    if isinstance(body.get("name"), str) and body["name"].lower() == room_settings.get("username", "user").lower():
        return JSONResponse({"error": "That name is reserved for the human user"}, status_code=409)
    try:
        result = (registry.register(base, label, body["name"]) if "name" in body
                  else registry.register(base, label))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except OSError:
        log.exception("Could not persist a new agent registration")
        return JSONResponse({"error": "Agent registration could not be saved. Retry when storage is available."},
                            status_code=503)
    if result is None:
        return JSONResponse({"error": f"unknown base: {base}"}, status_code=400)
    # Touch presence so the instance doesn't immediately time out
    import mcp_bridge
    for expired_name in result.pop("_expired", []):
        mcp_bridge.purge_identity(expired_name)
    with mcp_bridge._presence_lock:
        mcp_bridge._presence[result["name"]] = time.monotonic()
        if body.get("activity_known") is False:
            mcp_bridge._activity_known[result["name"]] = False
        else:
            mcp_bridge._activity_known.pop(result["name"], None)
    # If slot 1 was renamed (e.g. "claude" → "claude-1"), migrate state
    renamed = result.pop("_renamed_slot1", None)
    if renamed:
        mcp_bridge.migrate_identity(renamed["old"], renamed["new"])
        store.rename_sender(renamed["old"], renamed["new"])
        _migrate_agent_last_channel(renamed["old"], renamed["new"])
        if _event_loop:
            rename_event = json.dumps({
                "type": "agent_renamed",
                "old_name": renamed["old"],
                "new_name": renamed["new"],
            })
            asyncio.run_coroutine_threadsafe(_broadcast(rename_event), _event_loop)
    # Broadcast pending_instance event so UI can show naming lightbox
    if result.get("state") == "pending" and _event_loop:
        pending_event = json.dumps({
            "type": "pending_instance",
            "name": result["name"],
            "base": base,
            "label": result.get("label", result["name"]),
            "color": result.get("color", "#888"),
        })
        asyncio.run_coroutine_threadsafe(_broadcast(pending_event), _event_loop)
    return JSONResponse(result)


@app.post("/api/deregister/{name}")
async def deregister_agent(name: str, request: Request):
    """Wrapper calls this on shutdown to remove its instance."""
    auth_inst = _resolve_authenticated_agent(request)
    presented_token = _extract_agent_token(request)
    if presented_token and not auth_inst:
        return JSONResponse({"error": "stale_session"}, status_code=409)
    if auth_inst:
        name = auth_inst["name"]
    elif registry and registry.is_agent_family(name):
        return JSONResponse({"error": "authenticated agent session required"}, status_code=403)

    result = registry.deregister(name)
    if result is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    # Clean up runtime state (presence, activity, cursors, rename chains)
    import mcp_bridge
    mcp_bridge.purge_identity(name)
    registry.clean_renames_for(name)
    # If the remaining instance was renamed back (e.g. "claude-1" → "claude"), migrate state
    renamed = result.pop("_renamed_back", None)
    if renamed:
        mcp_bridge.migrate_identity(renamed["old"], renamed["new"])
        store.rename_sender(renamed["old"], renamed["new"])
        _migrate_agent_last_channel(renamed["old"], renamed["new"])
        if _event_loop:
            rename_event = json.dumps({
                "type": "agent_renamed",
                "old_name": renamed["old"],
                "new_name": renamed["new"],
            })
            asyncio.run_coroutine_threadsafe(_broadcast(rename_event), _event_loop)
    return JSONResponse({"ok": True})


@app.post("/api/label/{name}")
async def rename_agent_label(name: str, request: Request):
    """Rename an agent (human-initiated from UI). Changes identity + label."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    label = body.get("label", "").strip()
    if not label:
        return JSONResponse({"error": "label is required"}, status_code=400)

    new_id = sanitize_chat_id(label) or name

    if new_id == name:
        # Same ID — label-only change
        if registry.set_label(name, label):
            return JSONResponse({"ok": True})
        return JSONResponse({"error": "not found"}, status_code=404)

    result = registry.rename(name, new_id, label)
    if isinstance(result, str):
        # Rename failed — try label-only as fallback
        if registry.set_label(name, label):
            return JSONResponse({"ok": True, "warning": result})
        return JSONResponse({"error": result}, status_code=400)

    import mcp_bridge
    mcp_bridge.migrate_identity(name, new_id)
    # Update sender on all historical messages
    store.rename_sender(name, new_id, result["identity_id"])
    _migrate_agent_last_channel(name, new_id)
    return JSONResponse({"ok": True, "new_name": new_id})


@app.post("/api/heartbeat/{agent_name}")
async def heartbeat(agent_name: str, request: Request):
    """Wrapper calls this to keep presence alive and report activity.

    Returns the canonical name from the registry so the wrapper can
    detect renames (e.g. claim renamed 'claude-2' to 'claude-music').
    """
    import mcp_bridge
    auth_inst = _resolve_authenticated_agent(request)
    presented_token = _extract_agent_token(request)
    if presented_token and not auth_inst:
        return JSONResponse({"error": "stale_session"}, status_code=409)
    if registry and registry.is_agent_family(agent_name) and not auth_inst:
        return JSONResponse({"error": "authenticated agent session required"}, status_code=403)

    current_name = auth_inst["name"] if auth_inst else agent_name
    mcp_bridge._touch_presence(current_name)
    # Optional activity report from wrapper's terminal monitor
    _activity_changed = False
    try:
        body = await request.json()
        if isinstance(body.get("activity_known"), bool):
            known = body["activity_known"]
            if mcp_bridge.is_activity_known(current_name) != known:
                _activity_changed = True
            mcp_bridge.set_activity_known(current_name, known)
        if isinstance(body.get("active"), bool):
            active_val = bool(body["active"])
            was_active = mcp_bridge._activity.get(current_name, False)
            mcp_bridge.set_active(current_name, active_val)
            _activity_changed = _activity_changed or was_active != active_val
        if "needs_input" in body:
            needs_val = bool(body["needs_input"])
            if mcp_bridge.is_needs_input(current_name) != needs_val:
                _activity_changed = True
            mcp_bridge.set_needs_input(current_name, needs_val)
    except Exception:
        pass  # No body = plain heartbeat
    # Immediately broadcast on activity state change (don't wait for background checker)
    if _activity_changed:
        await broadcast_status()
    if auth_inst and session_engine:
        session_engine.retry_pending_wakes(current_name, auth_inst.get("identity_id"))
    # Return canonical name so wrapper can track renames
    resp = {"ok": True, "name": current_name}
    if auth_inst:
        resp["pending"] = auth_inst["state"] == "pending"
    return resp


# --- Open agent session in terminal ---

@app.get("/api/platform")
async def get_platform():
    """Return the server's platform so the web UI can match path formats."""
    import sys
    return JSONResponse({"platform": sys.platform})


@app.post("/api/open-path")
async def open_path(body: dict):
    """Open a file or directory in the native file manager.

    Cross-platform: Explorer on Windows, Finder on macOS, xdg-open on Linux.

    Security note: This endpoint is intended for local-only use (127.0.0.1).
    Do not expose this server on a public network without additional access controls.
    """
    import subprocess
    import sys

    path = body.get("path", "")
    if not path:
        return JSONResponse({"error": "no path"}, status_code=400)

    p = Path(path).expanduser().resolve()
    try:
        if sys.platform == "win32":
            if p.is_file():
                subprocess.Popen(["explorer", "/select,", str(p)])
            elif p.is_dir():
                subprocess.Popen(["explorer", str(p)])
            else:
                return JSONResponse({"error": "Path not found", "path": str(p)}, status_code=404)
        elif sys.platform == "darwin":
            if p.is_file():
                subprocess.Popen(["open", "-R", str(p)])
            elif p.is_dir():
                subprocess.Popen(["open", str(p)])
            else:
                return JSONResponse({"error": "Path not found", "path": str(p)}, status_code=404)
        else:
            # Linux — xdg-open opens the containing folder for files
            if p.is_file():
                subprocess.Popen(["xdg-open", str(p.parent)])
            elif p.is_dir():
                subprocess.Popen(["xdg-open", str(p)])
            else:
                return JSONResponse({"error": "Path not found", "path": str(p)}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e), "path": str(p)}, status_code=500)

    return JSONResponse({"ok": True})


# Serve uploaded images
# --- Sessions API ---

@app.get("/api/sessions/templates")
async def get_session_templates():
    if not session_store:
        return JSONResponse({"error": "sessions not configured"}, status_code=500)
    return JSONResponse(session_store.get_templates())


@app.get("/api/sessions/active")
async def get_active_session(channel: str = "general"):
    if not session_engine:
        return JSONResponse(None)
    session = session_engine.get_active(channel)
    return JSONResponse(session)


@app.get("/api/sessions/active-all")
async def get_all_active_sessions():
    if not session_engine:
        return JSONResponse([])
    return JSONResponse(session_engine.list_active())


@app.post("/api/sessions/start")
async def start_session(request: Request):
    if not session_engine or not session_store:
        return JSONResponse({"error": "sessions not configured"}, status_code=500)
    body = await request.json()
    template_id = body.get("template_id", "")
    draft_message_id = body.get("draft_message_id")
    channel = body.get("channel", "general")
    cast = body.get("cast", {})
    goal = body.get("goal", "")
    started_by = body.get("started_by", "user")

    # If running from a draft, load the inline template from message metadata
    tmpl = None
    if draft_message_id:
        draft_msg = store.get_by_id(int(draft_message_id))
        if not draft_msg:
            return JSONResponse({"error": "draft message not found"}, status_code=404)
        meta = draft_msg.get("metadata", {})
        if not meta.get("valid"):
            return JSONResponse({"error": "draft is not valid"}, status_code=400)
        tmpl = meta.get("template")
        if not tmpl:
            return JSONResponse({"error": "draft has no template"}, status_code=400)
        # Register as a temporary template
        template_id = tmpl.get("id", f"draft-{draft_message_id}")
        tmpl["id"] = template_id
        tmpl["is_custom"] = True
        session_store._templates[template_id] = tmpl

    # Validate template exists
    if not tmpl:
        tmpl = session_store.get_template(template_id)
    if not tmpl:
        return JSONResponse({"error": f"unknown template: {template_id}"}, status_code=400)

    # Auto-fill cast from available agents if not fully provided
    if not cast:
        online = registry.get_active_names() if registry else []
        roles = tmpl.get("roles", [])
        cast = _auto_cast(roles, online, started_by)
        if not cast:
            return JSONResponse(
                {"error": "not enough agents online to fill all roles"},
                status_code=400,
            )

    session = session_engine.start_session(template_id, channel, cast, started_by, goal)
    if not session:
        return JSONResponse({"error": "could not start session (one may already be active)"}, status_code=409)

    # Add start banner to chat (only after confirmed success)
    store.add(
        sender="system",
        text=f"Session started: {tmpl.get('name', template_id)}",
        msg_type="session_start",
        channel=channel,
        metadata={"template_id": template_id, "goal": goal, "session_id": session["id"]},
    )
    session_engine.emit_current_phase_banner(session)

    return JSONResponse(session)


@app.post("/api/sessions/{session_id}/end")
async def end_session(session_id: int):
    if not session_engine:
        return JSONResponse({"error": "sessions not configured"}, status_code=500)
    session = session_engine.end_session(session_id)
    if not session:
        return JSONResponse({"error": "session not found or already ended"}, status_code=404)

    # Banner is added by _on_session_change("interrupt", ...) callback
    return JSONResponse(session)


@app.post("/api/sessions/request-draft")
async def request_session_draft(request: Request):
    """Ask an agent to design a session template. Called by the 'Design a session' UI."""
    body = await request.json()
    agent_name = body.get("agent", "").strip()
    description = body.get("description", "").strip()
    channel = body.get("channel", "general")
    sender = body.get("sender", "user")
    if not agent_name or not description:
        return JSONResponse({"error": "agent and description required"}, status_code=400)

    mention_str = f"@{agent_name}"
    store.add(
        "system",
        f"Requested session draft from {mention_str}. Wait for a proposal.",
        channel=channel,
    )
    store.add(
        sender,
        f"{mention_str} Design a session workflow for: **{description}**\n\n"
        "Respond with a single chat message containing a fenced JSON code block with this exact structure:\n"
        "```session\n"
        '{"name": "...", "description": "...", "roles": ["role1", "role2", ...], '
        '"phases": [{"name": "...", "participants": ["role1"], "prompt": "...", "is_output": false}, ...]}\n'
        "```\n"
        "Rules: max 6 roles, max 6 phases, max 4 participants per phase, max 200 chars per prompt. "
        "Mark exactly one phase as `is_output: true` (the final deliverable). "
        f"Keep it focused and sequential. Use the chat_send tool to post your response in the #{channel} channel. "
        "Do NOT respond only in your terminal.",
        channel=channel,
        msg_type="session_request",
        metadata={"session_request": True, "mentions": [f"@{agent_name}"], "request": description},
        sender_kind="human",
    )
    return JSONResponse({"ok": True})


@app.post("/api/sessions/save-draft")
async def save_draft(request: Request):
    if not session_store:
        return JSONResponse({"error": "sessions not configured"}, status_code=500)
    body = await request.json()
    msg_id = body.get("message_id")
    if not msg_id:
        return JSONResponse({"error": "message_id required"}, status_code=400)
    msg = store.get_by_id(int(msg_id))
    if not msg:
        return JSONResponse({"error": "message not found"}, status_code=404)
    meta = msg.get("metadata", {})
    if not meta.get("valid"):
        return JSONResponse({"error": "draft is not valid"}, status_code=400)
    tmpl = meta.get("template")
    if not tmpl:
        return JSONResponse({"error": "no template in draft"}, status_code=400)

    tmpl.setdefault("id", f"custom-{msg_id}")
    session_store.save_custom_template(tmpl)
    return JSONResponse({"ok": True, "template_id": tmpl["id"]})


@app.delete("/api/sessions/templates/{template_id}")
async def delete_session_template(template_id: str):
    if not session_store:
        return JSONResponse({"error": "sessions not configured"}, status_code=500)
    deleted = session_store.delete_custom_template(template_id)
    if not deleted:
        return JSONResponse({"error": "template not found or not custom"}, status_code=404)
    return JSONResponse({"ok": True, "template_id": template_id})


def _auto_cast(roles: list[str], online_agents: list[str], started_by: str) -> dict:
    """Auto-assign roles to available agents. Returns empty dict if not enough agents."""
    cast = {}
    available = list(online_agents)

    for role in roles:
        if not available:
            # Reuse agents if we run out (one agent, multiple roles)
            available = list(online_agents)
        if not available:
            return {}
        agent = available.pop(0)
        cast[role] = agent

    return cast


# --- Version check (GitHub release notifier) ---

_version_cache: dict = {"data": None, "fetched_at": 0.0}
_VERSION_CACHE_TTL = 1800  # 30 minutes


def _read_local_version() -> str:
    """Read version from VERSION file in project root."""
    vfile = Path(__file__).parent / "VERSION"
    try:
        return vfile.read_text().strip()
    except Exception:
        return ""


def _git_probe(*args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=Path(__file__).parent,
        capture_output=True, text=True, timeout=5,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _compute_build_version() -> str:
    """Build string shown in Help: local date-time of the running code's last commit.

    Falls back to this server's start time (also local) when git isn't available.
    """
    from datetime import datetime
    try:
        out = _git_probe("log", "-1", "--format=%ct")
        if out.isdigit():
            return datetime.fromtimestamp(int(out)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    return datetime.now().strftime("%Y-%m-%d %H:%M")


BUILD_VERSION = _compute_build_version()


@app.get("/api/build")
async def get_build():
    return {"build": BUILD_VERSION}


def _detect_install_kind() -> str:
    """Detect how this copy was installed: official_git, fork, or unknown."""
    try:
        url = _git_probe("remote", "get-url", "origin").lower()
        if "bcurts/agentchattr" in url:
            return "official_git"
        elif url:
            return "fork"
    except Exception:
        pass
    return "unknown"


def _fetch_latest_release() -> dict | None:
    """Fetch latest release from GitHub API, with 30-min cache."""
    import time
    import urllib.request

    now = time.time()
    if _version_cache["data"] and (now - _version_cache["fetched_at"]) < _VERSION_CACHE_TTL:
        return _version_cache["data"]

    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/bcurts/agentchattr/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "agentchattr"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            result = {
                "tag": data.get("tag_name", ""),
                "url": data.get("html_url", ""),
            }
            _version_cache["data"] = result
            _version_cache["fetched_at"] = now
            return result
    except Exception:
        return _version_cache.get("data")


def _compare_versions(current: str, latest_tag: str) -> str:
    """Compare version strings. Returns 'behind', 'current', or 'unknown'."""
    # Strip leading 'v' from tag
    latest = latest_tag.lstrip("v")
    if not current or not latest:
        return "unknown"
    try:
        from packaging.version import Version
        if Version(current) < Version(latest):
            return "behind"
        return "current"
    except Exception:
        return "unknown"


@app.get("/api/version_check")
async def version_check():
    """Check for newer releases on GitHub."""
    current = _read_local_version()
    loop = asyncio.get_event_loop()
    release = await loop.run_in_executor(None, _fetch_latest_release)

    if not release or not release.get("tag"):
        return JSONResponse({"current": current, "latest": "", "state": "unknown", "url": ""})

    latest_tag = release["tag"]
    install_kind = _detect_install_kind()
    comparison = _compare_versions(current, latest_tag)

    if comparison == "behind":
        if install_kind == "official_git":
            state = "update_available"
        elif install_kind == "fork":
            state = "upstream_update"
        else:
            state = "unknown"
    elif comparison == "current":
        state = "current"
    else:
        state = "unknown"

    return JSONResponse({
        "current": current,
        "latest": latest_tag,
        "state": state,
        "url": release.get("url", ""),
    })


@app.get("/uploads/{filename}")
async def serve_upload(filename: str):
    upload_dir = Path(config.get("images", {}).get("upload_dir", "./uploads"))
    filepath = (upload_dir / filename).resolve()
    if not filepath.is_relative_to(upload_dir.resolve()):
        return JSONResponse({"error": "invalid path"}, status_code=400)
    if filepath.is_file():
        extension = filepath.suffix.lower()
        if extension == ".svg":
            # A direct navigation opens SVG as a document rather than an image.
            return FileResponse(filepath, media_type="image/svg+xml", headers={
                "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:",
                "X-Content-Type-Options": "nosniff",
            })
        if extension in IMAGE_UPLOAD_EXTS:
            return FileResponse(filepath)
        # Documents are downloads, never executable same-origin pages.
        return FileResponse(filepath, media_type="application/octet-stream",
                            filename=filepath.name,
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
    return JSONResponse({"error": "not found"}, status_code=404)
