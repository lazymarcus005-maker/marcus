import re
from pathlib import Path

_SECRET_FILE_NAMES = frozenset({".env", ".env.local", ".env.production", "credentials.json"})
_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S
)
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)(api[_-]?key|secret|password|token|authorization)"
    r"(\s*[:=]\s*)(['\"]?)[^\s'\"]+\3"
)


def redact_secrets(path: str, content: str) -> tuple[str, int]:
    """Remove common credentials before file contents enter model context."""
    name = Path(path).name.lower()
    if name in _SECRET_FILE_NAMES or name.endswith((".pem", ".key")):
        return "[REDACTED SENSITIVE FILE CONTENT]", 1

    redacted, private_keys = _PRIVATE_KEY_PATTERN.subn("[REDACTED PRIVATE KEY]", content)
    redacted, credentials = _CREDENTIAL_PATTERN.subn(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", redacted
    )
    return redacted, private_keys + credentials
