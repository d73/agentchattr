"""Verify a configured operator independently of AgentChattr's browser session."""

import hashlib
import hmac
from pathlib import Path
import re

PROOF_FILE = ".orch-operator.env"
PROOF_KEY = "ORCH_OPERATOR_PROOF_SHA256"
_HEX_PROOF = re.compile(r"[0-9a-f]{64}", re.ASCII)


def configured_operator(config) -> str | None:
    operator = config.get("operator")
    identity = operator.get("identity") if isinstance(operator, dict) else None
    return identity.strip() if isinstance(identity, str) and identity.strip() else None


def prepare_operator_verifier(data_dir: Path) -> None:
    try:
        with (data_dir / PROOF_FILE).open("x", encoding="utf-8") as target:
            target.write(f"{PROOF_KEY}=\n")
    except FileExistsError:
        pass


def verify_operator(request, config: dict, session_token: str) -> str | None:
    """Return only a configured, verified operator; ignore sender/display names."""
    identity = configured_operator(config)
    session = request.headers.get("x-session-token", "")
    proof = request.headers.get("x-orch-operator-proof", "")
    if (identity is None
            or not session_token or not hmac.compare_digest(session.encode(), session_token.encode())
            or "authorization" in request.headers or "x-agent-token" in request.headers
            or not _HEX_PROOF.fullmatch(proof)):
        return None

    path = Path(config.get("server", {}).get("data_dir", "./data")) / PROOF_FILE
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return None
    verifiers = []
    for line in lines:
        name, separator, value = line.partition("=")
        if separator and name.strip() == PROOF_KEY:
            verifiers.append(value.strip())
    if len(verifiers) != 1 or not _HEX_PROOF.fullmatch(verifiers[0]):
        return None
    digest = hashlib.sha256(proof.encode("ascii")).hexdigest()
    return identity if hmac.compare_digest(digest, verifiers[0]) else None
