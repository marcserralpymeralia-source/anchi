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
from app.connectors.registry import (
    ConnectionRuntimeRegistry,
    ConnectorConfigField,
    ConnectorDefinition,
    ConnectorDirection,
    ConnectorMaturity,
    IntegrationHealth,
    IntegrationRegistry,
    IntegrationState,
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
    "MockOdooJSON2Transport",
    "OdooClient",
    "OdooConnector",
    "OdooExternalIdentity",
    "OdooJSON2Config",
    "OdooJSON2Request",
    "OdooPriceResolution",
    "OdooSalesOrderDTO",
    "OdooSalesOrderMapper",
    "OdooTransportError",
    "ConnectionRuntimeRegistry",
    "ConnectorConfigField",
    "ConnectorDefinition",
    "ConnectorDirection",
    "ConnectorMaturity",
    "IntegrationHealth",
    "IntegrationRegistry",
    "IntegrationState",
]


def __getattr__(name: str):
    """Keep optional connector modules out of process startup imports."""

    if name == "WhatsAppCatalogInboundConnector":
        from app.connectors.whatsapp_catalog import WhatsAppCatalogInboundConnector

        return WhatsAppCatalogInboundConnector
    sage_names = {
        "MockSage200ESTransport",
        "Sage200ESClient",
        "Sage200ESConfig",
        "Sage200ESConnector",
        "Sage200ESIdentity",
        "Sage200ESPriceResolution",
        "Sage200ESSalesOrderDTO",
        "Sage200ESSalesOrderMapper",
        "Sage200ESTransportError",
    }
    if name in sage_names:
        from app.connectors import sage200es

        return getattr(sage200es, name)
    odoo_names = {
        "MockOdooJSON2Transport",
        "OdooClient",
        "OdooConnector",
        "OdooExternalIdentity",
        "OdooJSON2Config",
        "OdooJSON2Request",
        "OdooPriceResolution",
        "OdooSalesOrderDTO",
        "OdooSalesOrderMapper",
        "OdooTransportError",
    }
    if name in odoo_names:
        from app.connectors import odoo

        return getattr(odoo, name)
    raise AttributeError(name)
