from __future__ import annotations

import json
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user, require_tenant_role
from app.core.templating import templates
from app.db.models import Order, OrderRoute
from app.integrations.service import (
    IntegrationConfigurationError,
    ROLE_CAPABILITIES,
    ROLE_LABELS,
    activate_connection,
    connection_to_dict,
    create_connection,
    disable_connection,
    get_connection,
    health_check_connection,
    list_connections,
    list_integration_roles,
    set_integration_role,
    set_order_route,
    update_connection,
)
from app.connectors.registry import IntegrationRegistry
from app.master.service import TenantUser
from app.tenancy.database import get_tenant_db


router = APIRouter(tags=["integrations"])
registry = IntegrationRegistry()


def _admin_user(user: TenantUser = Depends(require_tenant_role("Administrador", "Superadmin"))) -> TenantUser:
    return user


async def _payload(request: Request) -> dict[str, Any]:
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        payload = await request.json()
        return payload if isinstance(payload, dict) else {}
    form = await request.form()
    raw_config = form.get("config") or form.get("config_json") or "{}"
    try:
        config = json.loads(str(raw_config)) if isinstance(raw_config, str) else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="config debe ser JSON válido") from exc
    return {**dict(form), "config": config}


def _error_response(exc: IntegrationConfigurationError) -> JSONResponse:
    return JSONResponse({"ok": False, "message": str(exc), "errors": exc.errors}, status_code=422)


def _route_payload(db: Session, company_id: int) -> dict[str, Any]:
    route = db.scalar(select(OrderRoute).where(OrderRoute.company_id == company_id, OrderRoute.order_type == "order"))
    return {
        "order_type": route.order_type if route else "order",
        "source_connection_id": route.source_connection_id if route else None,
        "destination_connection_id": route.destination_connection_id if route else None,
        "destination_connector_key": route.destination_connector_key if route else None,
        "is_active": bool(route and route.is_active),
    }


@router.get("/integrations/definitions")
def integration_definitions(user: TenantUser = Depends(current_user)):
    environment = os.getenv("APP_ENV", "development").strip().lower()
    return {"items": [definition.to_dict(environment=environment) for definition in registry.list_definitions(environment=environment)]}


@router.get("/integrations/connections")
def integration_connections(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return {"company_id": user.company_id, "items": [connection_to_dict(item, registry=registry) for item in list_connections(db, company_id=user.company_id)]}


@router.post("/integrations/connections")
async def create_integration_connection(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(_admin_user)):
    payload = await _payload(request)
    connector_key = str(payload.get("connector_key") or "").strip()
    try:
        registry.definition(connector_key)
        if connector_key not in {item.key for item in registry.list_definitions()}:
            raise IntegrationConfigurationError("Este connector no está disponible en el entorno actual.")
        connection = create_connection(
            db,
            company_id=user.company_id,
            connector_key=connector_key,
            name=str(payload.get("name") or ""),
            config=payload.get("config") if isinstance(payload.get("config"), dict) else {},
            secret_provider_ref=payload.get("secret_provider_ref"),
            credentials_configured=bool(payload.get("credentials_configured", False)),
            registry=registry,
        )
        db.commit()
        return JSONResponse({"ok": True, "connection": connection_to_dict(connection, registry=registry)}, status_code=201)
    except IntegrationConfigurationError as exc:
        db.rollback()
        return _error_response(exc)
    except KeyError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/integrations/connections/{connection_id}")
async def patch_integration_connection(connection_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(_admin_user)):
    payload = await _payload(request)
    try:
        connection = update_connection(
            db,
            company_id=user.company_id,
            connection_id=connection_id,
            name=payload.get("name"),
            config=payload.get("config") if isinstance(payload.get("config"), dict) else None,
            secret_provider_ref=payload.get("secret_provider_ref"),
            registry=registry,
        )
        db.commit()
        return {"ok": True, "connection": connection_to_dict(connection, registry=registry)}
    except IntegrationConfigurationError as exc:
        db.rollback()
        return _error_response(exc)
    except KeyError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/integrations/connections/{connection_id}/enable")
def enable_integration_connection(connection_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(_admin_user)):
    try:
        connection = activate_connection(db, company_id=user.company_id, connection_id=connection_id, registry=registry)
        db.commit()
        return {"ok": True, "connection": connection_to_dict(connection, registry=registry)}
    except IntegrationConfigurationError as exc:
        db.rollback()
        return _error_response(exc)
    except KeyError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/integrations/connections/{connection_id}/disable")
def disable_integration_connection(connection_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(_admin_user)):
    try:
        connection = disable_connection(db, company_id=user.company_id, connection_id=connection_id)
        db.commit()
        return {"ok": True, "connection": connection_to_dict(connection, registry=registry)}
    except KeyError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/integrations/connections/{connection_id}/validate")
def validate_integration_connection(connection_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    connection = get_connection(db, company_id=user.company_id, connection_id=connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Conexión no encontrada")
    return {"ok": True, "connection": connection_to_dict(connection, registry=registry)}


@router.post("/integrations/connections/{connection_id}/health")
def check_integration_health(connection_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    try:
        connection = health_check_connection(db, company_id=user.company_id, connection_id=connection_id, registry=registry)
        db.commit()
        return {"ok": True, "connection": connection_to_dict(connection, registry=registry)}
    except KeyError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/integrations/connections/{connection_id}/dry-run")
async def dry_run_integration_connection(connection_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(_admin_user)):
    payload = await _payload(request)
    try:
        order_id = int(payload.get("order_id"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="order_id debe ser un entero.") from exc
    connection = get_connection(db, company_id=user.company_id, connection_id=connection_id)
    order = db.scalar(select(Order).where(Order.id == order_id, Order.company_id == user.company_id))
    if connection is None or order is None:
        raise HTTPException(status_code=404, detail="Conexión o pedido no encontrado.")
    if connection.connector_key != "generic_rest":
        raise HTTPException(status_code=400, detail="dry-run solo está disponible para Generic REST.")
    try:
        connector = registry.resolve(db, company_id=user.company_id, connection_id=connection_id)
        dry_run = getattr(connector, "dry_run_order", None)
        if not callable(dry_run):
            raise HTTPException(status_code=400, detail="El connector no soporta dry-run.")
        return {"ok": True, "dry_run": dry_run(db, order)}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=422)


@router.get("/integrations/roles")
def integration_roles(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return {
        "items": [
            {"role_key": role.role_key, "label": ROLE_LABELS.get(role.role_key, role.role_key), "connection_id": role.connection_id}
            for role in list_integration_roles(db, company_id=user.company_id)
        ],
        "available_roles": [{"key": key, "label": ROLE_LABELS[key], "capability": capability} for key, capability in ROLE_CAPABILITIES.items()],
    }


@router.put("/integrations/roles/{role_key}")
async def update_integration_role(role_key: str, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(_admin_user)):
    payload = await _payload(request)
    try:
        role = set_integration_role(
            db,
            company_id=user.company_id,
            role_key=role_key,
            connection_id=int(payload.get("connection_id")),
            registry=registry,
        )
        db.commit()
        return {"ok": True, "role_key": role.role_key, "connection_id": role.connection_id}
    except (TypeError, ValueError) as exc:
        db.rollback()
        return JSONResponse({"ok": False, "message": "connection_id debe ser un entero."}, status_code=400)
    except IntegrationConfigurationError as exc:
        db.rollback()
        return _error_response(exc)
    except KeyError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/integrations/routes")
def integration_routes(db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return {"items": [_route_payload(db, user.company_id)]}


@router.put("/integrations/routes/{order_type}")
async def update_integration_route(order_type: str, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(_admin_user)):
    payload = await _payload(request)
    try:
        route = set_order_route(
            db,
            company_id=user.company_id,
            order_type=order_type,
            source_connection_id=int(payload["source_connection_id"]) if payload.get("source_connection_id") not in {None, ""} else None,
            destination_connection_id=int(payload["destination_connection_id"]),
            registry=registry,
        )
        db.commit()
        return {"ok": True, "route": _route_payload(db, user.company_id), "route_id": route.id}
    except (TypeError, ValueError, KeyError) as exc:
        db.rollback()
        if isinstance(exc, KeyError) and str(exc).strip("'") == "destination_connection_id":
            return JSONResponse({"ok": False, "message": "Falta destination_connection_id."}, status_code=400)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IntegrationConfigurationError as exc:
        db.rollback()
        return _error_response(exc)


@router.get("/settings/integrations")
def integrations_settings_page(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    connections = list_connections(db, company_id=user.company_id)
    return templates.TemplateResponse(
        "settings/integrations.html",
        {
            "request": request,
            "user": user,
            "connections": [connection_to_dict(item, registry=registry) for item in connections],
            "definitions": [item.to_dict(environment=os.getenv("APP_ENV", "development")) for item in registry.list_definitions()],
            "roles": list_integration_roles(db, company_id=user.company_id),
            "role_labels": ROLE_LABELS,
            "route": _route_payload(db, user.company_id),
            "can_edit": user.role.name in {"Administrador", "Superadmin"},
        },
    )


__all__ = ["router"]
