"""Safe, declarative Generic REST connector PoC.

The connector is deliberately local-only in BLOQUE 7.  ``HTTPGenericRESTTransport``
is a future seam that refuses execution; all tests and demos use the deterministic
``MockGenericRESTTransport``.  The mapping language lives in
``generic_rest_mapping`` and does not evaluate code or templates.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import quote, urlsplit, urlunsplit

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
from app.connectors.generic_rest_mapping import (
    GENERIC_REST_MAX_LINES,
    GENERIC_REST_MAX_REQUEST_BYTES,
    GENERIC_REST_MAX_RESPONSE_BYTES,
    GenericRESTConfig,
    GenericRESTConfigError,
    configured_capability_values,
    evaluate_mapping,
    extract_response_mapping,
    identity_namespace,
    validate_generic_rest_config,
)
from app.db.models import ExternalIdentity, Order


GENERIC_REST_IDENTIFIER = "generic_rest"
GENERIC_REST_MOCK_CONTRACT = "MOCK_GENERIC_REST_CONTRACT"


@dataclass(frozen=True, slots=True)
class GenericRESTResponse:
    status_code: int
    body: Mapping[str, Any] = field(default_factory=dict)
    body_bytes: int | None = None


class GenericRESTTransportError(RuntimeError):
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


@dataclass(frozen=True, slots=True)
class GenericRESTRequest:
    operation: str
    method: str
    url: str
    path: str
    headers: Mapping[str, str]
    query: Mapping[str, Any]
    body: Mapping[str, Any] | None
    credential_ref: str | None = None


class GenericRESTTransport(Protocol):
    def execute(self, request: GenericRESTRequest) -> GenericRESTResponse: ...


class HTTPGenericRESTTransport:
    """Reserved future transport; never opens a socket in BLOQUE 7."""

    def execute(self, request: GenericRESTRequest) -> GenericRESTResponse:
        raise GenericRESTTransportError(
            "HTTP real de Generic REST está bloqueado durante el PoC.",
            error_type="real_http_blocked",
        )


class GenericRESTClient:
    """Builds bounded requests from a validated operation without retrying."""

    def __init__(self, config: GenericRESTConfig, transport: GenericRESTTransport) -> None:
        self.config = config
        self.transport = transport

    def _operation(self, operation_name: str) -> Mapping[str, Any]:
        try:
            return self.config.operations[operation_name]
        except KeyError as exc:
            raise GenericRESTConfigError(f"Operation no configurada: {operation_name}.") from exc

    @staticmethod
    def _safe_path_value(value: Any, label: str) -> str:
        text = str(value).strip()
        if not text or "://" in text or text.startswith("//") or "\\" in text or ".." in text:
            raise GenericRESTConfigError(f"{label} contiene una URL o path inseguro.")
        return quote(text, safe="-._~:@")

    def build_request(
        self,
        operation_name: str,
        context: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> GenericRESTRequest:
        operation = self._operation(operation_name)
        path_params = evaluate_mapping(operation.get("path_params", {}), context, label=f"{operation_name}.path_params")
        path = str(operation.get("path") or "")
        for name, value in path_params.items():
            path = path.replace("{" + str(name) + "}", self._safe_path_value(value, f"{operation_name}.{name}"))
        query = evaluate_mapping(operation.get("query_mapping", {}), context, label=f"{operation_name}.query_mapping")
        body: dict[str, Any] | None = None
        if str(operation.get("method") or "").upper() != "GET" or operation.get("request_mapping"):
            body = evaluate_mapping(operation.get("request_mapping", {}), context, label=f"{operation_name}.request_mapping")
        headers = {str(key): str(value) for key, value in (operation.get("request_headers") or {}).items()}
        headers.setdefault("Accept", "application/json")
        if body is not None:
            headers.setdefault("Content-Type", "application/json")
        idempotency = operation.get("idempotency") or {}
        if idempotency_key and isinstance(idempotency, Mapping):
            if idempotency.get("location") == "header":
                headers[str(idempotency.get("name"))] = idempotency_key
            elif idempotency.get("location") == "body":
                if body is None:
                    body = {}
                self._set_body_value(body, str(idempotency.get("name")), idempotency_key)
        if body is not None:
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > self.config.max_request_bytes:
                raise GenericRESTConfigError("El request supera el máximo de bytes permitido.")
        parsed_base = urlsplit(self.config.base_url.rstrip("/"))
        base_path = parsed_base.path.rstrip("/")
        full_path = f"{base_path}{path}" or "/"
        url = urlunsplit((parsed_base.scheme, parsed_base.netloc, full_path, "", ""))
        return GenericRESTRequest(
            operation=operation_name,
            method=str(operation.get("method") or "GET").upper(),
            url=url,
            path=full_path,
            headers=headers,
            query=query,
            body=body,
            credential_ref=self.config.credential_ref,
        )

    @staticmethod
    def _set_body_value(body: dict[str, Any], name: str, value: Any) -> None:
        parts = [part for part in name.split(".") if part]
        if not parts:
            raise GenericRESTConfigError("idempotency.name no puede estar vacío.")
        current = body
        for part in parts[:-1]:
            child = current.setdefault(part, {})
            if not isinstance(child, dict):
                raise GenericRESTConfigError("idempotency.name entra en conflicto con el request mapping.")
            current = child
        current[parts[-1]] = value

    def execute(
        self,
        operation_name: str,
        context: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> GenericRESTResponse:
        request = self.build_request(operation_name, context, idempotency_key=idempotency_key)
        response = self.transport.execute(request)
        if not isinstance(response, GenericRESTResponse):
            raise GenericRESTTransportError("El transport no devolvió una respuesta JSON válida.", error_type="invalid_response")
        body_bytes = response.body_bytes
        if body_bytes is None:
            body_bytes = len(json.dumps(response.body, ensure_ascii=False).encode("utf-8"))
        if body_bytes > self.config.max_response_bytes:
            raise GenericRESTTransportError("La respuesta supera el máximo de bytes permitido.", error_type="response_too_large")
        operation = self._operation(operation_name)
        success = set(operation.get("success_status_codes", [200]))
        if response.status_code not in success:
            retryable = set(operation.get("retryable_status_codes", [429, 500, 502, 503, 504]))
            raise GenericRESTTransportError(
                f"Generic REST devolvió HTTP {response.status_code}.",
                status_code=response.status_code,
                error_type="retryable_http" if response.status_code in retryable else "permanent_http",
                remote_commit_unknown=response.status_code in {408, 500, 502, 503, 504},
            )
        return response


class MockGenericRESTTransport:
    """Local Acme/Beta ERP fixture; it never performs HTTP."""

    def __init__(self, *, scenario: str = "acme_a") -> None:
        scenario_text = str(scenario or "acme_a")
        self.fixture = "acme_b" if scenario_text.startswith("acme_b") else "acme_a"
        self.failure = scenario_text.removeprefix(self.fixture).lstrip("_") or "success"
        self.requests: list[GenericRESTRequest] = []
        self.created_orders: dict[str, dict[str, Any]] = {}

    def execute(self, request: GenericRESTRequest) -> GenericRESTResponse:
        self.requests.append(request)
        if self.failure == "permission_denied":
            raise GenericRESTTransportError("Mock ERP denegó credenciales.", status_code=403, error_type="auth_error")
        if self.failure == "rate_limited":
            raise GenericRESTTransportError("Mock ERP está rate-limited.", status_code=429, error_type="retryable_http")
        if self.failure == "temporary_failure":
            raise GenericRESTTransportError("Mock ERP no está disponible.", status_code=503, error_type="retryable_http")
        if self.failure == "invalid_payload":
            raise GenericRESTTransportError("Mock ERP rechazó el payload.", status_code=400, error_type="permanent_http")
        path = request.path.rstrip("/")
        if request.method == "GET" and ("/customers/" in path or "/clients/" in path):
            if self.failure == "customer_missing":
                return GenericRESTResponse(404, {"error": "customer_not_found"})
            return GenericRESTResponse(200, {"client": "C001"} if self.fixture == "acme_b" else {"code": "C001", "name": "Acme Demo"})
        if request.method == "GET" and ("/products/" in path or "/items/" in path):
            if self.failure == "product_missing":
                return GenericRESTResponse(404, {"error": "product_not_found"})
            return GenericRESTResponse(200, {"sku": "SKU-001", "name": "Producto Acme"})
        if request.method == "GET" and ("by-reference" in path or "by-ref" in path):
            reference = path.rsplit("/", 1)[-1]
            for record in self.created_orders.values():
                if record.get("reference") == reference:
                    return GenericRESTResponse(200, record["response"])
            return GenericRESTResponse(404, {"error": "order_not_found"})
        if request.method == "POST" and (path.endswith("/orders") or path.endswith("/sales-orders")):
            key = self._idempotency_key(request)
            if key in self.created_orders:
                return GenericRESTResponse(201, self.created_orders[key]["response"])
            if self.failure == "timeout_before_create":
                raise GenericRESTTransportError("Timeout antes de crear pedido.", error_type="timeout")
            reference = self._reference(request)
            prefix = "BETA" if self.fixture == "acme_b" else "ACME"
            external_id = f"{prefix}-ORDER-{len(self.created_orders) + 1:06d}"
            response_key = "salesOrderNo" if self.fixture == "acme_b" else "orderNumber"
            response = {response_key: external_id, "status": "created", "reference": reference}
            self.created_orders[key] = {"reference": reference, "response": response, "request": request}
            if self.failure == "timeout_after_create":
                raise GenericRESTTransportError(
                    "Timeout después de crear pedido.", error_type="timeout", remote_commit_unknown=True
                )
            return GenericRESTResponse(201, response)
        return GenericRESTResponse(400, {"error": "mock_operation_not_found"})

    @staticmethod
    def _idempotency_key(request: GenericRESTRequest) -> str:
        for key, value in request.headers.items():
            if key.lower() == "idempotency-key":
                return str(value)
        body = request.body or {}
        for key in ("externalReference", "reference", "external_reference", "idempotencyKey"):
            if key in body:
                return str(body[key])
        return ""

    @staticmethod
    def _reference(request: GenericRESTRequest) -> str:
        body = request.body or {}
        for key in ("externalReference", "reference", "external_reference", "external_ref"):
            if key in body:
                return str(body[key])
        return MockGenericRESTTransport._idempotency_key(request)


class GenericRESTConnector(DestinationConnector):
    """Tenant-scoped destination backed by a restricted configured contract."""

    identifier = GENERIC_REST_IDENTIFIER
    capabilities = frozenset()

    def __init__(self, *, config: GenericRESTConfig, transport: GenericRESTTransport) -> None:
        self.config = config
        self.client = GenericRESTClient(config, transport)
        self.transport = transport
        self.identity_system = identity_namespace(config.connection_id)
        self._health = ConnectorHealth(ConnectorStatus.HEALTHY, "Generic REST PoC con MockTransport.")

    @property
    def health(self) -> ConnectorHealth:
        errors = self.validate_configuration()
        if errors:
            return ConnectorHealth(ConnectorStatus.MISCONFIGURED, "; ".join(errors))
        return self._health

    def validate_configuration(self) -> list[str]:
        errors = list(self.config.validation_errors())
        if self.config.tenant_id <= 0:
            errors.append("Generic REST necesita un tenant_id positivo.")
        if self.config.connection_id <= 0:
            errors.append("Generic REST necesita un connection_id positivo.")
        return list(dict.fromkeys(errors))

    @property
    def configured_capabilities(self) -> frozenset[ConnectorCapability]:
        return frozenset(ConnectorCapability(value) for value in configured_capability_values({"base_url": self.config.base_url, "operations": self.config.operations, "real_http_enabled": self.config.real_http_enabled, "timeout_seconds": self.config.timeout_seconds, "max_request_bytes": self.config.max_request_bytes, "max_response_bytes": self.config.max_response_bytes, "max_lines": self.config.max_lines, "auth": self.config.auth, "private_network_allowlist": list(self.config.private_network_allowlist)}))

    @staticmethod
    def external_reference_for_order(order: Order) -> str:
        return f"ANCHI-{order.company_id}-{order.id}"

    def _identity_for_entity(self, db: Session, *, company_id: int, entity_type: str, entity_id: int) -> ExternalIdentity | None:
        return db.scalar(
            select(ExternalIdentity).where(
                ExternalIdentity.company_id == company_id,
                ExternalIdentity.entity_type == entity_type,
                ExternalIdentity.entity_id == entity_id,
                ExternalIdentity.external_system == self.identity_system,
            )
        )

    def _context(self, db: Session, order: Order, *, idempotency_key: str | None = None) -> dict[str, Any]:
        customer_identity = self._identity_for_entity(
            db, company_id=order.company_id, entity_type="customer", entity_id=order.validated_customer_id or 0
        )
        customer = {
            "external_id": customer_identity.external_id if customer_identity else None,
            "name": getattr(order.validated_customer, "fiscal_name", None),
        }
        lines: list[dict[str, Any]] = []
        for line in order.lines or []:
            product_identity = self._identity_for_entity(
                db, company_id=order.company_id, entity_type="product", entity_id=line.validated_product_id or 0
            )
            lines.append(
                {
                    "product": {"external_id": product_identity.external_id if product_identity else None},
                    "quantity": line.quantity,
                    "unit": line.unit,
                    "unit_price": line.unit_price_received,
                    "currency": line.currency_received,
                    "description": line.detected_product or line.original_text,
                }
            )
        reference = self.external_reference_for_order(order)
        return {
            "order": {
                "id": order.id,
                "company_id": order.company_id,
                "external_reference": reference,
                "idempotency_key": idempotency_key or reference,
                "notes": order.notes,
                "customer": customer,
                "lines": lines,
            }
        }

    def _ensure_identity_errors(self, db: Session, order: Order) -> list[str]:
        errors: list[str] = []
        if order.company_id != self.config.tenant_id:
            errors.append("La configuración Generic REST pertenece a otro tenant.")
        if not order.validated_customer_id or self._identity_for_entity(db, company_id=order.company_id, entity_type="customer", entity_id=order.validated_customer_id) is None:
            errors.append("Falta ExternalIdentity Generic REST del cliente para esta connection.")
        if not order.lines:
            errors.append("El pedido no tiene líneas.")
        for line in order.lines or []:
            if not line.validated_product_id or self._identity_for_entity(db, company_id=order.company_id, entity_type="product", entity_id=line.validated_product_id) is None:
                errors.append(f"Falta ExternalIdentity Generic REST del producto en la línea {line.id}.")
            if line.quantity is None or float(line.quantity) <= 0:
                errors.append(f"Cantidad inválida en línea {line.id}.")
        return list(dict.fromkeys(errors))

    def validate_order(self, db: Session, order: Order) -> list[str]:
        errors = self.validate_configuration() + self._ensure_identity_errors(db, order)
        if "orders.create" not in configured_capability_values({"base_url": self.config.base_url, "operations": self.config.operations, "real_http_enabled": self.config.real_http_enabled, "timeout_seconds": self.config.timeout_seconds, "max_request_bytes": self.config.max_request_bytes, "max_response_bytes": self.config.max_response_bytes, "max_lines": self.config.max_lines, "auth": self.config.auth, "private_network_allowlist": list(self.config.private_network_allowlist)}):
            errors.append("La connection Generic REST no tiene orders.create configurado.")
        return list(dict.fromkeys(errors))

    def dry_run_order(self, db: Session, order: Order, *, idempotency_key: str | None = None) -> dict[str, Any]:
        errors = self.validate_order(db, order)
        if errors:
            raise GenericRESTConfigError("No se puede ejecutar dry-run.", errors=errors)
        context = self._context(db, order, idempotency_key=idempotency_key)
        request = self.client.build_request("orders.create", context, idempotency_key=idempotency_key or self.external_reference_for_order(order))
        return {
            "operation": request.operation,
            "method": request.method,
            "url": request.url,
            "path": request.path,
            "query": dict(request.query),
            "headers": {key: value for key, value in request.headers.items() if key.lower() not in {"authorization", "cookie"}},
            "body": request.body,
            "credential_ref_configured": bool(request.credential_ref),
        }

    def _delivery_error(self, exc: GenericRESTTransportError) -> ConnectorDeliveryError:
        if exc.status_code in {401, 403}:
            error_type = "configuration_auth"
            retryable = False
        elif exc.status_code == 409:
            error_type = "idempotency_conflict"
            retryable = False
        elif exc.status_code == 429 or (exc.status_code is not None and 500 <= exc.status_code <= 599):
            error_type = "retryable_http"
            retryable = True
        elif exc.error_type in {"timeout", "connection_error"}:
            error_type = exc.error_type
            retryable = True
        else:
            error_type = exc.error_type
            retryable = False
        return ConnectorDeliveryError(
            str(exc), error_type=error_type, retryable=retryable, remote_commit_unknown=exc.remote_commit_unknown
        )

    def find_order_by_idempotency_key(self, db: Session, *, idempotency_key: str) -> str | None:
        operation_name = "orders.read" if "orders.read" in self.config.operations else "orders.find_by_reference"
        if operation_name not in self.config.operations:
            return None
        context = {"order": {"idempotency_key": idempotency_key, "external_reference": idempotency_key, "lines": [], "customer": {}}}
        try:
            response = self.client.execute(operation_name, context)
        except GenericRESTTransportError as exc:
            if exc.status_code == 404:
                return None
            raise self._delivery_error(exc) from exc
        mapping = self.config.operations[operation_name].get("response_mapping", {})
        mapped = extract_response_mapping(mapping, response.body)
        return str(mapped["external_order_id"]) if mapped.get("external_order_id") else None

    def create_order(self, db: Session, order: Order, *, idempotency_key: str | None = None) -> DestinationOrderResult:
        errors = self.validate_order(db, order)
        if errors:
            raise ConnectorDeliveryError("; ".join(errors), error_type="permanent_validation_error")
        key = idempotency_key or self.external_reference_for_order(order)
        context = self._context(db, order, idempotency_key=key)
        try:
            response = self.client.execute("orders.create", context, idempotency_key=key)
        except GenericRESTTransportError as exc:
            raise self._delivery_error(exc) from exc
        mapping = self.config.operations["orders.create"].get("response_mapping", {})
        mapped = extract_response_mapping(mapping, response.body)
        external_id = str(mapped.get("external_order_id") or "").strip()
        if not external_id:
            raise ConnectorDeliveryError("La respuesta Generic REST no contiene external_order_id.", error_type="invalid_response")
        return DestinationOrderResult(
            connector_key=self.identifier,
            external_order_id=external_id,
            status=str(mapped.get("status") or "created"),
        )

    def lookup_customer(self, db: Session, *, entity_id: int, company_id: int) -> Mapping[str, Any]:
        identity = self._identity_for_entity(db, company_id=company_id, entity_type="customer", entity_id=entity_id)
        if identity is None:
            raise ConnectorDeliveryError("Falta ExternalIdentity Generic REST del cliente.", error_type="permanent_validation_error")
        context = {"order": {"customer": {"external_id": identity.external_id}, "lines": []}}
        try:
            return self.client.execute("customers.read", context).body
        except GenericRESTTransportError as exc:
            raise self._delivery_error(exc) from exc


def _config_from_mapping(config: Mapping[str, Any]) -> GenericRESTConfig:
    errors = validate_generic_rest_config(config)
    if errors:
        raise GenericRESTConfigError("Configuración Generic REST inválida.", errors=errors)
    return GenericRESTConfig(
        tenant_id=int(config.get("tenant_id") or 0),
        connection_id=int(config.get("connection_id") or 0),
        base_url=str(config.get("base_url") or ""),
        operations=config.get("operations") or {},
        credential_ref=str(config.get("secret_provider_ref") or "").strip() or None,
        auth=config.get("auth") if isinstance(config.get("auth"), Mapping) else None,
        timeout_seconds=float(config.get("timeout_seconds", 10)),
        max_request_bytes=int(config.get("max_request_bytes", GENERIC_REST_MAX_REQUEST_BYTES)),
        max_response_bytes=int(config.get("max_response_bytes", GENERIC_REST_MAX_RESPONSE_BYTES)),
        max_lines=int(config.get("max_lines", GENERIC_REST_MAX_LINES)),
        real_http_enabled=bool(config.get("real_http_enabled", False)),
        mock_scenario=str(config.get("mock_scenario") or "acme_a"),
        private_network_allowlist=tuple(config.get("private_network_allowlist") or ()),
    )


def build_generic_rest_mock_connector(config: Mapping[str, Any]) -> GenericRESTConnector:
    config_dict = dict(config)
    config_dict["connection_id"] = int(config_dict.get("connection_id") or 0)
    generic_config = _config_from_mapping(config_dict)
    return GenericRESTConnector(config=generic_config, transport=MockGenericRESTTransport(scenario=generic_config.mock_scenario))


__all__ = [
    "GENERIC_REST_IDENTIFIER",
    "GENERIC_REST_MOCK_CONTRACT",
    "GenericRESTClient",
    "GenericRESTConfig",
    "GenericRESTConnector",
    "GenericRESTRequest",
    "GenericRESTResponse",
    "GenericRESTTransportError",
    "HTTPGenericRESTTransport",
    "MockGenericRESTTransport",
    "build_generic_rest_mock_connector",
]
