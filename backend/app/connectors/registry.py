"""Static connector definitions and lazy tenant connector resolution.

Definitions are metadata only.  Connector modules are imported by factory
functions at resolution time so listing the registry never instantiates or
loads an unused integration.
"""

from __future__ import annotations

import importlib
import json
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.contracts import BaseConnector, ConnectorCapability, ConnectorRegistry
from app.db.models import IntegrationConnection


class ConnectorDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    BIDIRECTIONAL = "bidirectional"


class ConnectorMaturity(str, Enum):
    STABLE = "stable"
    EXPERIMENTAL = "experimental"


class IntegrationState(str, Enum):
    DRAFT = "DRAFT"
    CONFIGURED = "CONFIGURED"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    MISCONFIGURED = "MISCONFIGURED"


class IntegrationHealth(str, Enum):
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    MISCONFIGURED = "MISCONFIGURED"


@dataclass(frozen=True, slots=True)
class ConnectorConfigField:
    key: str
    label: str
    value_type: str = "string"
    required: bool = False
    secret: bool = False
    default: Any = None


@dataclass(frozen=True, slots=True)
class ConnectorDefinition:
    key: str
    display_name: str
    direction: ConnectorDirection
    capabilities: frozenset[ConnectorCapability]
    config_schema: tuple[ConnectorConfigField, ...] = ()
    health_check_supported: bool = True
    requires_credentials: bool = False
    credentials_source: str | None = None
    maturity: ConnectorMaturity = ConnectorMaturity.STABLE
    visible_environments: frozenset[str] = frozenset({"development", "test", "demo", "production"})
    factory_path: str | None = None
    mock_capabilities: frozenset[ConnectorCapability] = frozenset()
    runtime_enabled: bool = True
    legacy_config_source: str | None = None

    def to_dict(self, *, environment: str) -> dict[str, Any]:
        return {
            "key": self.key,
            "display_name": self.display_name,
            "direction": self.direction.value,
            "capabilities": sorted(capability.value for capability in self.capabilities),
            "mock_capabilities": sorted(capability.value for capability in self.mock_capabilities),
            "config_schema": [
                {
                    "key": field.key,
                    "label": field.label,
                    "type": field.value_type,
                    "required": field.required,
                    "secret": field.secret,
                    "default": field.default,
                }
                for field in self.config_schema
            ],
            "health_check_supported": self.health_check_supported,
            "requires_credentials": self.requires_credentials,
            "credentials_source": self.credentials_source,
            "maturity": self.maturity.value,
            "runtime_enabled": self.runtime_enabled,
            "legacy_config_source": self.legacy_config_source,
            "visible_in_environment": environment in self.visible_environments,
        }


def build_fake_inbound_connector(config: Mapping[str, Any]) -> BaseConnector:
    from app.connectors.fake import FakeInboundConnector

    return FakeInboundConnector()


def build_fake_erp_connector(config: Mapping[str, Any]) -> BaseConnector:
    from app.connectors.fake import FakeERPConnector

    return FakeERPConnector(scenario=str(config.get("scenario") or "success"))


def build_whatsapp_catalog_connector(config: Mapping[str, Any]) -> BaseConnector:
    from app.connectors.whatsapp_catalog import WhatsAppCatalogInboundConnector

    return WhatsAppCatalogInboundConnector()


def _builtin_definitions() -> tuple[ConnectorDefinition, ...]:
    return (
        ConnectorDefinition(
            key="whatsapp_catalog",
            display_name="WhatsApp Catalog",
            direction=ConnectorDirection.INBOUND,
            capabilities=frozenset(),
            config_schema=(
                ConnectorConfigField("catalog_id", "Catálogo", required=True),
                ConnectorConfigField("phone_number_id", "Phone number ID"),
            ),
            requires_credentials=True,
            credentials_source="legacy_whatsapp_channel",
            legacy_config_source="whatsapp_channel_settings",
            factory_path="app.connectors.registry:build_whatsapp_catalog_connector",
        ),
        ConnectorDefinition(
            key="email",
            display_name="Email",
            direction=ConnectorDirection.INBOUND,
            capabilities=frozenset(),
            health_check_supported=False,
            requires_credentials=True,
            credentials_source="legacy_email_settings",
            legacy_config_source="email_settings",
        ),
        ConnectorDefinition(
            key="fake_inbound",
            display_name="Fake Inbound",
            direction=ConnectorDirection.INBOUND,
            capabilities=frozenset(),
            maturity=ConnectorMaturity.EXPERIMENTAL,
            visible_environments=frozenset({"development", "test"}),
            factory_path="app.connectors.registry:build_fake_inbound_connector",
        ),
        ConnectorDefinition(
            key="fake_erp",
            display_name="Fake ERP",
            direction=ConnectorDirection.OUTBOUND,
            capabilities=frozenset(
                {
                    ConnectorCapability.CUSTOMERS_READ,
                    ConnectorCapability.PRODUCTS_READ,
                    ConnectorCapability.ORDERS_CREATE,
                    ConnectorCapability.ORDERS_READ,
                }
            ),
            config_schema=(ConnectorConfigField("scenario", "Escenario", default="success"),),
            maturity=ConnectorMaturity.EXPERIMENTAL,
            visible_environments=frozenset({"development", "test"}),
            factory_path="app.connectors.registry:build_fake_erp_connector",
        ),
        ConnectorDefinition(
            key="sage_200_es",
            display_name="Sage 200 España",
            direction=ConnectorDirection.BIDIRECTIONAL,
            # The PoC deliberately exposes no verified runtime capability.
            capabilities=frozenset(),
            mock_capabilities=frozenset(
                {
                    ConnectorCapability.CUSTOMERS_READ,
                    ConnectorCapability.PRODUCTS_READ,
                    ConnectorCapability.PRICING_READ,
                    ConnectorCapability.ORDERS_CREATE,
                    ConnectorCapability.ORDERS_READ,
                }
            ),
            config_schema=(
                ConnectorConfigField("site", "Site Sage", required=True),
                ConnectorConfigField("real_http_enabled", "HTTP real", value_type="boolean", default=False),
                ConnectorConfigField("base_url", "Base URL"),
            ),
            requires_credentials=True,
            credentials_source="future_secret_provider",
            maturity=ConnectorMaturity.EXPERIMENTAL,
            factory_path="app.connectors.sage200es:build_sage200es_mock_connector",
            runtime_enabled=False,
        ),
    )


class IntegrationRegistry:
    """Registry of available connector types, not tenant instances."""

    def __init__(self, definitions: tuple[ConnectorDefinition, ...] | None = None) -> None:
        self._definitions = {definition.key: definition for definition in (definitions or _builtin_definitions())}
        self._factories: dict[str, Callable[[Mapping[str, Any]], BaseConnector]] = {}

    def definition(self, key: str) -> ConnectorDefinition:
        try:
            return self._definitions[key]
        except KeyError as exc:
            raise KeyError(f"Connector no registrado: {key}") from exc

    def list_definitions(self, *, environment: str | None = None, include_hidden: bool = False) -> tuple[ConnectorDefinition, ...]:
        environment = environment or os.getenv("APP_ENV", "development").strip().lower()
        definitions = self._definitions.values()
        if not include_hidden:
            definitions = (definition for definition in definitions if environment in definition.visible_environments)
        return tuple(sorted(definitions, key=lambda definition: definition.key))

    def register_factory(self, connector_key: str, factory: Callable[[Mapping[str, Any]], BaseConnector]) -> None:
        self.definition(connector_key)
        self._factories[connector_key] = factory

    def _factory(self, definition: ConnectorDefinition) -> Callable[[Mapping[str, Any]], BaseConnector]:
        factory = self._factories.get(definition.key)
        if factory is not None:
            return factory
        if not definition.factory_path:
            raise RuntimeError(f"El connector {definition.key} no tiene factory de runtime.")
        module_name, function_name = definition.factory_path.split(":", 1)
        module = importlib.import_module(module_name)
        factory = getattr(module, function_name)
        if not callable(factory):
            raise TypeError(f"La factory {definition.factory_path} no es invocable.")
        return factory

    def resolve(self, db: Session, *, company_id: int, connection_id: int) -> BaseConnector:
        connection = db.scalar(
            select(IntegrationConnection).where(
                IntegrationConnection.id == connection_id,
                IntegrationConnection.company_id == company_id,
            )
        )
        if connection is None:
            raise KeyError(f"Conexión de integración no encontrada: {connection_id}")
        if connection.state == IntegrationState.DISABLED.value:
            raise RuntimeError("La conexión de integración está deshabilitada.")
        if connection.state != IntegrationState.ACTIVE.value:
            raise RuntimeError("La conexión de integración no está activa.")
        definition = self.definition(connection.connector_key)
        if not definition.runtime_enabled and connection.connector_key != "sage_200_es":
            raise RuntimeError(f"El runtime del connector {definition.key} está bloqueado.")
        config = json.loads(connection.config_json or "{}")
        if not isinstance(config, dict):
            raise ValueError("La configuración de integración no es un objeto JSON.")
        config["tenant_id"] = company_id
        return self._factory(definition)(config)

    def capabilities_for(self, connection: IntegrationConnection, *, environment: str | None = None) -> frozenset[ConnectorCapability]:
        definition = self.definition(connection.connector_key)
        environment = environment or os.getenv("APP_ENV", "development").strip().lower()
        config = json.loads(connection.config_json or "{}")
        if (
            definition.mock_capabilities
            and bool(config.get("mock_mode", connection.connector_key == "sage_200_es"))
            and environment in {"development", "test"}
        ):
            return definition.mock_capabilities
        return definition.capabilities


class ConnectionRuntimeRegistry(ConnectorRegistry):
    """Compatibility registry that can distinguish duplicate connector types."""

    def __init__(self) -> None:
        super().__init__()
        self._connection_connectors: dict[int, BaseConnector] = {}

    def register_for_connection(self, connection_id: int, connector: BaseConnector) -> None:
        self._connection_connectors[connection_id] = connector
        if self.get(connector.identifier) is None:
            self.register(connector)

    def get_for_connection(self, connection_id: int, identifier: str | None = None) -> BaseConnector | None:
        connector = self._connection_connectors.get(connection_id)
        if connector is None:
            return None
        if identifier and connector.identifier != identifier:
            return None
        return connector


def build_runtime_registry(
    db: Session,
    *,
    company_id: int,
    connection_ids: set[int] | None = None,
    registry: IntegrationRegistry | None = None,
) -> ConnectionRuntimeRegistry:
    registry = registry or IntegrationRegistry()
    runtime = ConnectionRuntimeRegistry()
    query = select(IntegrationConnection).where(
        IntegrationConnection.company_id == company_id,
        IntegrationConnection.state == IntegrationState.ACTIVE.value,
    )
    if connection_ids:
        query = query.where(IntegrationConnection.id.in_(connection_ids))
    for connection in db.scalars(query).all():
        connector = registry.resolve(db, company_id=company_id, connection_id=connection.id)
        runtime.register_for_connection(connection.id, connector)
    return runtime


__all__ = [
    "ConnectionRuntimeRegistry",
    "ConnectorConfigField",
    "ConnectorDefinition",
    "ConnectorDirection",
    "ConnectorMaturity",
    "IntegrationHealth",
    "IntegrationRegistry",
    "IntegrationState",
    "build_runtime_registry",
]
