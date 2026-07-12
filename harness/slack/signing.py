import hashlib
import hmac
import time


def is_valid_slack_signature(
    *,
    signing_secret: str,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    now: int | None = None,
) -> bool:
    if not signing_secret or not timestamp or not signature:
        return False

    try:
        request_ts = int(timestamp)
    except ValueError:
        return False

    current = int(time.time()) if now is None else now
    if abs(current - request_ts) > 60 * 5:
        return False

    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
