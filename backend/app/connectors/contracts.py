from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, TYPE_CHECKING

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.db.models import Order


class InputMode(str, Enum):
    STRUCTURED = "structured"
    UNSTRUCTURED = "unstructured"


class ConnectorCapability(str, Enum):
    CUSTOMERS_READ = "customers.read"
    PRODUCTS_READ = "products.read"
    PRICING_READ = "pricing.read"
    INVENTORY_READ = "inventory.read"
    ORDERS_CREATE = "orders.create"
    ORDERS_READ = "orders.read"
    CATALOG_READ = "catalog.read"
    CATALOG_WRITE = "catalog.write"


class ConnectorStatus(str, Enum):
    HEALTHY = "healthy"
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    MISCONFIGURED = "misconfigured"
    NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True, slots=True)
class ConnectorHealth:
    status: ConnectorStatus
    message: str = ""


@dataclass(frozen=True, slots=True)
class StructuredOrderItem:
    source_product_id: str
    quantity: float
    unit: str | None = None
    unit_price: float | None = None
    currency: str | None = None
    description: str | None = None
    source_product_system: str | None = None


@dataclass(frozen=True, slots=True)
class InboundOrderEvent:
    company_id: int
    connector_key: str
    provider: str
    source_mode: InputMode
    external_event_id: str
    external_system: str
    customer_external_system: str | None = None
    external_order_id: str | None = None
    customer_external_id: str | None = None
    customer_name: str | None = None
    items: tuple[StructuredOrderItem, ...] = ()
    notes: str | None = None
    source_catalog_id: str | None = None
    raw_payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DestinationOrderResult:
    connector_key: str
    external_order_id: str
    status: str = "created"
    idempotent_replay: bool = False


class ConnectorDeliveryError(RuntimeError):
    """Normalized connector failure used by the delivery retry policy."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str = "connector_error",
        retryable: bool = False,
        remote_commit_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable
        self.remote_commit_unknown = remote_commit_unknown


class BaseConnector(ABC):
    identifier: str
    capabilities: frozenset[ConnectorCapability] = frozenset()

    @property
    def health(self) -> ConnectorHealth:
        return ConnectorHealth(ConnectorStatus.HEALTHY)

    def validate_configuration(self) -> list[str]:
        return []


class InboundConnector(BaseConnector):
    @abstractmethod
    def to_inbound_order_event(self, payload: Mapping[str, Any], *, company_id: int) -> InboundOrderEvent:
        """Translate provider data into a neutral order event."""


class DestinationConnector(BaseConnector):
    @abstractmethod
    def validate_order(self, db: Session, order: Order) -> list[str]:
        """Return deterministic acceptance errors without side effects."""

    @abstractmethod
    def create_order(self, db: Session, order: Order, *, idempotency_key: str | None = None) -> DestinationOrderResult:
        """Create or idempotently replay the destination order."""

    def find_order_by_idempotency_key(self, db: Session, *, idempotency_key: str) -> str | None:
        """Optionally reconcile a remote order after an unknown local outcome."""

        return None


class ConnectorRegistry:
    def __init__(self, connectors: list[BaseConnector] | None = None) -> None:
        self._connectors: dict[str, BaseConnector] = {}
        for connector in connectors or []:
            self.register(connector)

    def register(self, connector: BaseConnector) -> None:
        identifier = str(connector.identifier).strip()
        if not identifier:
            raise ValueError("El connector necesita un identifier.")
        if identifier in self._connectors:
            raise ValueError(f"Connector duplicado: {identifier}")
        self._connectors[identifier] = connector

    def get(self, identifier: str) -> BaseConnector | None:
        return self._connectors.get(identifier)

    def require(self, identifier: str) -> BaseConnector:
        connector = self.get(identifier)
        if connector is None:
            raise KeyError(f"Connector no registrado: {identifier}")
        return connector

    def capabilities(self, identifier: str) -> frozenset[ConnectorCapability]:
        connector = self.require(identifier)
        return connector.capabilities

    def identifiers(self) -> tuple[str, ...]:
        return tuple(sorted(self._connectors))
