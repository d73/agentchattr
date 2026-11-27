"""Scoped local interfaces for the orchestrator status and Job closeout."""

import hmac
import json
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(ProxyHandler({}), NoRedirect())


def prepare_credentials(directory):
    path = directory / ".orchestrator.env"
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write("ORCH_STATUS_TOKEN=\nORCH_CLOSEOUT_TOKEN=\n")
    except FileExistsError:
        pass
    return path


def read_secret(config, name):
    data = config.get("server", {}).get("data_dir", "./data")
    path = Path(data) / ".orchestrator.env"
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return None
    values = []
    for line in lines:
        key, delimiter, value = line.partition("=")
        if delimiter and key.strip() == name:
            values.append(value.strip())
    if len(values) != 1 or not 32 <= len(values[0]) <= 512:
        return None
    token = values[0]
    return token if token.isascii() and not any(char.isspace() for char in token) else None


def authorized_closeout(config, header):
    expected = read_secret(config, "ORCH_CLOSEOUT_TOKEN")
    given = header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""
    return bool(expected and given.isascii() and hmac.compare_digest(given, expected))


def status_snapshot(config):
    offline = {"version": 1, "daemon": {"state": "offline", "last_successful_tick": None,
                                      "timestamp": None}, "pipeline": {}, "staffing": {}, "jobs": []}
    settings = config.get("orchestrator", {})
    if not isinstance(settings, dict):
        return offline
    url = settings.get("status_url", "")
    token = read_secret(config, "ORCH_STATUS_TOKEN")
    if not isinstance(url, str) or not token:
        return offline
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
                or parsed.netloc != f"127.0.0.1:{parsed.port}" or parsed.path != "/status"
                or parsed.query or parsed.fragment):
            return offline
        request = Request(url, headers={"Authorization": f"Bearer {token}"})
        with _OPENER.open(request, timeout=2) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            return offline
        value = json.loads(raw)
        daemon = value["daemon"]
        if (value.get("version") == 1 and daemon.get("state") in {"online", "stale", "offline"}
                and isinstance(value.get("pipeline"), dict)
                and isinstance(value.get("staffing"), dict)
                and isinstance(value.get("jobs"), list)):
            return {key: value[key] for key in offline}
    except (URLError, OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return offline
