"""Keep credentials out of diagnostic records, including nested audit data."""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

_SECRET = re.compile(
    r"(?i)(access_token|refresh_token|client_secret|code_verifier|authorization_code|"
    r"oauth_state|owner_password|password|authorization|token|secret|state|code)"
)
_ASSIGNMENT = re.compile(
    r"(?i)\b(access_token|refresh_token|client_secret|code_verifier|authorization_code|"
    r"oauth_state|owner_password|password|token|secret|state|code)\b([\s\"']*[:=][\s\"']*)([^\s&,;\"'}]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;\"']+")
_URL = re.compile(r"https?://[^\s<>\"']+")


def _public_url(match: re.Match) -> str:
    try:
        parts = urlsplit(match.group())
        return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))
    except ValueError:
        return "[redacted-url]"


def redact_text(value: str) -> str:
    value = _URL.sub(_public_url, value)
    value = _BEARER.sub("Bearer [redacted]", value)
    return _ASSIGNMENT.sub(lambda m: m[1] + m[2] + "[redacted]", value)


def audit_value(value):
    if isinstance(value, dict):
        return {str(key): "[redacted]" if _SECRET.fullmatch(str(key)) else audit_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [audit_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
