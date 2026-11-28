"""Attribute local browser approvals to the configured operator."""

import hmac
from ipaddress import IPv6Address, ip_address


def configured_operator(config) -> str | None:
    operator = config.get("operator")
    identity = operator.get("identity") if isinstance(operator, dict) else None
    return identity.strip() if isinstance(identity, str) and identity.strip() else None


def verify_operator(request, config: dict, session_token: str, *, room_username: str) -> str | None:
    """Return the configured attribution for a direct loopback browser request."""
    session = request.headers.get("x-session-token", "")
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
    if not address.is_loopback:
        return None
    username = room_username.strip() if isinstance(room_username, str) else ""
    return configured_operator(config) or username or "user"
