"""Derived onboarding and go-live readiness for one tenant."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.registry import ConnectorDirection, IntegrationHealth, IntegrationRegistry, IntegrationState
from app.connectors.security import external_http_gate_status
from app.core.observability import decode_structured_message
from app.db.models import AuditLog, Company, Customer, ExternalIdentity, IntegrationConnection, OrderRoute, Product, TenantIntegrationRole
from app.integrations.audit import append_integration_audit
from app.integrations.service import (
    ROLE_CAPABILITIES,
    ROLE_LABELS,
    _legacy_credentials_configured,
    connection_config_errors,
    get_connection,
)


ONBOARDING_STATUSES = frozenset({"NOT_STARTED", "IN_PROGRESS", "READY_FOR_TEST", "READY_FOR_ACTIVATION", "ACTIVE", "BLOCKED"})
TEST_STATUSES = frozenset({"NOT_TESTED", "SUCCESS", "FAILED", "BLOCKED"})


class IntegrationReadinessError(ValueError):
    def __init__(self, message: str, *, blockers: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.blockers = blockers or [{"code": "readiness_blocked", "message": message}]


@dataclass(frozen=True, slots=True)
class MappingStatus:
    entity_type: str
    total: int
    mapped: int
    unresolved_ids: tuple[int, ...]

    @property
    def unresolved(self) -> int:
        return max(0, self.total - self.mapped)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "total": self.total,
            "mapped": self.mapped,
            "unresolved": self.unresolved,
            "unresolved_ids": list(self.unresolved_ids),
        }


def _environment() -> str:
    return os.getenv("APP_ENV", "development").strip().lower()


def _issue(code: str, message: str, *, connection_id: int | None = None, role_key: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"code": code, "message": message}
    if connection_id is not None:
        item["connection_id"] = connection_id
    if role_key is not None:
        item["role_key"] = role_key
    return item


class IntegrationReadinessService:
    """Calculate readiness from durable tenant configuration, never UI checkboxes."""

    def __init__(self, db: Session, *, company_id: int, registry: IntegrationRegistry | None = None) -> None:
        self.db = db
        self.company_id = int(company_id)
        self.registry = registry or IntegrationRegistry()
        self.environment = _environment()

    def _connections(self) -> list[IntegrationConnection]:
        return list(
            self.db.scalars(
                select(IntegrationConnection)
                .where(IntegrationConnection.company_id == self.company_id)
                .order_by(IntegrationConnection.name, IntegrationConnection.id)
            ).all()
        )

    def _route(self) -> OrderRoute | None:
        return self.db.scalar(select(OrderRoute).where(OrderRoute.company_id == self.company_id, OrderRoute.order_type == "order"))

    def _roles(self) -> list[TenantIntegrationRole]:
        return list(self.db.scalars(select(TenantIntegrationRole).where(TenantIntegrationRole.company_id == self.company_id)).all())

    def _contract_verified(self, connection: IntegrationConnection) -> bool:
        definition = self.registry.definition(connection.connector_key)
        return bool(definition.runtime_enabled and not definition.mock_only)

    def _mock_activation_allowed(self, connection: IntegrationConnection) -> bool:
        return connection.connector_key in {"generic_rest", "fake_erp"} and self.environment in {"development", "test"}

    def _credentials_ready(self, connection: IntegrationConnection) -> bool:
        return not connection.requires_credentials or bool(connection.credentials_configured or _legacy_credentials_configured(self.db, connection))

    def _mapping_system(self, connection: IntegrationConnection) -> str:
        if connection.connector_key == "generic_rest":
            return f"generic_rest:{connection.id}"
        if connection.connector_key == "odoo":
            return "odoo"
        if connection.connector_key == "sage_200_es":
            return "sage_200_es"
        return connection.connector_key

    def mapping_status(self, connection: IntegrationConnection) -> dict[str, Any]:
        system = self._mapping_system(connection)
        mappings: list[MappingStatus] = []
        for entity_type, model in (("customer", Customer), ("product", Product)):
            entity_ids = set(self.db.scalars(select(model.id).where(model.company_id == self.company_id)).all())
            mapped_ids = set(
                self.db.scalars(
                    select(ExternalIdentity.entity_id).where(
                        ExternalIdentity.company_id == self.company_id,
                        ExternalIdentity.entity_type == entity_type,
                        ExternalIdentity.external_system == system,
                        ExternalIdentity.entity_id.in_(entity_ids) if entity_ids else False,
                    )
                ).all()
            )
            mappings.append(MappingStatus(entity_type, len(entity_ids), len(entity_ids & mapped_ids), tuple(sorted(entity_ids - mapped_ids)[:100])))
        return {
            "connection_id": connection.id,
            "external_system": system,
            "items": [item.to_dict() for item in mappings],
            "total": sum(item.total for item in mappings),
            "mapped": sum(item.mapped for item in mappings),
            "unresolved": sum(item.unresolved for item in mappings),
        }

    def _audit_exists(self, *, action: str, connection_id: int) -> bool:
        # Some service callers deliberately use a session with autoflush disabled
        # (for example the background activation flow).  Readiness must still see
        # audit events appended earlier in the same unit of work.
        self.db.flush()
        logs = self.db.scalars(
            select(AuditLog).where(AuditLog.company_id == self.company_id, AuditLog.action == action).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(50)
        ).all()
        for log in logs:
            metadata = decode_structured_message(log.message).get("metadata") or {}
            if str(metadata.get("connection_id")) == str(connection_id):
                return True
        return False

    def connection_test_status(self, connection: IntegrationConnection) -> dict[str, Any]:
        if self._audit_exists(action="connection.tested", connection_id=connection.id):
            return {"status": "SUCCESS", "message": "Connection test completado."}
        if connection.health_checked_at and connection.health_status == IntegrationHealth.HEALTHY.value:
            return {"status": "SUCCESS", "message": connection.health_message or "Health check correcto."}
        if connection.health_checked_at and connection.health_status in {IntegrationHealth.UNAVAILABLE.value, IntegrationHealth.MISCONFIGURED.value}:
            return {"status": "FAILED", "message": connection.health_message or "Connection test fallido."}
        external = external_http_gate_status(connection, connector_runtime_enabled=bool(self.registry.definition(connection.connector_key).runtime_enabled))
        if connection.connector_key in {"sage_200_es", "odoo"}:
            return {"status": "BLOCKED", "message": "Connector contract verification required; el runtime real está bloqueado."}
        if external["reason"] == "global_gate":
            return {"status": "BLOCKED", "message": "External HTTP disabled by runtime policy."}
        return {"status": "NOT_TESTED", "message": "Connection test pendiente."}

    def _connection_summary(self, connection: IntegrationConnection) -> dict[str, Any]:
        definition = self.registry.definition(connection.connector_key)
        config_errors = connection_config_errors(connection, registry=self.registry)
        mapping = self.mapping_status(connection)
        return {
            "id": connection.id,
            "name": connection.name,
            "connector_key": connection.connector_key,
            "display_name": definition.display_name,
            "direction": definition.direction.value,
            "state": connection.state,
            "configuration_valid": not config_errors,
            "config_errors": config_errors,
            "credentials_ready": self._credentials_ready(connection),
            "credentials_status": "configured" if self._credentials_ready(connection) else "missing",
            "capabilities": sorted(capability.value for capability in self.registry.capabilities_for(connection, environment=self.environment)),
            "contract_verified": self._contract_verified(connection),
            "mock_activation_allowed": self._mock_activation_allowed(connection),
            "connector_runtime_enabled": bool(definition.runtime_enabled or self._mock_activation_allowed(connection)),
            "health": {"status": connection.health_status, "message": connection.health_message, "checked_at": connection.health_checked_at.isoformat() if connection.health_checked_at else None},
            "test": self.connection_test_status(connection),
            "mapping": mapping,
            "external_http": external_http_gate_status(connection, connector_runtime_enabled=bool(definition.runtime_enabled)),
        }

    def tenant_readiness(self) -> dict[str, Any]:
        connections = self._connections()
        route = self._route()
        roles = {role.role_key: role for role in self._roles()}
        blockers: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        source = get_connection(self.db, company_id=self.company_id, connection_id=route.source_connection_id) if route and route.source_connection_id else None
        destination = get_connection(self.db, company_id=self.company_id, connection_id=route.destination_connection_id) if route and route.destination_connection_id else None
        source_summary = self._connection_summary(source) if source else None
        destination_summary = self._connection_summary(destination) if destination else None
        paused_configuration = bool(route and not route.is_active)

        if self.db.get(Company, self.company_id) is None:
            blockers.append(_issue("tenant_missing", "El tenant no tiene configuración de empresa."))
        if source is None:
            blockers.append(_issue("source_missing", "Añade y selecciona una connection de entrada."))
        elif self.registry.definition(source.connector_key).direction not in {ConnectorDirection.INBOUND, ConnectorDirection.BIDIRECTIONAL}:
            blockers.append(_issue("source_not_inbound", "La connection seleccionada no es un connector inbound.", connection_id=source.id))
        if destination is None:
            blockers.append(_issue("destination_missing", "Añade y selecciona una connection de destino."))
        else:
            if self.registry.definition(destination.connector_key).direction not in {ConnectorDirection.OUTBOUND, ConnectorDirection.BIDIRECTIONAL}:
                blockers.append(_issue("destination_not_outbound", "La connection seleccionada no es un connector outbound.", connection_id=destination.id))
            if destination.connector_key in {"sage_200_es", "odoo"}:
                blockers.append(_issue("contract_unverified", "Connector contract verification required antes de activar en producción.", connection_id=destination.id))
            elif destination.connector_key == "generic_rest":
                warnings.append(_issue("mock_only", "Generic REST está disponible como mock; producción requiere una habilitación contractual explícita.", connection_id=destination.id))

        for label, connection in (("source", source), ("destination", destination)):
            if connection is None:
                continue
            summary = source_summary if label == "source" else destination_summary
            if summary and not summary["configuration_valid"]:
                blockers.extend(_issue("configuration_invalid", message, connection_id=connection.id) for message in summary["config_errors"])
            if summary and not summary["credentials_ready"]:
                blockers.append(_issue("credentials_missing", "Faltan credenciales configuradas mediante SecretProvider.", connection_id=connection.id))
            if connection.state == IntegrationState.DISABLED.value:
                if paused_configuration:
                    warnings.append(_issue("paused", "La connection está pausada; la configuración se conserva para reanudar.", connection_id=connection.id))
                else:
                    blockers.append(_issue("connection_disabled", "La connection está deshabilitada.", connection_id=connection.id))

        if route is None:
            blockers.append(_issue("route_missing", "Configura un routing source → destination."))
        elif destination is not None:
            required = ROLE_CAPABILITIES["order_destination"]
            capabilities = set(self.registry.capabilities_for(destination, environment=self.environment))
            if required not in capabilities:
                blockers.append(_issue("capability_missing", f"El destino no declara {required}.", connection_id=destination.id, role_key="order_destination"))
            role = roles.get("order_destination")
            if role is None or role.connection_id != destination.id:
                blockers.append(_issue("role_missing", "Selecciona el destino de pedidos en Master Data Roles.", connection_id=destination.id, role_key="order_destination"))
            if route.destination_connection_id != destination.id:
                blockers.append(_issue("route_destination_mismatch", "El routing no coincide con el destino seleccionado."))

        for role_key, role in roles.items():
            connection = get_connection(self.db, company_id=self.company_id, connection_id=role.connection_id)
            if connection is None:
                blockers.append(_issue("role_connection_missing", f"El rol {ROLE_LABELS.get(role_key, role_key)} apunta a una connection inexistente.", role_key=role_key))
                continue
            capabilities = {capability.value for capability in self.registry.capabilities_for(connection, environment=self.environment)}
            if ROLE_CAPABILITIES.get(role_key) not in capabilities:
                blockers.append(_issue("role_capability_missing", f"{connection.name} no declara {ROLE_CAPABILITIES.get(role_key)}.", connection_id=connection.id, role_key=role_key))

        if destination is not None and destination.connector_key == "generic_rest":
            mapping = destination_summary["mapping"] if destination_summary else self.mapping_status(destination)
            for item in mapping["items"]:
                if item["unresolved"]:
                    blockers.append(_issue("identity_unresolved", f"{item['unresolved']} {item['entity_type']} sin mapping en {destination.name}.", connection_id=destination.id))
        elif destination is not None:
            warnings.append(_issue("identity_check_not_required", "Este connector no requiere ExternalIdentity local para el mock actual.", connection_id=destination.id))

        dry_run_ready = bool(destination and self._audit_exists(action="dry_run.succeeded", connection_id=destination.id))
        if destination and not dry_run_ready:
            blockers.append(_issue("dry_run_required", "Ejecuta un dry-run correcto antes de activar.", connection_id=destination.id))
        test = destination_summary["test"] if destination_summary else {"status": "NOT_TESTED"}
        if destination and test["status"] not in {"SUCCESS"}:
            blockers.append(_issue("connection_test_required", test.get("message") or "Ejecuta Test connection antes de activar.", connection_id=destination.id))

        core_codes = {"source_missing", "destination_missing", "source_not_inbound", "destination_not_outbound", "configuration_invalid", "credentials_missing", "connection_disabled", "route_missing", "capability_missing", "role_missing", "route_destination_mismatch", "role_connection_missing", "role_capability_missing", "identity_unresolved", "contract_unverified", "tenant_missing"}
        has_core_blockers = any(item["code"] in core_codes for item in blockers)
        any_config = bool(connections)
        all_active = bool(source and destination and source.state == IntegrationState.ACTIVE.value and destination.state == IntegrationState.ACTIVE.value and route and route.is_active)
        if all_active and not blockers:
            status = "ACTIVE"
        elif not any_config:
            status = "NOT_STARTED"
        elif has_core_blockers:
            status = "BLOCKED"
        elif blockers:
            status = "READY_FOR_TEST"
        else:
            status = "READY_FOR_ACTIVATION"
        current_step = next(
            (step for step, code in (("source", "source_missing"), ("destination", "destination_missing"), ("master_data", "role_missing"), ("routing", "route_missing"), ("mappings", "identity_unresolved"), ("test", "connection_test_required"), ("activation", "dry_run_required")) if any(item["code"] == code for item in blockers)),
            "complete" if status == "ACTIVE" else "configuration",
        )
        return {
            "status": status,
            "current_step": current_step,
            "tenant_id": self.company_id,
            "tenant_configured": self.db.get(Company, self.company_id) is not None,
            "configuration_valid": not any(item["code"] in {"configuration_invalid", "source_missing", "destination_missing"} for item in blockers),
            "credentials_ready": not any(item["code"] == "credentials_missing" for item in blockers),
            "capabilities_valid": not any(item["code"] in {"capability_missing", "role_capability_missing"} for item in blockers),
            "route_valid": not any(item["code"] in {"route_missing", "route_destination_mismatch"} for item in blockers),
            "master_data_valid": not any(item["code"] in {"role_missing", "role_connection_missing", "role_capability_missing"} for item in blockers),
            "connector_runtime_enabled": bool(destination and (self.registry.definition(destination.connector_key).runtime_enabled or self._mock_activation_allowed(destination))),
            "contract_verified": bool(destination and self._contract_verified(destination)),
            "health": destination_summary["health"] if destination_summary else {"status": IntegrationHealth.UNKNOWN.value, "message": "Sin destino"},
            "dry_run": {"status": "SUCCESS" if dry_run_ready else "NOT_TESTED", "message": "Dry-run correcto." if dry_run_ready else "Dry-run pendiente."},
            "test": test,
            "source": source_summary,
            "destination": destination_summary,
            "route": {"source_connection_id": route.source_connection_id if route else None, "destination_connection_id": route.destination_connection_id if route else None, "is_active": bool(route and route.is_active)},
            "mappings": destination_summary["mapping"] if destination_summary else None,
            "blocking_issues": blockers,
            "warnings": warnings,
            "connections": [self._connection_summary(connection) for connection in connections],
            "roles": [{"role_key": key, "label": ROLE_LABELS.get(key, key), "connection_id": role.connection_id} for key, role in sorted(roles.items())],
            "available_roles": [{"key": key, "label": ROLE_LABELS[key], "capability": capability} for key, capability in ROLE_CAPABILITIES.items()],
        }


__all__ = ["IntegrationReadinessError", "IntegrationReadinessService", "MappingStatus", "ONBOARDING_STATUSES", "TEST_STATUSES"]
