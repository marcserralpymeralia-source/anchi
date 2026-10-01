"""Restricted declarative mapping contract for the Generic REST connector.

This module intentionally contains no HTTP code.  It validates and evaluates a
small allow-listed mapping language against an ANCHI order context.  The
language has field paths, constants, defaults, optional values, arrays and a
small set of scalar conversions; it has no expressions, templates or code
execution primitives.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


GENERIC_REST_EXTERNAL_SYSTEM_PREFIX = "generic_rest:"
GENERIC_REST_OPERATION_CAPABILITIES = {
    "customers.read": "customers.read",
    "products.read": "products.read",
    "pricing.read": "pricing.read",
    "inventory.read": "inventory.read",
    "orders.create": "orders.create",
    "orders.read": "orders.read",
    "orders.find_by_reference": "orders.read",
}
GENERIC_REST_OPERATION_KEYS = frozenset(GENERIC_REST_OPERATION_CAPABILITIES)
GENERIC_REST_METHODS = frozenset({"GET", "POST", "PUT", "PATCH"})
GENERIC_REST_AUTH_TYPES = frozenset({"bearer", "api_key", "basic", "custom_header"})
GENERIC_REST_MAX_REQUEST_BYTES = 256 * 1024
GENERIC_REST_MAX_RESPONSE_BYTES = 1024 * 1024
GENERIC_REST_MAX_LINES = 500
GENERIC_REST_MAX_TIMEOUT_SECONDS = 30.0
GENERIC_REST_SOURCE_PATHS = frozenset(
    {
        "order.id",
        "order.company_id",
        "order.external_reference",
        "order.idempotency_key",
        "order.notes",
        "order.customer.external_id",
        "order.customer.name",
        "order.lines",
        "order.lines[].product.external_id",
        "order.lines[].quantity",
        "order.lines[].unit",
        "order.lines[].unit_price",
        "order.lines[].currency",
        "order.lines[].description",
        "line.product.external_id",
        "line.quantity",
        "line.unit",
        "line.unit_price",
        "line.currency",
        "line.description",
    }
)
_PATH_PLACEHOLDER = re.compile(r"\{([a-zA-Z][a-zA-Z0-9_]*)\}")
_SAFE_HEADER = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,80}$")
_SECRET_NAME_PARTS = (
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "api-key",
    "apikey",
    "proxy-",
    "forwarded",
    "host",
)
_MISSING = object()


class GenericRESTConfigError(ValueError):
    """Configuration or mapping is outside the restricted contract."""

    def __init__(self, message: str, *, errors: Sequence[str] | None = None) -> None:
        super().__init__(message)
        self.errors = tuple(errors or (message,))


@dataclass(frozen=True, slots=True)
class GenericRESTConfig:
    tenant_id: int
    connection_id: int
    base_url: str
    operations: Mapping[str, Mapping[str, Any]]
    credential_ref: str | None = None
    auth: Mapping[str, Any] | None = None
    timeout_seconds: float = 10.0
    max_request_bytes: int = GENERIC_REST_MAX_REQUEST_BYTES
    max_response_bytes: int = GENERIC_REST_MAX_RESPONSE_BYTES
    max_lines: int = GENERIC_REST_MAX_LINES
    real_http_enabled: bool = False
    mock_scenario: str = "acme_a"
    private_network_allowlist: tuple[str, ...] = ()

    def validation_errors(self) -> list[str]:
        return validate_generic_rest_config(
            {
                "base_url": self.base_url,
                "operations": self.operations,
                "auth": self.auth,
                "timeout_seconds": self.timeout_seconds,
                "max_request_bytes": self.max_request_bytes,
                "max_response_bytes": self.max_response_bytes,
                "max_lines": self.max_lines,
                "real_http_enabled": self.real_http_enabled,
                "private_network_allowlist": list(self.private_network_allowlist),
            }
        )

    def redacted_summary(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "connection_id": self.connection_id,
            "base_url": self.base_url,
            "operations": sorted(self.operations),
            "credential_ref_configured": bool(self.credential_ref),
            "auth_type": str((self.auth or {}).get("type") or "none"),
            "timeout_seconds": self.timeout_seconds,
            "max_request_bytes": self.max_request_bytes,
            "max_response_bytes": self.max_response_bytes,
            "max_lines": self.max_lines,
            "real_http_enabled": self.real_http_enabled,
            "mock_scenario": self.mock_scenario,
        }


def identity_namespace(connection_id: int) -> str:
    if int(connection_id) <= 0:
        raise ValueError("Generic REST necesita un connection_id positivo para aislar identidades.")
    return f"{GENERIC_REST_EXTERNAL_SYSTEM_PREFIX}{int(connection_id)}"


def _as_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GenericRESTConfigError(f"{label} debe ser un objeto JSON.")
    return value


def _validate_base_url(value: Any, allowlist: Sequence[str] = ()) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return ["Generic REST necesita base_url."]
    parsed = urlsplit(value.strip())
    errors: list[str] = []
    if parsed.scheme not in {"http", "https"}:
        errors.append("base_url solo admite http o https.")
    if os.getenv("APP_ENV", "development").strip().lower() == "production" and parsed.scheme != "https":
        errors.append("base_url debe usar HTTPS en production.")
    if not parsed.hostname:
        errors.append("base_url necesita un hostname.")
    if parsed.username or parsed.password:
        errors.append("base_url no puede incluir usuario o contraseña.")
    if parsed.query or parsed.fragment:
        errors.append("base_url no puede incluir query string ni fragmento.")
    try:
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            errors.append("base_url contiene un puerto inválido.")
    except ValueError:
        errors.append("base_url contiene un puerto inválido.")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    allowed = {str(item).strip().lower().rstrip(".") for item in allowlist if str(item).strip()}
    blocked_names = {"localhost", "localhost.localdomain", "metadata", "metadata.google.internal", "instance-data"}
    if host in blocked_names or host.endswith(".localhost") or host.endswith(".local"):
        errors.append("base_url apunta a un hostname local o de metadata no permitido.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if address.is_loopback or address.is_link_local or address.is_unspecified or address.is_reserved:
            errors.append("base_url apunta a loopback, link-local, reservado o unspecified.")
        elif address.is_private and host not in allowed:
            errors.append("base_url apunta a una red privada sin allowlist explícita.")
    return errors


def _validate_relative_path(path: Any, *, label: str) -> list[str]:
    if not isinstance(path, str) or not path.strip():
        return [f"{label} necesita un path relativo."]
    value = path.strip()
    parsed = urlsplit(value)
    errors: list[str] = []
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or value.startswith("//"):
        errors.append(f"{label} no puede ser una URL absoluta ni incluir query/fragmento.")
    if not value.startswith("/"):
        errors.append(f"{label} debe comenzar por '/'.")
    if "\\" in value or any(part == ".." for part in value.split("/")):
        errors.append(f"{label} no puede contener traversal de path.")
    placeholders = _PATH_PLACEHOLDER.findall(value)
    if len(placeholders) != len(set(placeholders)):
        errors.append(f"{label} no puede repetir placeholders.")
    if re.search(r"\{[^}]+\}", value) and not placeholders:
        errors.append(f"{label} contiene un placeholder inválido.")
    return errors


def _mapping_entries(value: Any, label: str) -> tuple[dict[str, Any], list[str]]:
    if isinstance(value, Mapping):
        return {str(key): child for key, child in value.items()}, []
    if not isinstance(value, list):
        return {}, [f"{label} debe ser un objeto o una lista de entradas."]
    result: dict[str, Any] = {}
    errors: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping) or "target" not in item:
            errors.append(f"{label}[{index}] necesita target y mapping.")
            continue
        target = str(item.get("target") or "").strip()
        if not target:
            errors.append(f"{label}[{index}] necesita target.")
        elif target in result:
            errors.append(f"{label} contiene mapping duplicado para {target}.")
        else:
            result[target] = item.get("mapping", item.get("value"))
    return result, errors


def _validate_source(source: Any, *, label: str, item_context: bool = False) -> list[str]:
    if not isinstance(source, str) or not source.strip():
        return [f"{label}.source debe ser un field path."]
    value = source.strip()
    allowed = GENERIC_REST_SOURCE_PATHS
    if item_context and value.startswith("line."):
        allowed = {item for item in allowed if item.startswith("line.")}
    if value not in allowed:
        return [f"{label}.source no está permitido: {value}."]
    return []


def _validate_mapping_value(value: Any, *, label: str, item_context: bool = False) -> list[str]:
    if isinstance(value, str):
        return _validate_source(value, label=label, item_context=item_context)
    if not isinstance(value, Mapping):
        return [f"{label} debe ser un field path, constant u objeto de mapping."]
    errors: list[str] = []
    keys = set(value)
    if "items" in value:
        source = value.get("source")
        errors.extend(_validate_source(source, label=f"{label}.source", item_context=False))
        if source != "order.lines":
            errors.append(f"{label}.items solo puede iterar order.lines.")
        item_map, entry_errors = _mapping_entries(value.get("items"), f"{label}.items")
        errors.extend(entry_errors)
        for key, child in item_map.items():
            errors.extend(_validate_mapping_value(child, label=f"{label}.items.{key}", item_context=True))
        allowed_keys = {"source", "items"}
    elif "source" in value:
        errors.extend(_validate_source(value.get("source"), label=label, item_context=item_context))
        allowed_keys = {"source", "type", "optional", "default"}
    elif "constant" in value:
        allowed_keys = {"constant", "type"}
    else:
        allowed_keys = set()
        for key, child in value.items():
            errors.extend(_validate_mapping_value(child, label=f"{label}.{key}", item_context=item_context))
    unknown = keys - allowed_keys
    if unknown:
        errors.append(f"{label} contiene campos no permitidos: {', '.join(sorted(map(str, unknown)))}.")
    if "type" in value and value.get("type") not in {"string", "integer", "number", "boolean"}:
        errors.append(f"{label}.type no está soportado.")
    return errors


def _validate_headers(headers: Any, label: str) -> list[str]:
    if headers is None:
        return []
    if not isinstance(headers, Mapping):
        return [f"{label} debe ser un objeto de headers estáticos."]
    errors: list[str] = []
    for name, value in headers.items():
        header = str(name).strip()
        lowered = header.lower()
        if not _SAFE_HEADER.fullmatch(header):
            errors.append(f"{label} contiene un nombre de header inválido: {header}.")
        if any(part in lowered for part in _SECRET_NAME_PARTS):
            errors.append(f"{label} no puede contener headers de credenciales o proxy: {header}.")
        if not isinstance(value, str) or len(value) > 500:
            errors.append(f"{label}.{header} debe ser un valor estático corto.")
    return errors


def _validate_operation(name: str, operation: Any) -> list[str]:
    if not isinstance(operation, Mapping):
        return [f"operations.{name} debe ser un objeto."]
    errors: list[str] = []
    allowed_keys = {
        "method",
        "path",
        "path_params",
        "query_mapping",
        "request_mapping",
        "request_headers",
        "response_mapping",
        "success_status_codes",
        "retryable_status_codes",
        "idempotency",
        "pagination",
    }
    unknown = set(operation) - allowed_keys
    if unknown:
        errors.append(f"operations.{name} contiene campos no permitidos: {', '.join(sorted(map(str, unknown)))}.")
    method = str(operation.get("method") or "").upper()
    if method not in GENERIC_REST_METHODS:
        errors.append(f"operations.{name}.method debe ser GET, POST, PUT o PATCH.")
    errors.extend(_validate_relative_path(operation.get("path"), label=f"operations.{name}.path"))
    path = str(operation.get("path") or "")
    placeholders = set(_PATH_PLACEHOLDER.findall(path))
    path_params, path_errors = _mapping_entries(operation.get("path_params", {}), f"operations.{name}.path_params")
    errors.extend(path_errors)
    if placeholders - set(path_params):
        errors.append(f"operations.{name}.path_params no cubre: {', '.join(sorted(placeholders - set(path_params)))}.")
    if set(path_params) - placeholders:
        errors.append(f"operations.{name}.path_params contiene parámetros no usados en path.")
    for key, mapping in path_params.items():
        errors.extend(_validate_mapping_value(mapping, label=f"operations.{name}.path_params.{key}"))
    request_mapping, mapping_errors = _mapping_entries(operation.get("request_mapping", {}), f"operations.{name}.request_mapping")
    errors.extend(mapping_errors)
    for key, mapping in request_mapping.items():
        errors.extend(_validate_mapping_value(mapping, label=f"operations.{name}.request_mapping.{key}"))
    query_mapping, query_errors = _mapping_entries(operation.get("query_mapping", {}), f"operations.{name}.query_mapping")
    errors.extend(query_errors)
    for key, mapping in query_mapping.items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", key):
            errors.append(f"operations.{name}.query_mapping tiene un nombre inválido: {key}.")
        errors.extend(_validate_mapping_value(mapping, label=f"operations.{name}.query_mapping.{key}"))
    errors.extend(_validate_headers(operation.get("request_headers"), f"operations.{name}.request_headers"))
    response_mapping, response_errors = _mapping_entries(operation.get("response_mapping", {}), f"operations.{name}.response_mapping")
    errors.extend(response_errors)
    for key, mapping in response_mapping.items():
        if not isinstance(mapping, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,119}", mapping):
            errors.append(f"operations.{name}.response_mapping.{key} debe ser un path JSON simple.")
    for field, default in (("success_status_codes", [200]), ("retryable_status_codes", [429, 500, 502, 503, 504])):
        codes = operation.get(field, default)
        if not isinstance(codes, list) or not codes or any(not isinstance(code, int) or not 100 <= code <= 599 for code in codes):
            errors.append(f"operations.{name}.{field} debe ser una lista de status HTTP.")
    idempotency = operation.get("idempotency")
    if idempotency is not None:
        if not isinstance(idempotency, Mapping):
            errors.append(f"operations.{name}.idempotency debe ser un objeto.")
        else:
            location = str(idempotency.get("location") or "")
            if location not in {"header", "body"}:
                errors.append(f"operations.{name}.idempotency.location debe ser header o body.")
            name_value = str(idempotency.get("name") or "")
            if not name_value or not _SAFE_HEADER.fullmatch(name_value) or any(part in name_value.lower() for part in _SECRET_NAME_PARTS):
                errors.append(f"operations.{name}.idempotency.name no es seguro.")
    pagination = operation.get("pagination")
    if pagination is not None and (not isinstance(pagination, Mapping) or pagination.get("mode") not in {"none", "page", "cursor"}):
        errors.append(f"operations.{name}.pagination solo admite none, page o cursor.")
    if name in {"orders.create", "orders.find_by_reference"}:
        if "external_order_id" not in response_mapping:
            errors.append(f"operations.{name}.response_mapping necesita external_order_id.")
    return errors


def validate_generic_rest_config(config: Mapping[str, Any] | None) -> list[str]:
    raw = dict(config or {})
    errors = _validate_base_url(raw.get("base_url"), raw.get("private_network_allowlist") or ())
    operations = raw.get("operations")
    if not isinstance(operations, Mapping) or not operations:
        errors.append("Generic REST necesita al menos una operation configurada.")
        operations = {}
    else:
        unknown_operations = set(operations) - GENERIC_REST_OPERATION_KEYS
        if unknown_operations:
            errors.append("Operations no soportadas: " + ", ".join(sorted(map(str, unknown_operations))) + ".")
        for name, operation in operations.items():
            if name in GENERIC_REST_OPERATION_KEYS:
                errors.extend(_validate_operation(str(name), operation))
    if raw.get("real_http_enabled") is True:
        errors.append("HTTP real de Generic REST está bloqueado durante el PoC.")
    timeout = raw.get("timeout_seconds", 10)
    try:
        if float(timeout) <= 0 or float(timeout) > GENERIC_REST_MAX_TIMEOUT_SECONDS:
            errors.append(f"timeout_seconds debe estar entre 0 y {GENERIC_REST_MAX_TIMEOUT_SECONDS}.")
    except (TypeError, ValueError):
        errors.append("timeout_seconds debe ser numérico.")
    for key, maximum, label in (
        ("max_request_bytes", GENERIC_REST_MAX_REQUEST_BYTES, "max_request_bytes"),
        ("max_response_bytes", GENERIC_REST_MAX_RESPONSE_BYTES, "max_response_bytes"),
        ("max_lines", GENERIC_REST_MAX_LINES, "max_lines"),
    ):
        value = raw.get(key, maximum)
        if not isinstance(value, int) or value <= 0 or value > maximum:
            errors.append(f"{label} debe ser entero entre 1 y {maximum}.")
    auth = raw.get("auth")
    if auth is not None:
        if not isinstance(auth, Mapping):
            errors.append("auth debe ser un objeto sin valores secretos.")
        else:
            auth_type = str(auth.get("type") or "")
            if auth_type not in GENERIC_REST_AUTH_TYPES:
                errors.append("auth.type no está soportado.")
            if any(str(key).lower() in {"value", "secret", "password", "token", "api_key", "credentials"} for key in auth):
                errors.append("auth solo puede describir el esquema; el secreto vive en SecretProvider.")
            if auth_type == "custom_header" and not str(auth.get("header") or "").strip():
                errors.append("auth custom_header necesita header.")
            if auth_type == "custom_header":
                header = str(auth.get("header") or "").strip()
                lowered = header.lower()
                if not _SAFE_HEADER.fullmatch(header) or any(part in lowered for part in ("authorization", "cookie", "proxy-", "forwarded", "host")):
                    errors.append("auth.header no es un header de credencial permitido.")
    allowlist = raw.get("private_network_allowlist", [])
    if not isinstance(allowlist, list) or any(not isinstance(item, str) or not item.strip() for item in allowlist):
        errors.append("private_network_allowlist debe ser una lista de hostnames explícitos.")
    return list(dict.fromkeys(errors))


def configured_capability_values(config: Mapping[str, Any] | None) -> frozenset[str]:
    if validate_generic_rest_config(config):
        return frozenset()
    operations = config.get("operations", {}) if isinstance(config, Mapping) else {}
    return frozenset(GENERIC_REST_OPERATION_CAPABILITIES[key] for key in operations if key in GENERIC_REST_OPERATION_CAPABILITIES)


def _read_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split(".") if path else ():
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return _MISSING
    return current


def _resolve_source(context: Mapping[str, Any], source: str) -> Any:
    if source == "order.lines":
        return context.get("order", {}).get("lines", _MISSING)
    if source.startswith("order.lines[]."):
        return _MISSING
    if source.startswith("line."):
        return _read_path(context.get("line", {}), source[5:])
    if source.startswith("order."):
        return _read_path(context, source)
    return _MISSING


def _convert(value: Any, type_name: str | None, label: str) -> Any:
    if value is _MISSING or type_name is None:
        return value
    try:
        if type_name == "string":
            return str(value)
        if type_name == "integer":
            return int(value)
        if type_name == "number":
            return float(value)
        if type_name == "boolean":
            if isinstance(value, bool):
                return value
            normalized = str(value).strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off"}:
                return False
    except (TypeError, ValueError) as exc:
        raise GenericRESTConfigError(f"No se pudo convertir {label} a {type_name}.") from exc
    return value


def _evaluate(value: Any, context: Mapping[str, Any], *, label: str) -> Any:
    if isinstance(value, str):
        result = _resolve_source(context, value)
        if result is _MISSING:
            raise GenericRESTConfigError(f"No existe el valor requerido para {label}: {value}.")
        return result
    if not isinstance(value, Mapping):
        raise GenericRESTConfigError(f"Mapping inválido en {label}.")
    if "constant" in value:
        return _convert(value["constant"], value.get("type"), label)
    if "items" in value:
        source = str(value.get("source") or "")
        collection = _resolve_source(context, source)
        if collection is _MISSING or collection is None:
            if value.get("optional"):
                return []
            raise GenericRESTConfigError(f"No existe la colección requerida para {label}: {source}.")
        if not isinstance(collection, Sequence) or isinstance(collection, (str, bytes, bytearray)):
            raise GenericRESTConfigError(f"{label} necesita una colección.")
        if len(collection) > GENERIC_REST_MAX_LINES:
            raise GenericRESTConfigError("El pedido supera el máximo de líneas permitido.")
        item_map, entry_errors = _mapping_entries(value.get("items"), f"{label}.items")
        if entry_errors:
            raise GenericRESTConfigError("; ".join(entry_errors))
        return [{key: _evaluate(child, {"line": item}, label=f"{label}[].{key}") for key, child in item_map.items()} for item in collection]
    if "source" in value:
        result = _resolve_source(context, str(value.get("source") or ""))
        if result is _MISSING or result is None or result == "":
            if "default" in value:
                result = value["default"]
            elif value.get("optional"):
                return None
            else:
                raise GenericRESTConfigError(f"No existe el valor requerido para {label}: {value.get('source')}.")
        return _convert(result, value.get("type"), label)
    return {key: _evaluate(child, context, label=f"{label}.{key}") for key, child in value.items()}


def evaluate_mapping(mapping: Any, context: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    entries, errors = _mapping_entries(mapping or {}, label)
    if errors:
        raise GenericRESTConfigError("; ".join(errors))
    result: dict[str, Any] = {}
    for target, descriptor in entries.items():
        value = _evaluate(descriptor, context, label=f"{label}.{target}")
        if value is not None:
            current: dict[str, Any] = result
            parts = target.split(".")
            for part in parts[:-1]:
                child = current.setdefault(part, {})
                if not isinstance(child, dict):
                    raise GenericRESTConfigError(f"Destino de mapping en conflicto: {target}.")
                current = child
            current[parts[-1]] = value
    return result


def extract_response_mapping(mapping: Mapping[str, Any], response: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for target, source in mapping.items():
        value = _read_path(response, str(source))
        if value is not _MISSING:
            result[str(target)] = value
    return result


__all__ = [
    "GENERIC_REST_EXTERNAL_SYSTEM_PREFIX",
    "GENERIC_REST_MAX_LINES",
    "GENERIC_REST_MAX_REQUEST_BYTES",
    "GENERIC_REST_MAX_RESPONSE_BYTES",
    "GENERIC_REST_OPERATION_CAPABILITIES",
    "GENERIC_REST_OPERATION_KEYS",
    "GenericRESTConfig",
    "GenericRESTConfigError",
    "configured_capability_values",
    "evaluate_mapping",
    "extract_response_mapping",
    "identity_namespace",
    "validate_generic_rest_config",
]
