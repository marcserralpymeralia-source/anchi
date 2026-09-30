"""Channel-neutral connector contracts for the modular order hub."""

from app.connectors.contracts import (
    BaseConnector,
    ConnectorCapability,
    ConnectorHealth,
    ConnectorDeliveryError,
    ConnectorRegistry,
    ConnectorStatus,
    DestinationConnector,
    DestinationOrderResult,
    InboundConnector,
    InboundOrderEvent,
    InputMode,
    StructuredOrderItem,
)
from app.connectors.whatsapp_catalog import WhatsAppCatalogInboundConnector

__all__ = [
    "BaseConnector",
    "ConnectorCapability",
    "ConnectorHealth",
    "ConnectorDeliveryError",
    "ConnectorRegistry",
    "ConnectorStatus",
    "DestinationConnector",
    "DestinationOrderResult",
    "InboundConnector",
    "InboundOrderEvent",
    "InputMode",
    "StructuredOrderItem",
    "WhatsAppCatalogInboundConnector",
]
