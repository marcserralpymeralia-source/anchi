"""Sage 200 Spain PoC connector.

The public Sage 200 Spain material reviewed in BLOQUE 4A does not expose a
complete, versioned entity contract.  This module therefore keeps the
transport behind a small interface and marks every mock payload as
``UNVERIFIED_SAGE_CONTRACT``.  It is deliberately safe by default: the PoC
does not contain an HTTP implementation and real HTTP is disabled in config.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

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
from app.db.models import ExternalIdentity, Order


SAGE200ES_EXTERNAL_SYSTEM = "sage_200_es"
UNVERIFIED_SAGE_CONTRACT = "UNVERIFIED_SAGE_CONTRACT"
SAGE_EXTERNAL_REFERENCE_FIELD = UNVERIFIED_SAGE_CONTRACT
SAGE200ES_SOURCE_DOCUMENTATION = "docs/research/sage-api-analysis.md"


@dataclass(frozen=True, slots=True)
class Sage200ESConfig:
    """Tenant-scoped configuration without a persisted credential model."""

    tenant_id: int
    site: str = ""
    base_url: str = ""
    subscription_key: str | None = field(default=None, repr=False)
    authorization: str | None = field(default=None, repr=False)
    nonce: str | None = field(default=None, repr=False)
    timeout_seconds: float = 10.0
    real_http_enabled: bool = False

    def validation_errors(self) -> list[str]:
        errors: list[str] = []
        if self.tenant_id <= 0:
            errors.append("Sage 200 ES necesita un tenant_id positivo.")
        if not self.site.strip():
            errors.append("Sage 200 ES necesita un site explícito.")
        if self.timeout_seconds <= 0:
            errors.append("Sage 200 ES necesita un timeout positivo.")
        if self.real_http_enabled:
            errors.append("HTTP real de Sage 200 ES está bloqueado durante el PoC.")
        return errors

    def redacted_summary(self) -> dict[str, Any]:
        """Return safe diagnostics; never expose credentials."""

        return {
            "tenant_id": self.tenant_id,
            "site": self.site,
            "base_url": self.base_url,
            "timeout_seconds": self.timeout_seconds,
            "real_http_enabled": self.real_http_enabled,
            "has_subscription_key": bool(self.subscription_key),
            "has_authorization": bool(self.authorization),
            "has_nonce": bool(self.nonce),
        }


@dataclass(frozen=True, slots=True)
class Sage200ESIdentity:
    """ExternalIdentity projection preserving internal ID and visible reference."""

    internal_id: str
    reference: str | None = None
    external_subtype: str | None = None


@dataclass(frozen=True, slots=True)
class Sage200ESPriceResolution:
    source_price: float | None
    authoritative_price: float | None
    currency: str | None
    source: str

    @property
    def difference(self) -> bool:
        return self.source_price != self.authoritative_price and self.authoritative_price is not None


@dataclass(frozen=True, slots=True)
class Sage200ESSalesOrderDTO:
    """Conceptual DTO for the mock; it is not a verified Sage schema."""

    site: str
    customer: Sage200ESIdentity
    lines: tuple[Mapping[str, Any], ...]
    external_reference: str
    notes: str | None
    source_documentation: str = SAGE200ES_SOURCE_DOCUMENTATION
    contract_status: str = UNVERIFIED_SAGE_CONTRACT

    def as_payload(self) -> dict[str, Any]:
        return {
            "contract_status": self.contract_status,
            "source_documentation": self.source_documentation,
            "site": self.site,
            "customer": {
                "internal_id": self.customer.internal_id,
                "reference": self.customer.reference,
            },
            "lines": [dict(line) for line in self.lines],
            "anchi_context": {
                "external_reference": self.external_reference,
                "external_reference_field": SAGE_EXTERNAL_REFERENCE_FIELD,
            },
            "notes": self.notes,
        }


class Sage200ESTransportError(RuntimeError):
    """Transport failure metadata; retry policy remains in OrderDelivery."""

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


class Sage200ESTransport(Protocol):
    """Small seam for a future verified HTTPSage200ESTransport."""

    def read_sites(self) -> Sequence[Mapping[str, Any]]: ...

    def read_rates(self, *, site: str) -> Sequence[Mapping[str, Any]]: ...

    def read_price_offers(self, *, site: str) -> Sequence[Mapping[str, Any]]: ...

    def read_customer(self, *, site: str, internal_id: str) -> Mapping[str, Any] | None: ...

    def read_product(self, *, site: str, internal_id: str) -> Mapping[str, Any] | None: ...

    def resolve_price(
        self,
        *,
        site: str,
        customer_internal_id: str,
        product_internal_id: str,
        source_price: float | None,
        currency: str | None,
    ) -> Sage200ESPriceResolution: ...

    def create_sales_order(
        self,
        *,
        site: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def find_sales_order_by_idempotency_key(self, *, site: str, idempotency_key: str) -> Mapping[str, Any] | None: ...


class Sage200ESClient:
    """Connector-facing client; it never retries and never performs HTTP here."""

    def __init__(self, config: Sage200ESConfig, transport: Sage200ESTransport) -> None:
        self.config = config
        self.transport = transport

    def _ensure_transport_enabled(self) -> None:
        if self.config.real_http_enabled:
            raise Sage200ESTransportError("HTTP real de Sage 200 ES está bloqueado durante el PoC.")

    def read_sites(self) -> Sequence[Mapping[str, Any]]:
        self._ensure_transport_enabled()
        return self.transport.read_sites()

    def read_rates(self) -> Sequence[Mapping[str, Any]]:
        self._ensure_transport_enabled()
        return self.transport.read_rates(site=self.config.site)

    def read_price_offers(self) -> Sequence[Mapping[str, Any]]:
        self._ensure_transport_enabled()
        return self.transport.read_price_offers(site=self.config.site)

    def read_customer(self, internal_id: str) -> Mapping[str, Any] | None:
        self._ensure_transport_enabled()
        return self.transport.read_customer(site=self.config.site, internal_id=internal_id)

    def read_product(self, internal_id: str) -> Mapping[str, Any] | None:
        self._ensure_transport_enabled()
        return self.transport.read_product(site=self.config.site, internal_id=internal_id)

    def resolve_price(
        self,
        *,
        customer_internal_id: str,
        product_internal_id: str,
        source_price: float | None,
        currency: str | None,
    ) -> Sage200ESPriceResolution:
        self._ensure_transport_enabled()
        return self.transport.resolve_price(
            site=self.config.site,
            customer_internal_id=customer_internal_id,
            product_internal_id=product_internal_id,
            source_price=source_price,
            currency=currency,
        )

    def create_sales_order(self, *, idempotency_key: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self._ensure_transport_enabled()
        return self.transport.create_sales_order(site=self.config.site, idempotency_key=idempotency_key, payload=payload)

    def find_sales_order_by_idempotency_key(self, *, idempotency_key: str) -> Mapping[str, Any] | None:
        self._ensure_transport_enabled()
        return self.transport.find_sales_order_by_idempotency_key(site=self.config.site, idempotency_key=idempotency_key)


class MockSage200ESTransport:
    """Deterministic local transport for the Sage 200 ES PoC only."""

    def __init__(
        self,
        *,
        sites: Sequence[Mapping[str, Any]] | None = None,
        customers: Sequence[Mapping[str, Any]] | None = None,
        products: Sequence[Mapping[str, Any]] | None = None,
        rates: Sequence[Mapping[str, Any]] | None = None,
        price_offers: Sequence[Mapping[str, Any]] | None = None,
        scenario: str = "success",
    ) -> None:
        self.sites = [dict(item) for item in (sites or [{"internal_id": "SITE-DEMO", "reference": "SITE-DEMO"}])]
        self.customers = {str(item["internal_id"]): dict(item) for item in (customers or [])}
        self.products = {str(item["internal_id"]): dict(item) for item in (products or [])}
        self.rates = [dict(item) for item in (rates or [])]
        self.price_offers = [dict(item) for item in (price_offers or [])]
        self.scenario = scenario
        self.created_orders: dict[tuple[str, str], dict[str, Any]] = {}
        self.last_order_payload: Mapping[str, Any] | None = None

    def set_scenario(self, scenario: str) -> None:
        self.scenario = scenario

    def read_sites(self) -> Sequence[Mapping[str, Any]]:
        return tuple(self.sites)

    def read_rates(self, *, site: str) -> Sequence[Mapping[str, Any]]:
        return tuple(self.rates)

    def read_price_offers(self, *, site: str) -> Sequence[Mapping[str, Any]]:
        return tuple(self.price_offers)

    def read_customer(self, *, site: str, internal_id: str) -> Mapping[str, Any] | None:
        return self.customers.get(internal_id)

    def read_product(self, *, site: str, internal_id: str) -> Mapping[str, Any] | None:
        return self.products.get(internal_id)

    def resolve_price(
        self,
        *,
        site: str,
        customer_internal_id: str,
        product_internal_id: str,
        source_price: float | None,
        currency: str | None,
    ) -> Sage200ESPriceResolution:
        selected = None
        for offer in self.price_offers:
            if str(offer.get("product_internal_id")) != product_internal_id:
                continue
            offer_customer = offer.get("customer_internal_id")
            if offer_customer not in {None, customer_internal_id}:
                continue
            selected = offer
            if offer_customer == customer_internal_id:
                break
        authoritative = selected.get("authoritative_price") if selected else None
        return Sage200ESPriceResolution(
            source_price=source_price,
            authoritative_price=float(authoritative) if authoritative is not None else None,
            currency=str(selected.get("currency") if selected else currency) or None,
            source=str(selected.get("source") if selected else "source_price") or "source_price",
        )

    def create_sales_order(
        self,
        *,
        site: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        existing = self.created_orders.get((site, idempotency_key))
        if existing:
            return existing
        if self.scenario == "timeout_before_create":
            raise Sage200ESTransportError("Timeout antes de crear el pedido Sage mock.", error_type="timeout")
        if self.scenario == "rate_limited":
            raise Sage200ESTransportError("Sage mock ha limitado la tasa.", status_code=429)
        if self.scenario == "unavailable":
            raise Sage200ESTransportError("Sage mock no está disponible.", status_code=503)
        if self.scenario == "validation_error":
            raise Sage200ESTransportError("Sage mock rechazó el pedido.", status_code=422)

        order_number = f"SAGE-ORDER-{len(self.created_orders) + 1:06d}"
        record = {
            "contract_status": UNVERIFIED_SAGE_CONTRACT,
            "source_documentation": SAGE200ES_SOURCE_DOCUMENTATION,
            "id": order_number,
            "operational_number": order_number,
            "site": site,
            "payload": dict(payload),
        }
        self.created_orders[(site, idempotency_key)] = record
        self.last_order_payload = payload
        if self.scenario == "timeout_after_create":
            raise Sage200ESTransportError(
                "Timeout después de crear el pedido Sage mock.",
                error_type="timeout",
                remote_commit_unknown=True,
            )
        return record

    def find_sales_order_by_idempotency_key(self, *, site: str, idempotency_key: str) -> Mapping[str, Any] | None:
        return self.created_orders.get((site, idempotency_key))


class Sage200ESSalesOrderMapper:
    """Pure ANCHI-to-conceptual-Sage mapping; no DB, transport or retries."""

    def map_order(
        self,
        order: Order,
        *,
        customer: Sage200ESIdentity,
        products: Mapping[int, Sage200ESIdentity],
        prices: Mapping[int, Sage200ESPriceResolution],
        external_reference: str,
        site: str,
    ) -> Sage200ESSalesOrderDTO:
        lines: list[Mapping[str, Any]] = []
        for line in order.lines or []:
            identity = products.get(line.id)
            if identity is None:
                raise ValueError(f"No hay identidad Sage para la línea {line.id}.")
            price = prices.get(line.id)
            lines.append(
                {
                    "product": {
                        "internal_id": identity.internal_id,
                        "reference": identity.reference,
                    },
                    "quantity": float(line.quantity),
                    "source_price": line.unit_price_received,
                    "authoritative_price": price.authoritative_price if price else None,
                    "currency": price.currency if price else line.currency_received,
                    "price_source": price.source if price else "source_price",
                }
            )
        return Sage200ESSalesOrderDTO(
            site=site,
            customer=customer,
            lines=tuple(lines),
            external_reference=external_reference,
            notes=order.notes or None,
        )


class Sage200ESConnector(DestinationConnector):
    """Tenant-scoped Sage 200 ES mock connector with real HTTP disabled."""

    identifier = "sage_200_es"
    # No public entity contract is sufficiently verified for runtime enablement.
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
        config: Sage200ESConfig,
        transport: Sage200ESTransport,
        mapper: Sage200ESSalesOrderMapper | None = None,
    ) -> None:
        self.config = config
        self.client = Sage200ESClient(config, transport)
        self.mapper = mapper or Sage200ESSalesOrderMapper()
        self._health = ConnectorHealth(ConnectorStatus.HEALTHY, "Sage 200 ES PoC con transporte mock.")

    @property
    def health(self) -> ConnectorHealth:
        configuration_errors = self.validate_configuration()
        if configuration_errors:
            return ConnectorHealth(ConnectorStatus.MISCONFIGURED, "; ".join(configuration_errors))
        return self._health

    def validate_configuration(self) -> list[str]:
        return self.config.validation_errors()

    def _ensure_order_tenant(self, order: Order) -> list[str]:
        if order.company_id != self.config.tenant_id:
            return ["La configuración Sage 200 ES pertenece a otro tenant."]
        return []

    @staticmethod
    def external_reference_for_order(order: Order) -> str:
        return f"ANCHI-{order.company_id}-{order.id}"

    def _identity_for_entity(
        self,
        db: Session,
        *,
        company_id: int,
        entity_type: str,
        entity_id: int,
    ) -> Sage200ESIdentity | None:
        identity = db.scalar(
            select(ExternalIdentity).where(
                ExternalIdentity.company_id == company_id,
                ExternalIdentity.entity_type == entity_type,
                ExternalIdentity.entity_id == entity_id,
                ExternalIdentity.external_system == SAGE200ES_EXTERNAL_SYSTEM,
            )
        )
        if identity is None:
            return None
        metadata: Mapping[str, Any] = {}
        if identity.metadata_json:
            try:
                parsed = json.loads(identity.metadata_json)
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, Mapping):
                metadata = parsed
        return Sage200ESIdentity(
            internal_id=identity.external_id,
            reference=str(metadata.get("reference") or "").strip() or None,
            external_subtype=identity.external_subtype,
        )

    def lookup_customer(self, external_id: str) -> Mapping[str, Any] | None:
        return self.client.read_customer(external_id)

    def lookup_product(self, external_id: str) -> Mapping[str, Any] | None:
        return self.client.read_product(external_id)

    def validate_order(self, db: Session, order: Order) -> list[str]:
        errors = self.validate_configuration() + self._ensure_order_tenant(order)
        customer_identity = None
        if not order.validated_customer_id:
            errors.append("Cliente no validado en ANCHI.")
        else:
            customer_identity = self._identity_for_entity(
                db,
                company_id=order.company_id,
                entity_type="customer",
                entity_id=order.validated_customer_id,
            )
            if customer_identity is None:
                errors.append("Falta ExternalIdentity de Sage para el cliente.")
            elif self.lookup_customer(customer_identity.internal_id) is None:
                errors.append("El cliente Sage no existe en el fixture/transport configurado.")
        if self.config.site and not any(
            str(site.get("internal_id") or site.get("reference") or "") == self.config.site
            for site in self.client.read_sites()
        ):
            errors.append("El site Sage no existe en el fixture/transport configurado.")
        if not order.lines:
            errors.append("El pedido no tiene líneas.")
        for line in order.lines or []:
            if not line.validated_product_id:
                errors.append(f"Producto no validado en ANCHI: {line.source_product_external_id or line.id}.")
                continue
            product_identity = self._identity_for_entity(
                db,
                company_id=order.company_id,
                entity_type="product",
                entity_id=line.validated_product_id,
            )
            if product_identity is None:
                errors.append(f"Falta ExternalIdentity de Sage para el producto {line.id}.")
            elif self.lookup_product(product_identity.internal_id) is None:
                errors.append(f"El producto Sage no existe en el fixture/transport: {line.id}.")
            if line.quantity is None or float(line.quantity) <= 0:
                errors.append(f"Cantidad inválida en línea {line.id}.")
        return list(dict.fromkeys(errors))

    def _build_dto(self, db: Session, order: Order) -> Sage200ESSalesOrderDTO:
        customer_identity = self._identity_for_entity(
            db,
            company_id=order.company_id,
            entity_type="customer",
            entity_id=order.validated_customer_id,
        ) if order.validated_customer_id else None
        if customer_identity is None:
            raise ValueError("Falta ExternalIdentity de Sage para el cliente.")
        products: dict[int, Sage200ESIdentity] = {}
        prices: dict[int, Sage200ESPriceResolution] = {}
        for line in order.lines or []:
            if not line.validated_product_id:
                raise ValueError(f"Falta producto validado en la línea {line.id}.")
            product_identity = self._identity_for_entity(
                db,
                company_id=order.company_id,
                entity_type="product",
                entity_id=line.validated_product_id,
            )
            if product_identity is None:
                raise ValueError(f"Falta ExternalIdentity de Sage para el producto {line.id}.")
            products[line.id] = product_identity
            prices[line.id] = self.client.resolve_price(
                customer_internal_id=customer_identity.internal_id,
                product_internal_id=product_identity.internal_id,
                source_price=line.unit_price_received,
                currency=line.currency_received,
            )
        return self.mapper.map_order(
            order,
            customer=customer_identity,
            products=products,
            prices=prices,
            external_reference=self.external_reference_for_order(order),
            site=self.config.site,
        )

    @staticmethod
    def _delivery_error(error: Sage200ESTransportError) -> ConnectorDeliveryError:
        if error.remote_commit_unknown:
            return ConnectorDeliveryError(
                str(error),
                error_type="timeout_after_remote_commit",
                retryable=True,
                remote_commit_unknown=True,
            )
        if error.error_type == "timeout":
            return ConnectorDeliveryError(str(error), error_type="timeout", retryable=True)
        if error.status_code == 429:
            return ConnectorDeliveryError(str(error), error_type="rate_limited", retryable=True)
        if error.status_code in {408, 500, 502, 503, 504, 524}:
            return ConnectorDeliveryError(str(error), error_type="sage_unavailable", retryable=True)
        if error.status_code in {401, 403}:
            return ConnectorDeliveryError(str(error), error_type="authentication_failed", retryable=False)
        return ConnectorDeliveryError(str(error), error_type="sage_validation_error", retryable=False)

    def find_order_by_idempotency_key(self, db: Session, *, idempotency_key: str) -> str | None:
        record = self.client.find_sales_order_by_idempotency_key(idempotency_key=idempotency_key)
        if not record:
            return None
        return str(record.get("id") or record.get("operational_number") or "").strip() or None

    def create_order(self, db: Session, order: Order, *, idempotency_key: str | None = None) -> DestinationOrderResult:
        validation_errors = self.validate_order(db, order)
        if validation_errors:
            raise ConnectorDeliveryError("; ".join(validation_errors), error_type="permanent_validation_error")
        key = idempotency_key or self.external_reference_for_order(order)
        try:
            payload = self._build_dto(db, order).as_payload()
            record = self.client.create_sales_order(idempotency_key=key, payload=payload)
        except Sage200ESTransportError as error:
            raise self._delivery_error(error) from error
        except (TimeoutError, ConnectionError, OSError) as error:
            raise ConnectorDeliveryError(str(error), error_type="connection_error", retryable=True) from error
        external_id = str(record.get("id") or record.get("operational_number") or "").strip()
        if not external_id:
            raise ConnectorDeliveryError("Sage mock no devolvió external order ID.", error_type="invalid_response")
        return DestinationOrderResult(self.identifier, external_id)


__all__ = [
    "MockSage200ESTransport",
    "Sage200ESClient",
    "Sage200ESConfig",
    "Sage200ESConnector",
    "Sage200ESIdentity",
    "Sage200ESPriceResolution",
    "Sage200ESSalesOrderDTO",
    "Sage200ESSalesOrderMapper",
    "Sage200ESTransportError",
    "SAGE200ES_EXTERNAL_SYSTEM",
    "SAGE_EXTERNAL_REFERENCE_FIELD",
    "UNVERIFIED_SAGE_CONTRACT",
]
