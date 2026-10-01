"""Odoo 19+ JSON-2 connector PoC.

Only the official JSON-2 transport boundary is modelled here.  The current
factory always uses ``MockOdooJSON2Transport``; no external HTTP is executed.
Odoo model fields and the sales-order command are marked provisional until a
tenant's dynamic ``/doc`` contract has been verified.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.contracts import (
    ConnectorCapability,
    ConnectorDeliveryError,
    ConnectorHealth,
    ConnectorStatus,
    DestinationConnector,
    DestinationOrderResult,
)
from app.connectors.odoo_fixtures import ODOO_MOCK_CUSTOMERS, ODOO_MOCK_PRICES, ODOO_MOCK_PRODUCTS
from app.connectors.security import SecureHTTPTransport, SecureHTTPTransportError
from app.core.observability import current_context
from app.db.models import ExternalIdentity, Order


ODOO_EXTERNAL_SYSTEM = "odoo"
ODOO_JSON2_VERSION = "19+"
UNVERIFIED_ODOO_CONTRACT = "UNVERIFIED_ODOO_CONTRACT"
ODOO_SOURCE_DOCUMENTATION = "https://www.odoo.com/documentation/19.0/developer/reference/external_api.html"
ODOO_MODEL_CUSTOMER = "res.partner"
ODOO_MODEL_PRODUCT_VARIANT = "product.product"
ODOO_MODEL_PRODUCT_TEMPLATE = "product.template"
ODOO_MODEL_SALES_ORDER = "sale.order"
ODOO_MODEL_SALES_ORDER_LINE = "sale.order.line"
ODOO_IDEMPOTENCY_FIELD = "client_order_ref"


@dataclass(frozen=True, slots=True)
class OdooJSON2Config:
    tenant_id: int
    base_url: str
    database: str = ""
    language: str = "es_ES"
    credential_ref: str | None = field(default=None, repr=False)
    timeout_seconds: float = 10.0
    real_http_enabled: bool = False
    mock_scenario: str = "success"

    def validation_errors(self) -> list[str]:
        errors: list[str] = []
        if self.tenant_id <= 0:
            errors.append("Odoo necesita un tenant_id positivo.")
        if not self.base_url.strip():
            errors.append("Odoo necesita base_url.")
        if self.timeout_seconds <= 0:
            errors.append("Odoo necesita un timeout positivo.")
        if self.real_http_enabled:
            errors.append("HTTP real de Odoo está bloqueado durante el PoC.")
        return errors

    def redacted_summary(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "base_url": self.base_url,
            "database": self.database,
            "language": self.language,
            "timeout_seconds": self.timeout_seconds,
            "real_http_enabled": self.real_http_enabled,
            "has_credential_ref": bool(self.credential_ref),
            "mock_scenario": self.mock_scenario,
        }


@dataclass(frozen=True, slots=True)
class OdooJSON2Request:
    model: str
    method: str
    body: Mapping[str, Any]
    headers: Mapping[str, str]

    @property
    def path(self) -> str:
        return f"/json/2/{self.model}/{self.method}"


class OdooTransportError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_type: str = "transport_error",
        remote_commit_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.remote_commit_unknown = remote_commit_unknown


class OdooTransport(Protocol):
    """Future HTTP adapter seam; it must not retry or own reliability."""

    def execute(self, request: OdooJSON2Request) -> Any: ...


class HTTPOdooJSON2Transport:
    """Future Odoo adapter; only an injected secure boundary may perform HTTP."""

    def __init__(self, secure_transport: SecureHTTPTransport | None = None) -> None:
        self.secure_transport = secure_transport

    def execute(self, request: OdooJSON2Request) -> Mapping[str, Any] | Sequence[Mapping[str, Any]]:
        if self.secure_transport is None:
            raise OdooTransportError("HTTP real de Odoo está bloqueado durante el PoC.", error_type="real_http_blocked")
        bridge = SimpleNamespace(
            operation=f"odoo.{request.model}.{request.method}",
            method="POST",
            url=f"{self.secure_transport.config.base_url.rstrip('/')}{request.path}",
            path=request.path,
            headers=dict(request.headers),
            query={},
            body=dict(request.body),
            request_id=uuid4().hex,
            correlation_id=current_context().get("correlation_id"),
        )
        try:
            response = self.secure_transport.execute(bridge)
        except SecureHTTPTransportError as exc:
            raise OdooTransportError(str(exc), status_code=exc.status_code, error_type=exc.category.value) from exc
        return response.body


class OdooClient:
    """JSON-2 request builder without authentication or retry side effects."""

    def __init__(self, config: OdooJSON2Config, transport: OdooTransport) -> None:
        self.config = config
        self.transport = transport

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "ANCHI-OdooConnector/0.1",
        }
        if self.config.database:
            headers["X-Odoo-Database"] = self.config.database
        # The credential is deliberately resolved outside this PoC and never
        # copied into a request fixture or persisted config_json.
        return headers

    def call(self, *, model: str, method: str, body: Mapping[str, Any]) -> Any:
        if self.config.real_http_enabled:
            raise OdooTransportError("HTTP real de Odoo está bloqueado durante el PoC.")
        return self.transport.execute(
            OdooJSON2Request(model=model, method=method, body=dict(body), headers=self._headers())
        )

    def read_customer(self, external_id: str) -> Mapping[str, Any] | None:
        records = self.call(
            model=ODOO_MODEL_CUSTOMER,
            method="read",
            body={"ids": [external_id], "context": {"lang": self.config.language}, "fields": ["id", "name"]},
        )
        return records[0] if isinstance(records, Sequence) and records else None

    def read_product_variant(self, external_id: str) -> Mapping[str, Any] | None:
        records = self.call(
            model=ODOO_MODEL_PRODUCT_VARIANT,
            method="read",
            body={
                "ids": [external_id],
                "context": {"lang": self.config.language},
                "fields": ["id", "name", "default_code", "product_tmpl_id"],
            },
        )
        return records[0] if isinstance(records, Sequence) and records else None

    def resolve_price(
        self,
        *,
        customer_external_id: str,
        product_external_id: str,
        source_price: float | None,
        currency: str | None,
    ) -> "OdooPriceResolution":
        result = self.call(
            model=UNVERIFIED_ODOO_CONTRACT,
            method="pricing.resolve",
            body={
                "customer_external_id": customer_external_id,
                "product_external_id": product_external_id,
                "source_price": source_price,
                "currency": currency,
            },
        )
        result = result if isinstance(result, Mapping) else {}
        authoritative = result.get("authoritative_price")
        return OdooPriceResolution(
            source_price=source_price,
            authoritative_price=float(authoritative) if authoritative is not None else None,
            currency=str(result.get("currency") or currency or "").strip() or None,
            source=str(result.get("source") or "source_price"),
        )

    def create_sales_order(self, *, idempotency_key: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        result = self.call(
            model=ODOO_MODEL_SALES_ORDER,
            method="create",
            body={"values": dict(payload), "context": {"lang": self.config.language}},
        )
        return result if isinstance(result, Mapping) else {}

    def find_sales_order_by_idempotency_key(self, *, idempotency_key: str) -> Mapping[str, Any] | None:
        result = self.call(
            model=ODOO_MODEL_SALES_ORDER,
            method="search_read",
            body={
                "domain": [[ODOO_IDEMPOTENCY_FIELD, "=", idempotency_key]],
                "fields": ["id", "name", ODOO_IDEMPOTENCY_FIELD],
                "limit": 1,
            },
        )
        return result[0] if isinstance(result, Sequence) and result else None


class MockOdooJSON2Transport:
    """Deterministic local JSON-2 fixture transport; never performs HTTP."""

    def __init__(
        self,
        *,
        customers: Sequence[Mapping[str, Any]] | None = None,
        products: Sequence[Mapping[str, Any]] | None = None,
        prices: Sequence[Mapping[str, Any]] | None = None,
        scenario: str = "success",
    ) -> None:
        self.customers = {str(item["id"]): dict(item) for item in (customers or ODOO_MOCK_CUSTOMERS)}
        self.products = {str(item["id"]): dict(item) for item in (products or ODOO_MOCK_PRODUCTS)}
        self.prices = [dict(item) for item in (prices or ODOO_MOCK_PRICES)]
        self.scenario = scenario
        self.created_orders: dict[str, dict[str, Any]] = {}
        self.requests: list[OdooJSON2Request] = []

    def set_scenario(self, scenario: str) -> None:
        self.scenario = scenario

    def _raise_global_scenario(self) -> None:
        if self.scenario == "permission_denied":
            raise OdooTransportError("Odoo mock denegó el permiso.", status_code=403, error_type="permission_denied")

    def execute(self, request: OdooJSON2Request) -> Any:
        self.requests.append(request)
        self._raise_global_scenario()
        if request.model == ODOO_MODEL_CUSTOMER and request.method == "read":
            if self.scenario == "customer_missing":
                return []
            return [self.customers[str(item)] for item in request.body.get("ids", []) if str(item) in self.customers]
        if request.model == ODOO_MODEL_PRODUCT_VARIANT and request.method == "read":
            if self.scenario == "product_missing":
                return []
            return [self.products[str(item)] for item in request.body.get("ids", []) if str(item) in self.products]
        if request.model == UNVERIFIED_ODOO_CONTRACT and request.method == "pricing.resolve":
            for price in self.prices:
                if (
                    str(price.get("customer_external_id")) == str(request.body.get("customer_external_id"))
                    and str(price.get("product_external_id")) == str(request.body.get("product_external_id"))
                ):
                    return price
            return {}
        if request.model == ODOO_MODEL_SALES_ORDER and request.method == "search_read":
            key = request.body.get("domain", [[None, None, None]])[0][2]
            return [record for record in self.created_orders.values() if record.get(ODOO_IDEMPOTENCY_FIELD) == key][:1]
        if request.model == ODOO_MODEL_SALES_ORDER and request.method == "create":
            return self._create_order(request.body.get("values") or {})
        raise OdooTransportError(
            f"Método Odoo mock no soportado: {request.model}.{request.method}",
            status_code=400,
            error_type="invalid_payload",
        )

    def _create_order(self, values: Mapping[str, Any]) -> Mapping[str, Any]:
        key = str(values.get(ODOO_IDEMPOTENCY_FIELD) or "").strip()
        if key in self.created_orders:
            return self.created_orders[key]
        if self.scenario == "timeout_before_create":
            raise OdooTransportError("Timeout Odoo antes de crear el pedido.", error_type="timeout")
        if self.scenario in {"temporary_failure", "rate_limited"}:
            status = 429 if self.scenario == "rate_limited" else 503
            error_type = "rate_limited" if status == 429 else "odoo_unavailable"
            raise OdooTransportError("Odoo mock no está disponible temporalmente.", status_code=status, error_type=error_type)
        if self.scenario == "invalid_payload":
            raise OdooTransportError("Odoo mock rechazó el payload de venta.", status_code=400, error_type="invalid_payload")
        order_id = f"ODOO-ORDER-{len(self.created_orders) + 1:06d}"
        record = {
            "id": order_id,
            "name": order_id,
            ODOO_IDEMPOTENCY_FIELD: key,
            "state": "draft",
            "model": ODOO_MODEL_SALES_ORDER,
            "contract_status": UNVERIFIED_ODOO_CONTRACT,
            "values": dict(values),
        }
        self.created_orders[key] = record
        if self.scenario == "timeout_after_create":
            raise OdooTransportError(
                "Timeout Odoo después de crear el pedido.",
                error_type="timeout",
                remote_commit_unknown=True,
            )
        return record


@dataclass(frozen=True, slots=True)
class OdooExternalIdentity:
    remote_id: str
    model: str
    display_ref: str | None = None
    variant_template_id: str | None = None


@dataclass(frozen=True, slots=True)
class OdooPriceResolution:
    source_price: float | None
    authoritative_price: float | None
    currency: str | None
    source: str

    @property
    def difference(self) -> bool:
        return self.authoritative_price is not None and self.source_price != self.authoritative_price


@dataclass(frozen=True, slots=True)
class OdooSalesOrderDTO:
    customer: OdooExternalIdentity
    lines: tuple[Mapping[str, Any], ...]
    external_reference: str
    notes: str | None
    state: str = "draft"
    contract_status: str = UNVERIFIED_ODOO_CONTRACT
    source_documentation: str = ODOO_SOURCE_DOCUMENTATION

    def as_payload(self) -> dict[str, Any]:
        return {
            "contract_status": self.contract_status,
            "source_documentation": self.source_documentation,
            "partner_id": self.customer.remote_id,
            "client_order_ref": self.external_reference,
            "order_line": [dict(line) for line in self.lines],
            "note": self.notes,
            "state_semantics": self.state,
        }


class OdooSalesOrderMapper:
    """Pure ANCHI-to-Odoo command mapping; no DB, HTTP or retry logic."""

    def map_order(
        self,
        order: Order,
        *,
        customer: OdooExternalIdentity,
        products: Mapping[int, OdooExternalIdentity],
        prices: Mapping[int, OdooPriceResolution],
        external_reference: str,
    ) -> OdooSalesOrderDTO:
        lines: list[Mapping[str, Any]] = []
        for line in order.lines or []:
            product = products.get(line.id)
            if product is None:
                raise ValueError(f"No hay identidad Odoo para la línea {line.id}.")
            price = prices.get(line.id)
            lines.append(
                {
                    "product_id": product.remote_id,
                    "product_model": product.model,
                    "product_template_id": product.variant_template_id,
                    "product_uom_qty": float(line.quantity),
                    "price_unit": price.authoritative_price if price and price.authoritative_price is not None else line.unit_price_received,
                    "source_price": line.unit_price_received,
                    "authoritative_price": price.authoritative_price if price else None,
                    "currency": price.currency if price else line.currency_received,
                    "price_source": price.source if price else "source_price",
                }
            )
        return OdooSalesOrderDTO(
            customer=customer,
            lines=tuple(lines),
            external_reference=external_reference,
            notes=order.notes or None,
        )


class OdooConnector(DestinationConnector):
    """Tenant-scoped Odoo 19+ JSON-2 mock connector."""

    identifier = "odoo"
    capabilities = frozenset()
    mock_capabilities = frozenset(
        {
            ConnectorCapability.CUSTOMERS_READ,
            ConnectorCapability.PRODUCTS_READ,
            ConnectorCapability.PRICING_READ,
            ConnectorCapability.ORDERS_CREATE,
            ConnectorCapability.ORDERS_READ,
        }
    )

    def __init__(
        self,
        *,
        config: OdooJSON2Config,
        transport: OdooTransport,
        mapper: OdooSalesOrderMapper | None = None,
    ) -> None:
        self.config = config
        self.client = OdooClient(config, transport)
        self.mapper = mapper or OdooSalesOrderMapper()
        self._health = ConnectorHealth(ConnectorStatus.HEALTHY, "Odoo JSON-2 PoC con transporte mock.")

    @property
    def health(self) -> ConnectorHealth:
        errors = self.validate_configuration()
        if errors:
            return ConnectorHealth(ConnectorStatus.MISCONFIGURED, "; ".join(errors))
        return self._health

    def validate_configuration(self) -> list[str]:
        return self.config.validation_errors()

    def _identity_for_entity(self, db: Session, *, company_id: int, entity_type: str, entity_id: int) -> OdooExternalIdentity | None:
        identity = db.scalar(
            select(ExternalIdentity).where(
                ExternalIdentity.company_id == company_id,
                ExternalIdentity.entity_type == entity_type,
                ExternalIdentity.entity_id == entity_id,
                ExternalIdentity.external_system == ODOO_EXTERNAL_SYSTEM,
            )
        )
        if identity is None:
            return None
        metadata: Mapping[str, Any] = {}
        try:
            parsed = json.loads(identity.metadata_json or "{}")
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, Mapping):
            metadata = parsed
        return OdooExternalIdentity(
            remote_id=identity.external_id,
            model=str(identity.external_subtype or metadata.get("model") or "").strip(),
            display_ref=str(metadata.get("display_ref") or metadata.get("default_code") or "").strip() or None,
            variant_template_id=str(metadata.get("product_tmpl_id") or "").strip() or None,
        )

    def _ensure_order_tenant(self, order: Order) -> list[str]:
        return [] if order.company_id == self.config.tenant_id else ["La configuración Odoo pertenece a otro tenant."]

    @staticmethod
    def external_reference_for_order(order: Order) -> str:
        return f"ANCHI-{order.company_id}-{order.id}"

    def validate_order(self, db: Session, order: Order) -> list[str]:
        errors = self.validate_configuration() + self._ensure_order_tenant(order)
        customer_identity = None
        if not order.validated_customer_id:
            errors.append("Cliente no validado en ANCHI.")
        else:
            customer_identity = self._identity_for_entity(
                db, company_id=order.company_id, entity_type="customer", entity_id=order.validated_customer_id
            )
            if customer_identity is None or customer_identity.model not in {"", ODOO_MODEL_CUSTOMER}:
                errors.append("Falta ExternalIdentity Odoo de res.partner para el cliente.")
        if not order.lines:
            errors.append("El pedido no tiene líneas.")
        for line in order.lines or []:
            if not line.validated_product_id:
                errors.append(f"Producto no validado en ANCHI: {line.source_product_external_id or line.id}.")
                continue
            product_identity = self._identity_for_entity(
                db, company_id=order.company_id, entity_type="product", entity_id=line.validated_product_id
            )
            if product_identity is None or product_identity.model != ODOO_MODEL_PRODUCT_VARIANT:
                errors.append(f"Falta ExternalIdentity Odoo de product.product para la línea {line.id}.")
            if line.quantity is None or float(line.quantity) <= 0:
                errors.append(f"Cantidad inválida en línea {line.id}.")
        return list(dict.fromkeys(errors))

    def _build_dto(self, db: Session, order: Order) -> OdooSalesOrderDTO:
        customer = self._identity_for_entity(
            db, company_id=order.company_id, entity_type="customer", entity_id=order.validated_customer_id
        ) if order.validated_customer_id else None
        if customer is None:
            raise ValueError("Falta ExternalIdentity Odoo del cliente.")
        if self.client.read_customer(customer.remote_id) is None:
            raise ConnectorDeliveryError(
                "El cliente Odoo no existe en el fixture/transport configurado.",
                error_type="permanent_validation_error",
            )
        products: dict[int, OdooExternalIdentity] = {}
        prices: dict[int, OdooPriceResolution] = {}
        for line in order.lines or []:
            product = self._identity_for_entity(
                db, company_id=order.company_id, entity_type="product", entity_id=line.validated_product_id
            ) if line.validated_product_id else None
            if product is None:
                raise ValueError(f"Falta ExternalIdentity Odoo de la línea {line.id}.")
            if self.client.read_product_variant(product.remote_id) is None:
                raise ConnectorDeliveryError(
                    f"La variante Odoo no existe en el fixture/transport: {line.id}.",
                    error_type="permanent_validation_error",
                )
            products[line.id] = product
            prices[line.id] = self.client.resolve_price(
                customer_external_id=customer.remote_id,
                product_external_id=product.remote_id,
                source_price=line.unit_price_received,
                currency=line.currency_received,
            )
        return self.mapper.map_order(
            order,
            customer=customer,
            products=products,
            prices=prices,
            external_reference=self.external_reference_for_order(order),
        )

    @staticmethod
    def _delivery_error(error: OdooTransportError) -> ConnectorDeliveryError:
        if error.remote_commit_unknown:
            return ConnectorDeliveryError(
                str(error), error_type="timeout_after_remote_commit", retryable=True, remote_commit_unknown=True
            )
        if error.error_type == "timeout":
            return ConnectorDeliveryError(str(error), error_type="timeout", retryable=True)
        if error.status_code == 429 or error.error_type == "rate_limited":
            return ConnectorDeliveryError(str(error), error_type="rate_limited", retryable=True)
        if error.status_code in {408, 500, 502, 503, 504, 524} or error.error_type == "odoo_unavailable":
            return ConnectorDeliveryError(str(error), error_type="odoo_unavailable", retryable=True)
        if error.status_code in {401, 403} or error.error_type == "permission_denied":
            return ConnectorDeliveryError(str(error), error_type="permission_denied", retryable=False)
        return ConnectorDeliveryError(str(error), error_type="permanent_validation_error", retryable=False)

    def find_order_by_idempotency_key(self, db: Session, *, idempotency_key: str) -> str | None:
        try:
            record = self.client.find_sales_order_by_idempotency_key(idempotency_key=idempotency_key)
        except OdooTransportError as error:
            raise self._delivery_error(error) from error
        return str(record.get("id") or record.get("name") or "").strip() if record else None

    def create_order(self, db: Session, order: Order, *, idempotency_key: str | None = None) -> DestinationOrderResult:
        errors = self.validate_order(db, order)
        if errors:
            raise ConnectorDeliveryError("; ".join(errors), error_type="permanent_validation_error")
        try:
            payload = self._build_dto(db, order).as_payload()
            record = self.client.create_sales_order(
                idempotency_key=idempotency_key or self.external_reference_for_order(order),
                payload=payload,
            )
        except OdooTransportError as error:
            raise self._delivery_error(error) from error
        external_id = str(record.get("id") or record.get("name") or "").strip()
        if not external_id:
            raise ConnectorDeliveryError("Odoo mock no devolvió external order ID.", error_type="invalid_response")
        return DestinationOrderResult(self.identifier, external_id)


def build_odoo_mock_connector(config: Mapping[str, Any]) -> OdooConnector:
    try:
        tenant_id = int(config.get("tenant_id") or 0)
    except (TypeError, ValueError):
        tenant_id = 0

    def fixture_values(key: str) -> Sequence[Mapping[str, Any]] | None:
        value = config.get(key)
        return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else None

    odoo_config = OdooJSON2Config(
        tenant_id=tenant_id,
        base_url=str(config.get("base_url") or "").strip(),
        database=str(config.get("database") or "").strip(),
        language=str(config.get("language") or "es_ES").strip(),
        credential_ref=str(config.get("secret_provider_ref") or "").strip() or None,
        timeout_seconds=float(config.get("timeout_seconds") or 10),
        real_http_enabled=bool(config.get("real_http_enabled", False)),
        mock_scenario=str(config.get("mock_scenario") or "success").strip(),
    )
    return OdooConnector(
        config=odoo_config,
        transport=MockOdooJSON2Transport(
            customers=fixture_values("mock_customers"),
            products=fixture_values("mock_products"),
            prices=fixture_values("mock_prices"),
            scenario=odoo_config.mock_scenario,
        ),
    )


__all__ = [
    "ODOO_EXTERNAL_SYSTEM",
    "ODOO_IDEMPOTENCY_FIELD",
    "ODOO_JSON2_VERSION",
    "ODOO_MODEL_CUSTOMER",
    "ODOO_MODEL_PRODUCT_TEMPLATE",
    "ODOO_MODEL_PRODUCT_VARIANT",
    "ODOO_MODEL_SALES_ORDER",
    "ODOO_MODEL_SALES_ORDER_LINE",
    "ODOO_SOURCE_DOCUMENTATION",
    "UNVERIFIED_ODOO_CONTRACT",
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
    "build_odoo_mock_connector",
]
