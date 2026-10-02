"""Tenant-scoped integration configuration and capability validation."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.contracts import ConnectorStatus
from app.connectors.registry import (
    IntegrationHealth,
    IntegrationRegistry,
    IntegrationState,
)
from app.connectors.security import EnvironmentSecretProvider, SecretProviderError, external_http_gate_status
from app.core.pilot_configuration import pilot_profile_state
from app.db.models import IntegrationConnection, OrderRoute, TenantIntegrationRole
from app.integrations.audit import append_integration_audit


ROLE_CAPABILITIES = {
    "customers_source": "customers.read",
    "products_source": "products.read",
    "pricing_source": "pricing.read",
    "inventory_source": "inventory.read",
    "order_destination": "orders.create",
}
ROLE_LABELS = {
    "customers_source": "Clientes",
    "products_source": "Productos",
    "pricing_source": "Precios",
    "inventory_source": "Stock",
    "order_destination": "Pedidos",
}
SECRET_KEY_PARTS = (
    "secret",
    "password",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "subscription_key",
    "private_key",
    "credential",
)


class IntegrationConfigurationError(ValueError):
    def __init__(self, message: str, *, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or [message]


def _environment() -> str:
    return os.getenv("APP_ENV", "development").strip().lower()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_config(raw: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise IntegrationConfigurationError("La configuración de integración no es JSON válido.") from exc
    if not isinstance(parsed, dict):
        raise IntegrationConfigurationError("La configuración de integración debe ser un objeto JSON.")
    return parsed


def _secret_keys(value: Any, path: str = "") -> list[str]:
    if isinstance(value, Mapping):
        found: list[str] = []
        for key, child in value.items():
            key_text = str(key).strip().lower()
            current_path = f"{path}.{key_text}" if path else key_text
            if any(part in key_text for part in SECRET_KEY_PARTS):
                found.append(current_path)
            else:
                found.extend(_secret_keys(child, current_path))
        return found
    if isinstance(value, (list, tuple)):
        found = []
        for index, child in enumerate(value):
            found.extend(_secret_keys(child, f"{path}[{index}]"))
        return found
    return []


def _normalize_field_value(field, value: Any) -> Any:  # noqa: ANN001
    if value is None or value == "":
        return value
    if field.value_type == "json":
        if isinstance(value, (dict, list)):
            return value
        try:
            parsed = json.loads(str(value))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field.label} debe ser JSON válido.") from exc
        if not isinstance(parsed, (dict, list)):
            raise ValueError(f"{field.label} debe ser un objeto o lista JSON.")
        return parsed
    if field.value_type == "number":
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field.label} debe ser numérico.") from exc
    if field.value_type == "boolean":
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
        raise ValueError(f"{field.label} debe ser booleano.")
    if field.value_type == "integer":
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field.label} debe ser entero.") from exc
    return str(value).strip()


def normalize_and_validate_config(definition, config: Mapping[str, Any] | None) -> tuple[dict[str, Any], list[str]]:  # noqa: ANN001
    raw = dict(config or {})
    errors: list[str] = []
    secret_keys = _secret_keys(raw)
    if secret_keys:
        errors.append("No se permiten secretos en config_json: " + ", ".join(secret_keys))
    normalized: dict[str, Any] = {}
    fields = {field.key: field for field in definition.config_schema}
    for key, value in raw.items():
        field = fields.get(key)
        if field is None:
            normalized[key] = value
            continue
        try:
            normalized[key] = _normalize_field_value(field, value)
        except ValueError as exc:
            errors.append(str(exc))
    for field in definition.config_schema:
        value = normalized.get(field.key, field.default)
        if field.required and (value is None or str(value).strip() == ""):
            errors.append(f"Falta configuración obligatoria: {field.key}.")
        if value is not None:
            normalized[field.key] = value
    if definition.mock_only and definition.key != "generic_rest" and normalized.get("real_http_enabled") is True:
        errors.append(f"HTTP real de {definition.display_name} está bloqueado durante el PoC.")
    if definition.key == "generic_rest":
        from app.connectors.generic_rest_mapping import validate_generic_rest_config

        errors.extend(validate_generic_rest_config(normalized))
    return normalized, list(dict.fromkeys(errors))


def get_connection(db: Session, *, company_id: int, connection_id: int) -> IntegrationConnection | None:
    return db.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.id == connection_id,
            IntegrationConnection.company_id == company_id,
        )
    )


def list_connections(db: Session, *, company_id: int) -> list[IntegrationConnection]:
    return db.scalars(
        select(IntegrationConnection)
        .where(IntegrationConnection.company_id == company_id)
        .order_by(IntegrationConnection.name, IntegrationConnection.id)
    ).all()


def connection_config_errors(connection: IntegrationConnection, *, registry: IntegrationRegistry | None = None) -> list[str]:
    registry = registry or IntegrationRegistry()
    definition = registry.definition(connection.connector_key)
    _, errors = normalize_and_validate_config(definition, _json_config(connection.config_json))
    config = _json_config(connection.config_json)
    generic_real = connection.connector_key == "generic_rest" and bool(config.get("real_http_enabled"))
    secret_ready = (
        _legacy_credentials_configured_for_ref(connection, config)
        if generic_real
        else bool(connection.credentials_configured)
    )
    if connection.requires_credentials and not secret_ready:
        is_mock = definition.mock_only and _environment() in {"development", "test"}
        if not is_mock:
            errors.append("Faltan credenciales configuradas fuera de config_json.")
    if definition.mock_only and _environment() not in {"development", "test"} and not generic_real:
        errors.append("El connector mock-only solo puede activarse en development/test.")
    if generic_real:
        truthy = {"1", "true", "yes", "on"}
        if os.getenv("PILOT_GENERIC_REST_RUNTIME_ENABLED", "").strip().lower() not in truthy:
            errors.append("PILOT_GENERIC_REST_RUNTIME_ENABLED debe habilitarse para Generic REST real.")
        if os.getenv("EXTERNAL_CONNECTOR_HTTP_ENABLED", "").strip().lower() not in truthy:
            errors.append("EXTERNAL_CONNECTOR_HTTP_ENABLED debe habilitarse para Generic REST real.")
        if pilot_profile_state() != "catalog":
            errors.append("Generic REST real sólo puede ejecutarse en el perfil CATALOG_ONLY_PILOT.")
    return list(dict.fromkeys(errors))


def _legacy_credentials_configured_for_ref(connection: IntegrationConnection, config: Mapping[str, Any]) -> bool:
    """Check a referenced runtime secret without ever copying it into DB/config."""
    if not connection.secret_provider_ref:
        return False
    auth = config.get("auth") if isinstance(config.get("auth"), Mapping) else {}
    credential_name = str(auth.get("credential_name") or "api_key")
    try:
        EnvironmentSecretProvider().resolve(
            tenant_id=int(connection.company_id),
            connection_id=int(connection.id),
            credential_ref=str(connection.secret_provider_ref),
            credential_name=credential_name,
        )
    except SecretProviderError:
        return False
    return True


def _legacy_credentials_configured(db: Session, connection: IntegrationConnection) -> bool:
    if connection.credentials_configured:
        return True
    if connection.connector_key == "whatsapp_catalog":
        from app.whatsapp.service import whatsapp_config

        config = whatsapp_config(db, connection.company_id)
        return bool(config.access_token and config.phone_number_id)
    if connection.connector_key == "email":
        from app.db.models import EmailSettings

        settings = db.scalar(select(EmailSettings).where(EmailSettings.company_id == connection.company_id))
        return bool(settings and (settings.access_token_encrypted or settings.imap_password_encrypted))
    return False


def create_connection(
    db: Session,
    *,
    company_id: int,
    connector_key: str,
    name: str,
    config: Mapping[str, Any] | None = None,
    secret_provider_ref: str | None = None,
    credentials_configured: bool = False,
    registry: IntegrationRegistry | None = None,
) -> IntegrationConnection:
    registry = registry or IntegrationRegistry()
    definition = registry.definition(connector_key)
    normalized, errors = normalize_and_validate_config(definition, config)
    if any(error.startswith("No se permiten secretos") for error in errors):
        raise IntegrationConfigurationError("Configuración inválida.", errors=errors)
    name = str(name or "").strip()
    if not name:
        raise IntegrationConfigurationError("La conexión necesita un nombre.")
    connection = IntegrationConnection(
        company_id=company_id,
        connector_key=connector_key,
        name=name[:200],
        state=IntegrationState.MISCONFIGURED.value if errors else IntegrationState.CONFIGURED.value,
        config_json=json.dumps(normalized, ensure_ascii=False, sort_keys=True),
        secret_provider_ref=str(secret_provider_ref or "").strip() or None,
        requires_credentials=definition.requires_credentials,
        credentials_configured=bool(credentials_configured),
        health_status=IntegrationHealth.UNKNOWN.value,
    )
    db.add(connection)
    db.flush()
    append_integration_audit(
        db,
        company_id=company_id,
        connection_id=connection.id,
        connector_key=connector_key,
        event_type="connection.created",
        outcome="configured" if not errors else "misconfigured",
        message="Conexión de integración creada.",
    )
    return connection


def update_connection(
    db: Session,
    *,
    company_id: int,
    connection_id: int,
    name: str | None = None,
    config: Mapping[str, Any] | None = None,
    secret_provider_ref: str | None = None,
    registry: IntegrationRegistry | None = None,
) -> IntegrationConnection:
    connection = get_connection(db, company_id=company_id, connection_id=connection_id)
    if connection is None:
        raise KeyError(f"Conexión de integración no encontrada: {connection_id}")
    registry = registry or IntegrationRegistry()
    definition = registry.definition(connection.connector_key)
    current = _json_config(connection.config_json)
    if config is not None:
        current.update(dict(config))
    normalized, errors = normalize_and_validate_config(definition, current)
    if any(error.startswith("No se permiten secretos") for error in errors):
        connection.state = IntegrationState.MISCONFIGURED.value
        raise IntegrationConfigurationError("Configuración inválida.", errors=errors)
    if name is not None:
        connection.name = str(name).strip()[:200] or connection.name
    connection.config_json = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    if secret_provider_ref is not None:
        connection.secret_provider_ref = str(secret_provider_ref).strip() or None
    connection.state = IntegrationState.MISCONFIGURED.value if errors else IntegrationState.CONFIGURED.value
    connection.updated_at = _now()
    append_integration_audit(
        db,
        company_id=company_id,
        connection_id=connection.id,
        connector_key=connection.connector_key,
        event_type="connection.config_changed",
        outcome="updated" if not errors else "misconfigured",
        message="Configuración de conexión actualizada.",
        metadata={"config_errors": errors},
    )
    return connection


def activate_connection(db: Session, *, company_id: int, connection_id: int, registry: IntegrationRegistry | None = None) -> IntegrationConnection:
    connection = get_connection(db, company_id=company_id, connection_id=connection_id)
    if connection is None:
        raise KeyError(f"Conexión de integración no encontrada: {connection_id}")
    registry = registry or IntegrationRegistry()
    definition = registry.definition(connection.connector_key)
    config = _json_config(connection.config_json)
    if not connection.credentials_configured and connection.connector_key == "generic_rest" and bool(config.get("real_http_enabled")) and _legacy_credentials_configured_for_ref(connection, config):
        connection.credentials_configured = True
    if not connection.credentials_configured and _legacy_credentials_configured(db, connection):
        connection.credentials_configured = True
    errors = connection_config_errors(connection, registry=registry)
    config = _json_config(connection.config_json)
    if definition.mock_only and definition.key != "generic_rest" and bool(config.get("real_http_enabled")):
        errors.append(f"{definition.display_name} no puede activar HTTP real.")
    if errors:
        connection.state = IntegrationState.MISCONFIGURED.value
        connection.updated_at = _now()
        raise IntegrationConfigurationError("La conexión no puede activarse.", errors=list(dict.fromkeys(errors)))
    connection.state = IntegrationState.ACTIVE.value
    connection.health_status = IntegrationHealth.UNKNOWN.value
    connection.health_message = "Pendiente de health check on-demand."
    connection.updated_at = _now()
    append_integration_audit(
        db,
        company_id=company_id,
        connection_id=connection.id,
        connector_key=connection.connector_key,
        event_type="connection.enabled",
        outcome="active",
        message="Conexión de integración habilitada.",
    )
    return connection


def disable_connection(db: Session, *, company_id: int, connection_id: int) -> IntegrationConnection:
    connection = get_connection(db, company_id=company_id, connection_id=connection_id)
    if connection is None:
        raise KeyError(f"Conexión de integración no encontrada: {connection_id}")
    connection.state = IntegrationState.DISABLED.value
    connection.health_status = IntegrationHealth.UNKNOWN.value
    connection.health_message = "Conexión deshabilitada; no se ejecuta health externo."
    connection.updated_at = _now()
    append_integration_audit(
        db,
        company_id=company_id,
        connection_id=connection.id,
        connector_key=connection.connector_key,
        event_type="connection.disabled",
        outcome="disabled",
        message="Conexión de integración deshabilitada.",
    )
    return connection


def _connection_capabilities(connection: IntegrationConnection, registry: IntegrationRegistry) -> set[str]:
    return {capability.value for capability in registry.capabilities_for(connection, environment=_environment())}


def set_integration_role(
    db: Session,
    *,
    company_id: int,
    role_key: str,
    connection_id: int,
    allow_inactive: bool = False,
    registry: IntegrationRegistry | None = None,
) -> TenantIntegrationRole:
    if role_key not in ROLE_CAPABILITIES:
        raise IntegrationConfigurationError(f"Rol de integración no soportado: {role_key}.")
    registry = registry or IntegrationRegistry()
    connection = get_connection(db, company_id=company_id, connection_id=connection_id)
    if connection is None:
        raise KeyError(f"Conexión de integración no encontrada: {connection_id}")
    if connection.state in {IntegrationState.DISABLED.value, IntegrationState.MISCONFIGURED.value}:
        raise IntegrationConfigurationError("No se puede seleccionar una conexión deshabilitada o inválida.")
    if connection.state != IntegrationState.ACTIVE.value and not allow_inactive:
        raise IntegrationConfigurationError("La conexión debe estar ACTIVE para este rol.")
    required = ROLE_CAPABILITIES[role_key]
    if required not in _connection_capabilities(connection, registry):
        raise IntegrationConfigurationError(
            f"{connection.name} no declara la capability requerida: {required}.",
            errors=[f"missing_capability:{required}"],
        )
    role = db.scalar(
        select(TenantIntegrationRole).where(
            TenantIntegrationRole.company_id == company_id,
            TenantIntegrationRole.role_key == role_key,
        )
    )
    if role is None:
        role = TenantIntegrationRole(company_id=company_id, role_key=role_key, connection_id=connection_id)
        db.add(role)
    else:
        role.connection_id = connection_id
        role.updated_at = _now()
    db.flush()
    append_integration_audit(
        db,
        company_id=company_id,
        connection_id=connection_id,
        connector_key=connection.connector_key,
        event_type="mapping.role_changed",
        outcome="configured",
        message="Rol de master data actualizado.",
        metadata={"role_key": role_key},
    )
    return role


def list_integration_roles(db: Session, *, company_id: int) -> list[TenantIntegrationRole]:
    return db.scalars(
        select(TenantIntegrationRole)
        .where(TenantIntegrationRole.company_id == company_id)
        .order_by(TenantIntegrationRole.role_key)
    ).all()


def set_order_route(
    db: Session,
    *,
    company_id: int,
    destination_connection_id: int,
    source_connection_id: int | None = None,
    order_type: str = "order",
    allow_inactive: bool = False,
    registry: IntegrationRegistry | None = None,
) -> OrderRoute:
    registry = registry or IntegrationRegistry()
    destination = get_connection(db, company_id=company_id, connection_id=destination_connection_id)
    if destination is None:
        raise KeyError(f"Conexión de integración no encontrada: {destination_connection_id}")
    if destination.state in {IntegrationState.DISABLED.value, IntegrationState.MISCONFIGURED.value}:
        raise IntegrationConfigurationError("El destino está deshabilitado o mal configurado.")
    if destination.state != IntegrationState.ACTIVE.value and not allow_inactive:
        raise IntegrationConfigurationError("El destino debe estar ACTIVE para entrar en routing.")
    if "orders.create" not in _connection_capabilities(destination, registry):
        raise IntegrationConfigurationError(
            "El destino no declara orders.create.",
            errors=["missing_capability:orders.create"],
        )
    source = None
    if source_connection_id is not None:
        source = get_connection(db, company_id=company_id, connection_id=source_connection_id)
        if source is None:
            raise KeyError(f"Conexión de entrada no encontrada: {source_connection_id}")
        if source.state in {IntegrationState.DISABLED.value, IntegrationState.MISCONFIGURED.value}:
            raise IntegrationConfigurationError("La fuente está deshabilitada o mal configurada.")
        if source.state != IntegrationState.ACTIVE.value and not allow_inactive:
            raise IntegrationConfigurationError("La fuente debe estar ACTIVE para entrar en routing.")
    route = db.scalar(
        select(OrderRoute).where(OrderRoute.company_id == company_id, OrderRoute.order_type == order_type)
    )
    if route is None:
        route = OrderRoute(company_id=company_id, order_type=order_type, destination_connector_key=destination.connector_key)
        db.add(route)
    route.destination_connector_key = destination.connector_key
    route.source_connection_id = source.id if source else None
    route.destination_connection_id = destination.id
    route.is_active = bool(destination.state == IntegrationState.ACTIVE.value and (source is None or source.state == IntegrationState.ACTIVE.value))
    route.updated_at = _now()
    db.flush()
    append_integration_audit(
        db,
        company_id=company_id,
        connection_id=destination.id,
        connector_key=destination.connector_key,
        event_type="route.changed",
        outcome="active" if route.is_active else "configured",
        message="Routing de pedidos actualizado.",
        metadata={"order_type": order_type, "source_connection_id": source.id if source else None},
    )
    return route


def health_check_connection(
    db: Session,
    *,
    company_id: int,
    connection_id: int,
    registry: IntegrationRegistry | None = None,
) -> IntegrationConnection:
    connection = get_connection(db, company_id=company_id, connection_id=connection_id)
    if connection is None:
        raise KeyError(f"Conexión de integración no encontrada: {connection_id}")
    connection.health_checked_at = _now()

    def record_health(outcome: str) -> None:
        append_integration_audit(
            db,
            company_id=company_id,
            connection_id=connection.id,
            connector_key=connection.connector_key,
            event_type="connection.health_checked",
            outcome=outcome,
            message=connection.health_message or "Health check de conexión completado.",
            metadata={"health_status": connection.health_status},
        )

    if connection.state == IntegrationState.DISABLED.value:
        connection.health_status = IntegrationHealth.UNKNOWN.value
        connection.health_message = "Conexión deshabilitada; no se ejecuta health externo."
        record_health("disabled")
        return connection
    registry = registry or IntegrationRegistry()
    errors = connection_config_errors(connection, registry=registry)
    if errors:
        connection.health_status = IntegrationHealth.MISCONFIGURED.value
        connection.health_message = "; ".join(errors)
        record_health("misconfigured")
        return connection
    definition = registry.definition(connection.connector_key)
    if not definition.health_check_supported or not definition.factory_path:
        connection.health_status = IntegrationHealth.UNKNOWN.value
        connection.health_message = "Health adapter pendiente; se mantiene la configuración legacy como source of truth."
        record_health("unsupported")
        return connection
    if connection.state != IntegrationState.ACTIVE.value:
        connection.health_status = IntegrationHealth.UNKNOWN.value
        connection.health_message = "La conexión no está ACTIVE."
        record_health("inactive")
        return connection
    try:
        connector = registry.resolve(db, company_id=company_id, connection_id=connection_id)
        status = connector.health.status
        mapped = {
            ConnectorStatus.HEALTHY: IntegrationHealth.HEALTHY,
            ConnectorStatus.READY: IntegrationHealth.HEALTHY,
            ConnectorStatus.DEGRADED: IntegrationHealth.DEGRADED,
            ConnectorStatus.UNAVAILABLE: IntegrationHealth.UNAVAILABLE,
            ConnectorStatus.MISCONFIGURED: IntegrationHealth.MISCONFIGURED,
            ConnectorStatus.NOT_CONFIGURED: IntegrationHealth.MISCONFIGURED,
        }.get(status, IntegrationHealth.UNKNOWN)
        connection.health_status = mapped.value
        connection.health_message = connector.health.message or mapped.value
    except Exception as exc:  # noqa: BLE001
        connection.health_status = IntegrationHealth.UNAVAILABLE.value
        connection.health_message = f"Health check fallido: {exc}"
    record_health(connection.health_status.lower())
    return connection


def connection_to_dict(connection: IntegrationConnection, *, registry: IntegrationRegistry | None = None) -> dict[str, Any]:
    registry = registry or IntegrationRegistry()
    definition = registry.definition(connection.connector_key)
    config = _json_config(connection.config_json)
    _, config_errors = normalize_and_validate_config(definition, config)
    generic_rest_runtime = (
        connection.connector_key == "generic_rest"
        and bool(config.get("real_http_enabled"))
        and os.getenv("PILOT_GENERIC_REST_RUNTIME_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    )
    external_http = external_http_gate_status(
        connection,
        connector_runtime_enabled=bool((definition.runtime_enabled and not definition.mock_only) or generic_rest_runtime),
    )
    return {
        "id": connection.id,
        "company_id": connection.company_id,
        "connector_key": connection.connector_key,
        "display_name": definition.display_name,
        "direction": definition.direction.value,
        "name": connection.name,
        "state": connection.state,
        "config": config,
        "secret_provider_ref": connection.secret_provider_ref,
        "requires_credentials": connection.requires_credentials,
        "credentials_configured": connection.credentials_configured,
        "credentials_status": "configured" if connection.credentials_configured and connection.secret_provider_ref else "missing",
        "external_http": external_http,
        "health": {
            "status": connection.health_status,
            "message": connection.health_message,
            "checked_at": connection.health_checked_at.isoformat() if connection.health_checked_at else None,
        },
        "config_errors": config_errors,
        "capabilities": sorted(capability.value for capability in registry.capabilities_for(connection)),
    }


__all__ = [
    "IntegrationConfigurationError",
    "ROLE_CAPABILITIES",
    "ROLE_LABELS",
    "activate_connection",
    "connection_config_errors",
    "connection_to_dict",
    "create_connection",
    "disable_connection",
    "get_connection",
    "health_check_connection",
    "list_connections",
    "list_integration_roles",
    "set_integration_role",
    "set_order_route",
    "update_connection",
]
