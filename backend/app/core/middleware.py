from __future__ import annotations

import logging
import uuid
import time
from collections.abc import Awaitable, Callable

from fastapi import Request

from app.core.config import get_settings
from app.core.metrics import record_request
from app.core.observability import current_context, observability_scope
from app.master.database import MasterSessionLocal
from app.master.service import load_tenant_context
from app.settings.branding import branding_to_dict, default_branding_payload, get_or_create_branding

_BRANDING_CACHE: dict[int, tuple[float, dict]] = {}
logger = logging.getLogger(__name__)


def default_alert_center_context() -> dict:
    return {
        "total": 0,
        "critical": 0,
        "high": 0,
        "medium": 0,
        "low": 0,
        "info": 0,
        "has_critical": False,
        "recent": [],
    }


def branding_to_dict_from_payload() -> dict:
    return branding_to_dict(default_branding_payload())


def _branding_cache_ttl() -> int:
    settings = get_settings()
    return max(int(getattr(settings, "branding_cache_ttl_seconds", 30)), 5)


def _get_cached_branding(company_id: int) -> dict | None:
    cached = _BRANDING_CACHE.get(company_id)
    if not cached:
        return None
    expires_at, value = cached
    if expires_at < time.time():
        _BRANDING_CACHE.pop(company_id, None)
        return None
    return value


def _set_cached_branding(company_id: int, value: dict) -> None:
    _BRANDING_CACHE[company_id] = (time.time() + _branding_cache_ttl(), value)


async def branding_middleware(request: Request, call_next: Callable[[Request], Awaitable]):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    correlation_id = request.headers.get("x-correlation-id") or request_id
    request.state.request_id = request_id
    request.state.correlation_id = correlation_id
    master_db = MasterSessionLocal()
    tenant = None
    try:
        tenant = load_tenant_context(request, master_db)
        if tenant:
            request.state.tenant = tenant
        company_id = tenant.company.id if tenant else None
        observability_values = {
            "request_id": request_id,
            "correlation_id": correlation_id,
            "tenant_id": company_id,
            "tenant_slug": tenant.company.slug if tenant else None,
            "user_id": tenant.user.id if tenant and tenant.user else None,
            "membership_id": tenant.user.membership_id if tenant and tenant.user else None,
            "route": request.url.path,
            "method": request.method,
        }
        with observability_scope(**observability_values):
            request.state.observability = current_context()
            if company_id:
                cached = _get_cached_branding(company_id)
                if cached is not None:
                    request.state.branding = cached
                elif tenant.company.database_url:
                    db = None
                    try:
                        from app.tenancy.database import tenant_db_session

                        db = tenant_db_session(tenant.company.database_url)()
                        branding = get_or_create_branding(db, company_id)
                        payload = branding_to_dict(branding)
                        request.state.branding = payload
                        _set_cached_branding(company_id, payload)
                    except Exception:
                        logger.exception("No se pudo cargar branding para company_id=%s", company_id)
                        request.state.branding = branding_to_dict_from_payload()
                    finally:
                        if db is not None:
                            db.close()
                else:
                    request.state.branding = branding_to_dict_from_payload()
            else:
                request.state.branding = branding_to_dict_from_payload()
            request.state.alert_center = default_alert_center_context()
            start = time.perf_counter()
            logger.info(
                "request.start",
                extra={
                    "event": "request.start",
                    "path": request.url.path,
                    "method": request.method,
                    "tenant_id": company_id,
                },
            )
            try:
                response = await call_next(request)
            except Exception:
                duration = time.perf_counter() - start
                record_request(route=request.url.path, status_code=500, duration_seconds=duration)
                logger.exception(
                    "request.error",
                    extra={
                        "event": "request.error",
                        "path": request.url.path,
                        "method": request.method,
                        "tenant_id": company_id,
                        "duration_ms": round(duration * 1000, 2),
                    },
                )
                raise
            duration = time.perf_counter() - start
            record_request(route=request.url.path, status_code=response.status_code, duration_seconds=duration)
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Correlation-ID"] = correlation_id
            logger.info(
                "request.end",
                extra={
                    "event": "request.end",
                    "path": request.url.path,
                    "method": request.method,
                    "status_code": response.status_code,
                    "duration_ms": round(duration * 1000, 2),
                    "tenant_id": company_id,
                },
            )
            return response
    finally:
        master_db.close()
