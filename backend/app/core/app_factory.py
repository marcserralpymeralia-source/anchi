from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.performance import configure_performance
from app.core.lifespan import app_lifespan
from app.core.middleware import branding_middleware
from app.core.router_registry import get_registered_routers
from app.core.templating import templates
from app.settings.branding import branding_css_vars


def internal_server_error_response(request) -> JSONResponse:  # noqa: ANN001
    request_id = getattr(request.state, "request_id", None)
    correlation_id = getattr(request.state, "correlation_id", None) or request_id
    payload = {
        "error": "internal_error",
        "message": "Ha ocurrido un error interno.",
        "request_id": request_id,
        "correlation_id": correlation_id,
    }
    response = JSONResponse(status_code=500, content=payload)
    if request_id:
        response.headers["X-Request-ID"] = request_id
    if correlation_id:
        response.headers["X-Correlation-ID"] = correlation_id
    return response


def sqlalchemy_error_response(request, exc: Exception):  # noqa: ANN001
    accept = (request.headers.get("accept") or "").lower()
    if request.method in {"GET", "HEAD"} or "text/html" in accept:
        response = RedirectResponse("/login", status_code=303)
        request_id = getattr(request.state, "request_id", None)
        correlation_id = getattr(request.state, "correlation_id", None) or request_id
        if request_id:
            response.headers["X-Request-ID"] = request_id
        if correlation_id:
            response.headers["X-Correlation-ID"] = correlation_id
        return response
    payload = {
        "error": "database_unavailable",
        "message": "La base de datos no está disponible temporalmente.",
        "request_id": getattr(request.state, "request_id", None),
        "correlation_id": getattr(request.state, "correlation_id", None) or getattr(request.state, "request_id", None),
    }
    response = JSONResponse(status_code=503, content=payload)
    request_id = payload["request_id"]
    correlation_id = payload["correlation_id"]
    if request_id:
        response.headers["X-Request-ID"] = request_id
    if correlation_id:
        response.headers["X-Correlation-ID"] = correlation_id
    return response


def create_app() -> FastAPI:
    configure_logging()
    configure_performance()
    settings = get_settings()
    app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=app_lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.app_secret_key,
        session_cookie=settings.session_cookie,
        https_only=bool(settings.session_cookie_secure),
        same_site=settings.session_cookie_samesite or "lax",
        max_age=settings.session_max_age,
        domain=settings.session_cookie_domain or None,
    )
    if settings.allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    cors_origins = settings.cors_allowed_origins
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.middleware("http")(branding_middleware)
    static_dir = Path(__file__).resolve().parents[1] / "static"
    app.mount("/static", StaticFiles(directory=static_dir.as_posix()), name="static")
    templates.env.globals["branding_css_vars"] = branding_css_vars
    templates.env.globals["app_settings"] = settings

    for router in get_registered_routers():
        app.include_router(router)

    @app.exception_handler(Exception)
    async def _fallback_exception_handler(request, exc):  # noqa: ANN001
        return internal_server_error_response(request)

    @app.exception_handler(SQLAlchemyError)
    async def _sqlalchemy_exception_handler(request, exc):  # noqa: ANN001
        return sqlalchemy_error_response(request, exc)

    return app
