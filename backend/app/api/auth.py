"""Single-user session authentication (HMAC-signed cookie) + CSRF guard."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from fastapi import HTTPException, Request

COOKIE = "pt_session"
CSRF_HEADER = "x-requested-with"


def _sign(key: bytes, payload: bytes) -> str:
    return base64.urlsafe_b64encode(hmac.new(key, payload, hashlib.sha256).digest()).decode().rstrip("=")


def make_token(key: bytes, hours: int) -> str:
    payload = json.dumps({"exp": int(time.time() + hours * 3600)}).encode()
    p64 = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{p64}.{_sign(key, payload)}"


def verify_token(key: bytes, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    p64, sig = token.rsplit(".", 1)
    try:
        payload = base64.urlsafe_b64decode(p64 + "=" * (-len(p64) % 4))
    except (ValueError, TypeError):
        return False
    if not hmac.compare_digest(_sign(key, payload), sig):
        return False
    try:
        return json.loads(payload).get("exp", 0) > time.time()
    except ValueError:
        return False


def require_session(request: Request) -> None:
    c = request.app.state.container
    if not verify_token(c.config.secret_key, request.cookies.get(COOKIE)):
        raise HTTPException(status_code=401, detail="Not authenticated")
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.headers.get(CSRF_HEADER) != "portfolio-tracker":
        raise HTTPException(status_code=403, detail="Missing CSRF header")
