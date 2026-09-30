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
from app.connectors.sage200es import (
    MockSage200ESTransport,
    Sage200ESClient,
    Sage200ESConfig,
    Sage200ESConnector,
    Sage200ESIdentity,
    Sage200ESPriceResolution,
    Sage200ESSalesOrderDTO,
    Sage200ESSalesOrderMapper,
    Sage200ESTransportError,
)

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
    "MockSage200ESTransport",
    "Sage200ESClient",
    "Sage200ESConfig",
    "Sage200ESConnector",
    "Sage200ESIdentity",
    "Sage200ESPriceResolution",
    "Sage200ESSalesOrderDTO",
    "Sage200ESSalesOrderMapper",
    "Sage200ESTransportError",
]
