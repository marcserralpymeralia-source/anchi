"""Internal boundary for the future IONOS/Sage order adapter.

This module defines a stable, tenant-scoped contract without making any
network or SQL call. Existing CSV, JSON, and FTP exports remain unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol

SAGE_SCHEMA_VERSION = "anchi.external-order.v1"
CONFIRMED_ORDER_STATUSES = frozenset({"pedido_confirmado", "pedido_validado"})
SAGE_RESULT_STATUSES = Literal["success", "retryable_error", "permanent_error"]


@dataclass(frozen=True, slots=True)
class SageValidationResult:
    """Structured result for order or contract validation."""

    errors: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {"ready": self.ready, "errors": list(self.errors)}


class SageContractError(ValueError):
    """Raised when an order cannot produce a valid Sage boundary contract."""

    def __init__(self, errors: tuple[str, ...] | list[str]):
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors) or "Contrato Sage no valido.")


def sage_external_id(company_id: int | None, order_id: int | None) -> str:
    """Return the stable identity shared by all retries of one tenant order."""

    if not isinstance(company_id, int) or company_id <= 0:
        raise ValueError("company_id es obligatorio para external_id.")
    if not isinstance(order_id, int) or order_id <= 0:
        raise ValueError("order_id es obligatorio para external_id.")
    return f"anchi:{company_id}:order:{order_id}:v1"


def sage_external_id_for_order(order: Any) -> str:
    return sage_external_id(
        getattr(order, "company_id", None),
        getattr(order, "id", None),
    )


def _text(value: Any) -> str:
    return str(value or "").strip()


def _positive_quantity(value: Any) -> bool:
    if value is None:
        return False
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return quantity.is_finite() and quantity > 0


def _validated_customer(order: Any) -> Any | None:
    customer = getattr(order, "validated_customer", None)
    if not getattr(order, "validated_customer_id", None):
        return None
    return customer


def _validated_product(line: Any) -> Any | None:
    if not getattr(line, "validated_product_id", None):
        return None
    return getattr(line, "validated_product", None)


def sage_readiness_errors(order: Any) -> list[str]:
    """Return concrete blockers before an order can reach the Sage boundary."""

    errors: list[str] = []
    status = _text(getattr(order, "status", None))
    if status not in CONFIRMED_ORDER_STATUSES:
        errors.append("El pedido no esta confirmado o validado.")

    try:
        sage_external_id_for_order(order)
    except ValueError as exc:
        errors.append(str(exc))

    customer = _validated_customer(order)
    if customer is None or not _text(getattr(customer, "code", None)):
        errors.append("El cliente validado no tiene identificador.")

    lines = list(getattr(order, "lines", None) or [])
    if not lines:
        errors.append("El pedido debe contener al menos una linea.")

    for index, line in enumerate(lines, start=1):
        product = _validated_product(line)
        if product is None:
            errors.append(f"La linea {index} no tiene producto validado.")
        elif not _text(getattr(product, "reference", None)):
            errors.append(f"La linea {index} no tiene referencia validada.")
        if not _positive_quantity(getattr(line, "quantity", None)):
            errors.append(f"La linea {index} no tiene una cantidad positiva.")

    return list(dict.fromkeys(errors))


def is_order_ready_for_sage(order: Any) -> bool:
    return not sage_readiness_errors(order)


def validate_order_for_sage(order: Any) -> SageValidationResult:
    return SageValidationResult(tuple(sage_readiness_errors(order)))


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def _source(order: Any) -> dict[str, Any]:
    source: dict[str, Any] = {"system": "anchi", "order_id": order.id}
    email = getattr(order, "email", None)
    conversation = getattr(order, "conversation", None)
    channel = _text(getattr(email, "provider", None)) or _text(getattr(conversation, "provider", None))
    source_id = _text(getattr(email, "external_id", None))
    if channel:
        source["channel"] = channel
    if source_id:
        source["source_id"] = source_id
    return source


def build_sage_contract(
    order: Any,
    *,
    confirmed_by_user_id: int | None = None,
) -> dict[str, Any]:
    """Build the deterministic v1 payload consumed by a future adapter."""

    validation = validate_order_for_sage(order)
    if not validation.ready:
        raise SageContractError(validation.errors)

    external_id = sage_external_id_for_order(order)
    customer = _validated_customer(order)
    lines = []
    for index, line in enumerate(order.lines, start=1):
        product = _validated_product(line)
        lines.append(
            {
                "line_number": index,
                "product_id": line.validated_product_id,
                "reference": _text(product.reference),
                "description": _text(getattr(product, "name", None)),
                "quantity": line.quantity,
                "unit": _text(getattr(line, "unit", None)),
            }
        )

    contract = {
        "schema_version": SAGE_SCHEMA_VERSION,
        "external_id": external_id,
        "tenant": {"company_id": order.company_id},
        "source": _source(order),
        "customer": {
            "identifier": {
                "type": "internal_code",
                "value": _text(customer.code),
            }
        },
        "order": {
            "order_id": order.id,
            "order_date": _iso(getattr(order, "order_date", None)),
            "requested_delivery_date": _iso(getattr(order, "requested_delivery_date", None)),
            "lines": lines,
        },
        "audit": {
            "confirmed_at": _iso(getattr(order, "confirmed_at", None)),
            "confirmed_by_user_id": confirmed_by_user_id,
            "source_order_id": order.id,
            "correlation_id": f"{external_id}:correlation",
        },
    }
    contract_validation = validate_sage_contract(contract)
    if not contract_validation.ready:
        raise SageContractError(contract_validation.errors)
    return contract


def validate_sage_contract(contract: Mapping[str, Any]) -> SageValidationResult:
    """Validate the shape required at the internal adapter boundary."""

    errors: list[str] = []
    if not isinstance(contract, Mapping):
        return SageValidationResult(("El contrato debe ser un objeto.",))
    if contract.get("schema_version") != SAGE_SCHEMA_VERSION:
        errors.append("Version de contrato no soportada.")
    if not _text(contract.get("external_id")):
        errors.append("external_id es obligatorio.")

    tenant = contract.get("tenant") or {}
    if not isinstance(tenant, Mapping) or not isinstance(tenant.get("company_id"), int) or tenant.get("company_id") <= 0:
        errors.append("tenant.company_id es obligatorio.")

    source = contract.get("source") or {}
    if not isinstance(source, Mapping) or source.get("system") != "anchi" or not source.get("order_id"):
        errors.append("source debe identificar el pedido de Anchi.")

    customer = contract.get("customer") or {}
    identifier = customer.get("identifier") if isinstance(customer, Mapping) else None
    if not isinstance(identifier, Mapping) or not _text(identifier.get("value")):
        errors.append("customer.identifier.value es obligatorio.")

    order = contract.get("order") or {}
    lines = order.get("lines") if isinstance(order, Mapping) else None
    if not isinstance(lines, list) or not lines:
        errors.append("order.lines debe contener al menos una linea.")
    else:
        for index, line in enumerate(lines, start=1):
            if not isinstance(line, Mapping):
                errors.append(f"La linea {index} no es valida.")
                continue
            for field in ("product_id", "reference", "quantity", "unit"):
                if field not in line or (field != "quantity" and not _text(line.get(field))):
                    errors.append(f"La linea {index} no tiene {field}.")
            if not _positive_quantity(line.get("quantity")):
                errors.append(f"La linea {index} no tiene una cantidad positiva.")

    audit = contract.get("audit") or {}
    if not isinstance(audit, Mapping) or not _text(audit.get("correlation_id")):
        errors.append("audit.correlation_id es obligatorio.")
    return SageValidationResult(tuple(dict.fromkeys(errors)))


@dataclass(frozen=True, slots=True)
class SageAdapterResult:
    status: SAGE_RESULT_STATUSES
    external_id: str
    external_reference: str | None = None
    error_code: str | None = None
    message: str | None = None

    @classmethod
    def success(cls, external_id: str, external_reference: str | None = None, message: str | None = None) -> SageAdapterResult:
        return cls("success", external_id, external_reference, message=message)

    @classmethod
    def retryable_error(cls, external_id: str, error_code: str | None = None, message: str | None = None) -> SageAdapterResult:
        return cls("retryable_error", external_id, error_code=error_code, message=message)

    @classmethod
    def permanent_error(cls, external_id: str, error_code: str | None = None, message: str | None = None) -> SageAdapterResult:
        return cls("permanent_error", external_id, error_code=error_code, message=message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "external_id": self.external_id,
            "external_reference": self.external_reference,
            "error_code": self.error_code,
            "message": self.message,
        }


def normalize_adapter_result(result: SageAdapterResult | Mapping[str, Any]) -> SageAdapterResult:
    if isinstance(result, SageAdapterResult):
        return result
    status = result.get("status")
    if status not in {"success", "retryable_error", "permanent_error"}:
        raise ValueError("Resultado de adaptador Sage no soportado.")
    external_id = _text(result.get("external_id"))
    if not external_id:
        raise ValueError("Resultado de adaptador Sage sin external_id.")
    return SageAdapterResult(
        status=status,
        external_id=external_id,
        external_reference=result.get("external_reference"),
        error_code=result.get("error_code"),
        message=result.get("message"),
    )


class SageOrderAdapter(Protocol):
    """Future HTTPS adapter contract; intentionally no transport implementation."""

    def send_order(self, contract: Mapping[str, Any]) -> SageAdapterResult:
        ...
