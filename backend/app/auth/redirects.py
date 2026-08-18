from __future__ import annotations

from urllib.parse import quote, urlsplit

from fastapi import Request


DEFAULT_LOGIN_DESTINATION = "/inicio"


def safe_internal_next(value: str | None, *, default: str = DEFAULT_LOGIN_DESTINATION) -> str:
    if not value or not isinstance(value, str):
        return default
    candidate = value.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return default
    if not candidate.startswith("/") or candidate.startswith("//"):
        return default
    return candidate


def requested_path(request: Request) -> str:
    path = request.url.path or DEFAULT_LOGIN_DESTINATION
    query = getattr(request.url, "query", "")
    return f"{path}?{query}" if query else path


def login_location_for_request(request: Request) -> str:
    destination = safe_internal_next(requested_path(request))
    return f"/login?next={quote(destination, safe='')}"
