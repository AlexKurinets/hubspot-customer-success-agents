"""HubSpot webhook verification, v3 scheme: base64 HMAC-SHA256 over method + URI + body + timestamp,
keyed with the app's client secret. Requests older than five minutes are rejected as replays."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

MAX_AGE_MS = 5 * 60 * 1000


def signature(secret: str, method: str, uri: str, body: str, timestamp: str) -> str:
    digest = hmac.new(secret.encode(), f"{method}{uri}{body}{timestamp}".encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def verify(secret: str, method: str, uri: str, body: str, timestamp: str | None, given: str | None,
           now_ms: int | None = None) -> str | None:
    """Returns None when the request is genuine, otherwise the reason it was rejected."""
    if not timestamp or not given:
        return "missing signature headers"
    try:
        age = (now_ms if now_ms is not None else int(time.time() * 1000)) - int(timestamp)
    except ValueError:
        return "bad timestamp"
    if age > MAX_AGE_MS:
        return "timestamp older than five minutes"
    if not hmac.compare_digest(signature(secret, method, uri, body, timestamp), given):
        return "signature mismatch"
    return None
