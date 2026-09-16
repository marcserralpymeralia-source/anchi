from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request

CSRF_SESSION_KEY = "_csrf_token"
CSRF_FORM_FIELD = "_csrf_token"
CSRF_HEADER = "X-CSRF-Token"
CSRF_COOKIE = "anchi_csrf"


def get_or_create_token(request: Request) -> str:
    """Return a double-submit token without adding data to the auth cookie."""

    token = request.cookies.get(CSRF_COOKIE)
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        request.state.csrf_token_needs_cookie = True
    else:
        request.state.csrf_token_needs_cookie = False
    request.state.csrf_token = token
    return token


def tokens_match(expected: str | None, supplied: str | None) -> bool:
    if not expected or not supplied:
        return False
    return hmac.compare_digest(str(expected), str(supplied))


async def validate_request_csrf(request: Request) -> None:
    """Validate browser mutations after FastAPI has parsed the request body."""

    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    path = request.url.path or "/"
    if path.startswith("/cron"):
        return
    if path.startswith("/webhooks/whatsapp/") and not path.endswith("/respond"):
        return
    from app.core.config import get_settings

    if not get_settings().csrf_protection_enabled:
        return
    supplied = request.headers.get(CSRF_HEADER)
    if not supplied and (request.headers.get("content-type") or "").lower().startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    ):
        form = await request.form()
        supplied = form.get(CSRF_FORM_FIELD)
    if not tokens_match(getattr(request.state, "csrf_token", None), supplied):
        raise HTTPException(status_code=403, detail="Token CSRF no válido o ausente")
