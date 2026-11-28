"""Attribute local browser approvals to the configured operator."""

import hmac
from ipaddress import IPv6Address, ip_address


def configured_operator(config) -> str | None:
    operator = config.get("operator")
    identity = operator.get("identity") if isinstance(operator, dict) else None
    return identity.strip() if isinstance(identity, str) and identity.strip() else None


def verify_operator(request, config: dict, session_token: str) -> str | None:
    """Return the configured attribution for a direct loopback browser request."""
    identity = configured_operator(config)
    session = request.headers.get("x-session-token", "")
    if (identity is None
            or not session_token or not hmac.compare_digest(session.encode(), session_token.encode())
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
    return identity if address.is_loopback else None
