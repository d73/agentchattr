"""Attribute local browser approvals to the configured operator."""

import hmac
from collections.abc import Callable
from ipaddress import IPv6Address, ip_address


def configured_operator(config) -> str | None:
    operator = config.get("operator")
    identity = operator.get("identity") if isinstance(operator, dict) else None
    return identity.strip() if isinstance(identity, str) and identity.strip() else None


def verify_operator(
    request, config: dict, session_token: str, *,
    room_username: str, is_host_interface: Callable[[str], bool] | None = None,
    browser_token: str | None = None,
) -> str | None:
    """Attribute a direct browser request from loopback or an allowed host interface."""
    session = request.headers.get("x-session-token", "") if browser_token is None else browser_token
    if (not session_token or not hmac.compare_digest(session.encode(), session_token.encode())
            or any(header in request.headers for header in (
                "authorization", "x-agent-token", "forwarded", "x-forwarded-for"
            ))
            or request.client is None):
        return None
    try:
        address = ip_address(request.client.host)
    except ValueError:
        return None
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if not address.is_loopback and (
        is_host_interface is None or not is_host_interface(request.client.host)
    ):
        return None
    username = room_username.strip() if isinstance(room_username, str) else ""
    return configured_operator(config) or username or "user"
