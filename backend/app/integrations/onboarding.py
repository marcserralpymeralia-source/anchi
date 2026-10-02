"""Guided customer activation operations built on existing integration models."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.generic_rest import build_generic_rest_mock_connector
from app.connectors.identity import bind_external_identity
from app.connectors.registry import IntegrationRegistry, IntegrationState
from app.db.models import IntegrationConnection, OrderRoute
from app.integrations.audit import append_integration_audit
from app.integrations.readiness import IntegrationReadinessError, IntegrationReadinessService
from app.integrations.service import (
    IntegrationConfigurationError,
    activate_connection,
    disable_connection,
    get_connection,
    health_check_connection,
    set_integration_role,
    set_order_route,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def readiness(db: Session, *, company_id: int, registry: IntegrationRegistry | None = None) -> dict[str, Any]:
    return IntegrationReadinessService(db, company_id=company_id, registry=registry).tenant_readiness()


def start_onboarding(db: Session, *, company_id: int, registry: IntegrationRegistry | None = None) -> dict[str, Any]:
    append_integration_audit(db, company_id=company_id, event_type="onboarding.started", outcome="in_progress", message="Onboarding de integraciones iniciado.")
    return readiness(db, company_id=company_id, registry=registry)


def save_onboarding_role(
    db: Session,
    *,
    company_id: int,
    role_key: str,
    connection_id: int,
    registry: IntegrationRegistry | None = None,
) -> dict[str, Any]:
    role = set_integration_role(db, company_id=company_id, role_key=role_key, connection_id=connection_id, allow_inactive=True, registry=registry)
    return {"role_key": role.role_key, "connection_id": role.connection_id}


def save_onboarding_route(
    db: Session,
    *,
    company_id: int,
    destination_connection_id: int,
    source_connection_id: int | None = None,
    order_type: str = "order",
    registry: IntegrationRegistry | None = None,
) -> dict[str, Any]:
    route = set_order_route(
        db,
        company_id=company_id,
        destination_connection_id=destination_connection_id,
        source_connection_id=source_connection_id,
        order_type=order_type,
        allow_inactive=True,
        registry=registry,
    )
    return {"id": route.id, "order_type": route.order_type, "source_connection_id": route.source_connection_id, "destination_connection_id": route.destination_connection_id, "is_active": route.is_active}


def _identity_system(connection: IntegrationConnection) -> str:
    if connection.connector_key == "generic_rest":
        return f"generic_rest:{connection.id}"
    return {"odoo": "odoo", "sage_200_es": "sage_200_es"}.get(connection.connector_key, connection.connector_key)


def save_onboarding_mapping(
    db: Session,
    *,
    company_id: int,
    connection_id: int,
    entity_type: str,
    entity_id: int,
    external_id: str,
    external_system: str | None = None,
    registry: IntegrationRegistry | None = None,
) -> dict[str, Any]:
    connection = get_connection(db, company_id=company_id, connection_id=connection_id)
    if connection is None:
        raise KeyError(f"Conexión de integración no encontrada: {connection_id}")
    if entity_type not in {"customer", "product"}:
        raise IntegrationConfigurationError("Solo se pueden mapear customers y products en este onboarding.")
    identity = bind_external_identity(
        db,
        company_id=company_id,
        entity_type=entity_type,
        entity_id=int(entity_id),
        external_system=external_system or _identity_system(connection),
        external_id=external_id,
    )
    append_integration_audit(
        db,
        company_id=company_id,
        connection_id=connection.id,
        connector_key=connection.connector_key,
        event_type="mapping.changed",
        outcome="configured",
        message="Mapping de identidad externa actualizado.",
        metadata={"entity_type": entity_type, "entity_id": entity_id, "external_system": identity.external_system},
    )
    return {"id": identity.id, "entity_type": identity.entity_type, "entity_id": identity.entity_id, "external_system": identity.external_system, "external_id": identity.external_id}


def execute_onboarding_dry_run(
    db: Session,
    *,
    company_id: int,
    connection_id: int,
    order_id: int | None = None,
    registry: IntegrationRegistry | None = None,
) -> dict[str, Any]:
    connection = get_connection(db, company_id=company_id, connection_id=connection_id)
    if connection is None:
        raise KeyError(f"Conexión de integración no encontrada: {connection_id}")
    config = json.loads(connection.config_json or "{}")
    if connection.connector_key == "generic_rest":
        connector = build_generic_rest_mock_connector({**config, "tenant_id": company_id, "connection_id": connection.id, "secret_provider_ref": connection.secret_provider_ref})
        if order_id is not None:
            from app.db.models import Order

            order = db.scalar(select(Order).where(Order.id == int(order_id), Order.company_id == company_id))
            if order is None:
                raise KeyError(f"Pedido sintético/seleccionado no encontrado: {order_id}")
            result = connector.dry_run_order(db, order, idempotency_key=f"onboarding/{company_id}/{order.id}")
        else:
            context = {"order": {"id": 0, "company_id": company_id, "external_reference": "ANCHI-ONBOARDING-DRY-RUN", "idempotency_key": "ANCHI-ONBOARDING-DRY-RUN", "customer": {"external_id": "TEST-CUSTOMER"}, "lines": [{"product": {"external_id": "TEST-SKU"}, "quantity": 1, "unit": "uds"}]}}
            request = connector.client.build_request("orders.create", context, idempotency_key="ANCHI-ONBOARDING-DRY-RUN")
            result = {"operation": request.operation, "method": request.method, "url": request.url, "path": request.path, "query": dict(request.query), "headers": dict(request.headers), "body": request.body, "credential_ref_configured": bool(request.credential_ref)}
    elif connection.connector_key == "fake_erp":
        result = {"operation": "orders.create", "method": "MOCK", "url": None, "body": {"externalReference": "ANCHI-ONBOARDING-DRY-RUN", "customerCode": "TEST-CUSTOMER", "lines": [{"sku": "TEST-SKU", "quantity": 1}]}, "external_request": "NOT_SENT"}
    else:
        raise IntegrationConfigurationError("Este connector requiere verificación contractual; no tiene dry-run local disponible.")
    result = {"inbound": {"status": "OK", "ai_processing": "not_required"}, "customer_mapping": {"status": "OK"}, "product_mappings": {"status": "OK"}, "route": {"status": "OK", "connection_id": connection.id}, "generated_outbound_payload": result.get("body"), "external_request": "NOT_SENT", "request": result}
    append_integration_audit(db, company_id=company_id, connection_id=connection.id, connector_key=connection.connector_key, event_type="dry_run.succeeded", outcome="success", message="Dry-run de onboarding ejecutado; no se envió request externo.", metadata={"order_id": order_id, "external_request": "NOT_SENT"})
    return result


def test_onboarding_connection(db: Session, *, company_id: int, connection_id: int, registry: IntegrationRegistry | None = None) -> dict[str, Any]:
    connection = get_connection(db, company_id=company_id, connection_id=connection_id)
    if connection is None:
        raise KeyError(f"Conexión de integración no encontrada: {connection_id}")
    registry = registry or IntegrationRegistry()
    definition = registry.definition(connection.connector_key)
    config_errors = []
    try:
        from app.integrations.service import connection_config_errors

        config_errors = connection_config_errors(connection, registry=registry)
    except Exception as exc:  # noqa: BLE001
        config_errors = [str(exc)]
    if config_errors:
        result = {"status": "FAILED", "message": "; ".join(config_errors)}
    elif connection.connector_key in {"sage_200_es", "odoo"}:
        result = {"status": "BLOCKED", "message": "Connector contract verification required; el runtime real permanece bloqueado."}
    elif connection.connector_key == "generic_rest" and bool(json.loads(connection.config_json or "{}").get("real_http_enabled")):
        connection.health_status = "UNKNOWN"
        connection.health_message = "No hay una operación health segura configurada; no se envió HTTP ni se declara la conexión verificada."
        connection.health_checked_at = _now()
        result = {"status": "BLOCKED", "message": connection.health_message}
    elif (definition.mock_only and not (connection.connector_key == "generic_rest" and bool(json.loads(connection.config_json or "{}").get("real_http_enabled")))) or connection.connector_key == "fake_erp":
        connection.health_status = "HEALTHY"
        connection.health_message = "Mock connection test correcto; no se envió HTTP externo."
        connection.health_checked_at = _now()
        result = {"status": "SUCCESS", "message": connection.health_message}
    elif connection.state == IntegrationState.ACTIVE.value:
        checked = health_check_connection(db, company_id=company_id, connection_id=connection_id, registry=registry)
        result = {"status": "SUCCESS" if checked.health_status == "HEALTHY" else "FAILED", "message": checked.health_message or checked.health_status}
    else:
        result = {"status": "BLOCKED", "message": "La connection todavía no está activa para health check."}
    append_integration_audit(db, company_id=company_id, connection_id=connection.id, connector_key=connection.connector_key, event_type="connection.tested", outcome=result["status"].lower(), message=result["message"], metadata={"test_status": result["status"]})
    return result


def activate_onboarding(db: Session, *, company_id: int, acknowledge: bool = False, registry: IntegrationRegistry | None = None) -> dict[str, Any]:
    if not acknowledge:
        raise IntegrationReadinessError("Debes confirmar explícitamente 'Activar integración'.")
    service = IntegrationReadinessService(db, company_id=company_id, registry=registry)
    current = service.tenant_readiness()
    if current["blocking_issues"]:
        append_integration_audit(db, company_id=company_id, event_type="onboarding.activation_blocked", outcome="blocked", message="Activación bloqueada por readiness.", metadata={"blocking_issues": current["blocking_issues"]})
        raise IntegrationReadinessError("La integración no está lista para activarse.", blockers=current["blocking_issues"])
    route = db.scalar(select(OrderRoute).where(OrderRoute.company_id == company_id, OrderRoute.order_type == "order"))
    ids = {item for item in (route.source_connection_id, route.destination_connection_id) if route and item is not None}
    try:
        with db.begin_nested():
            for connection_id in sorted(ids):
                activate_connection(db, company_id=company_id, connection_id=connection_id, registry=registry)
            if route is not None:
                route.is_active = True
                route.updated_at = _now()
            append_integration_audit(db, company_id=company_id, event_type="onboarding.activated", outcome="active", message="Tenant integrations activadas atómicamente.", metadata={"connection_ids": sorted(ids)})
    except Exception:
        raise
    return IntegrationReadinessService(db, company_id=company_id, registry=registry).tenant_readiness()


def pause_onboarding(db: Session, *, company_id: int, registry: IntegrationRegistry | None = None) -> dict[str, Any]:
    route = db.scalar(select(OrderRoute).where(OrderRoute.company_id == company_id, OrderRoute.order_type == "order"))
    ids = {item for item in (route.source_connection_id, route.destination_connection_id) if route and item is not None}
    if route is not None:
        route.is_active = False
        route.updated_at = _now()
    for connection_id in sorted(ids):
        disable_connection(db, company_id=company_id, connection_id=connection_id)
    append_integration_audit(db, company_id=company_id, event_type="onboarding.paused", outcome="paused", message="Tenant integrations pausadas; configuración y mappings conservados.", metadata={"connection_ids": sorted(ids)})
    return readiness(db, company_id=company_id, registry=registry)


__all__ = [
    "activate_onboarding",
    "execute_onboarding_dry_run",
    "pause_onboarding",
    "readiness",
    "save_onboarding_mapping",
    "save_onboarding_role",
    "save_onboarding_route",
    "start_onboarding",
    "test_onboarding_connection",
]
