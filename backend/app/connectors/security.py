"""Shared security boundary for future external connector HTTP.

This module intentionally does not enable network traffic by itself.  Every
request must pass connection, connector, global, credentials and egress gates.
Mocks remain independent of this boundary.
"""

from __future__ import annotations

import base64
import ipaddress
import os
import re
import socket
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from app.core.observability import current_context, redact_http_headers, redact_sensitive_data


class ExternalConnectorErrorCategory(str, Enum):
    AUTHENTICATION = "AUTHENTICATION"
    AUTHORIZATION = "AUTHORIZATION"
    CONFIGURATION = "CONFIGURATION"
    VALIDATION = "VALIDATION"
    RATE_LIMIT = "RATE_LIMIT"
    TIMEOUT = "TIMEOUT"
    NETWORK = "NETWORK"
    REMOTE_TEMPORARY = "REMOTE_TEMPORARY"
    REMOTE_PERMANENT = "REMOTE_PERMANENT"
    EGRESS_BLOCKED = "EGRESS_BLOCKED"
    UNKNOWN = "UNKNOWN"


class SecretProviderError(RuntimeError):
    def __init__(self, message: str, *, category: ExternalConnectorErrorCategory = ExternalConnectorErrorCategory.CONFIGURATION) -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True, slots=True, repr=False)
class SecretValue:
    value: str

    def __repr__(self) -> str:
        return "SecretValue([redacted])"


class SecretProvider(Protocol):
    def resolve(self, *, tenant_id: int, connection_id: int, credential_ref: str, credential_name: str) -> SecretValue: ...


_SECRET_REF_RE = re.compile(r"^tenant-(?P<tenant>\d+)$")
_CONNECTION_REF_RE = re.compile(r"^connection-(?P<connection>\d+)$")
_SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


def _secret_env_name(*, tenant_id: int, connection_id: int, credential_name: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9]+", "_", credential_name).strip("_").upper()
    return f"ANCHI_SECRET_TENANT_{tenant_id}_CONNECTION_{connection_id}_{safe_name}"


def _parse_secret_ref(credential_ref: str, *, tenant_id: int, connection_id: int) -> str:
    parsed = urlsplit(str(credential_ref or ""))
    if parsed.scheme != "secret" or not parsed.netloc or parsed.query or parsed.fragment:
        raise SecretProviderError("credential_ref debe usar el formato secret://tenant-N/connection-N/name.")
    tenant_match = _SECRET_REF_RE.fullmatch(parsed.netloc)
    parts = [part for part in parsed.path.split("/") if part]
    connection_match = _CONNECTION_REF_RE.fullmatch(parts[0]) if parts else None
    name = "/".join(parts[1:]) if connection_match else ""
    if not tenant_match or not connection_match or not name or not _SECRET_NAME_RE.fullmatch(name):
        raise SecretProviderError("credential_ref no tiene un scope tenant/connection válido.")
    if int(tenant_match.group("tenant")) != int(tenant_id) or int(connection_match.group("connection")) != int(connection_id):
        raise SecretProviderError("credential_ref no pertenece al tenant o connection solicitados.")
    return name


class EnvironmentSecretProvider:
    """Development/test provider backed by explicitly scoped environment keys."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self.environ = environ if environ is not None else os.environ

    def resolve(self, *, tenant_id: int, connection_id: int, credential_ref: str, credential_name: str) -> SecretValue:
        name = _parse_secret_ref(credential_ref, tenant_id=tenant_id, connection_id=connection_id)
        if name != credential_name:
            raise SecretProviderError("credential_name no coincide con credential_ref.")
        env_name = _secret_env_name(tenant_id=tenant_id, connection_id=connection_id, credential_name=name)
        value = str(self.environ.get(env_name) or "")
        if not value:
            raise SecretProviderError("La credencial referenciada no está disponible.")
        return SecretValue(value)


class MappingSecretProvider(EnvironmentSecretProvider):
    """Deterministic test seam with the same tenant/connection checks."""

    def __init__(self, values: Mapping[str, str]) -> None:
        super().__init__(values)


class DNSResolver(Protocol):
    def resolve(self, hostname: str, port: int) -> tuple[str, ...]: ...


class SystemDNSResolver:
    def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        try:
            records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise EgressPolicyError("No se pudo resolver el destino externo.", code="DNS_RESOLUTION_FAILED") from exc
        addresses = tuple(sorted({str(record[4][0]) for record in records if record[4]}))
        if not addresses:
            raise EgressPolicyError("El destino externo no tiene direcciones resolubles.", code="DNS_EMPTY")
        return addresses


class EgressPolicyError(RuntimeError):
    def __init__(self, message: str, *, code: str = "EGRESS_BLOCKED") -> None:
        super().__init__(message)
        self.code = code


def _blocked_address(address: str, *, hostname: str, allowed_private_hosts: set[str]) -> bool:
    lowered = hostname.lower().rstrip(".")
    if lowered in {"localhost", "metadata", "metadata.google.internal", "instance-data"}:
        return True
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return True
    if parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified or parsed.is_multicast or parsed.is_reserved:
        return True
    if parsed.is_private and lowered not in allowed_private_hosts:
        return True
    if address in {"169.254.169.254", "100.100.100.200", "fd00:ec2::254"}:
        return True
    return False


@dataclass(frozen=True, slots=True)
class EgressBinding:
    tenant_id: int
    connection_id: int
    scheme: str
    hostname: str
    port: int
    base_path: str
    resolved_addresses: tuple[str, ...]
    allowed_private_hosts: frozenset[str] = frozenset()


class EgressPolicy:
    """Connection-scoped origin policy with DNS rebinding checks."""

    def __init__(self, *, resolver: DNSResolver | None = None) -> None:
        self.resolver = resolver or SystemDNSResolver()

    def bind(
        self,
        *,
        tenant_id: int,
        connection_id: int,
        base_url: str,
        allow_http: bool = False,
        allowed_private_hosts: Sequence[str] = (),
    ) -> EgressBinding:
        try:
            parsed = urlsplit(str(base_url or "").rstrip("/"))
            host = (parsed.hostname or "").lower().rstrip(".")
        except ValueError as exc:
            raise EgressPolicyError("El origin externo no es válido.", code="ORIGIN_INVALID") from exc
        private_hosts = {str(item).lower().rstrip(".") for item in allowed_private_hosts if str(item).strip()}
        if parsed.scheme not in {"https", "http"}:
            raise EgressPolicyError("El destino externo solo admite HTTP/HTTPS.", code="SCHEME_BLOCKED")
        if parsed.scheme != "https" and (not allow_http or os.getenv("APP_ENV", "development").strip().lower() not in {"development", "test"}):
            raise EgressPolicyError("HTTP externo no está permitido por la política.", code="HTTPS_REQUIRED")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or not host:
            raise EgressPolicyError("El destino externo contiene componentes no permitidos.", code="ORIGIN_INVALID")
        if host in {"localhost", "metadata", "metadata.google.internal", "instance-data"} or host.endswith(".localhost"):
            raise EgressPolicyError("El destino externo apunta a un host local/metadata.", code="HOST_BLOCKED")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise EgressPolicyError("El puerto externo no es válido.", code="PORT_INVALID") from exc
        allowed_ports = {443} if parsed.scheme == "https" else {80}
        if port not in allowed_ports:
            raise EgressPolicyError("El puerto externo no está autorizado.", code="PORT_BLOCKED")
        try:
            addresses = self.resolver.resolve(host, port)
        except EgressPolicyError:
            raise
        except OSError as exc:
            raise EgressPolicyError("No se pudo resolver el destino externo.", code="DNS_RESOLUTION_FAILED") from exc
        if not addresses:
            raise EgressPolicyError("El destino externo no tiene direcciones resolubles.", code="DNS_EMPTY")
        if any(_blocked_address(address, hostname=host, allowed_private_hosts=private_hosts) for address in addresses):
            raise EgressPolicyError("La resolución DNS contiene una dirección no permitida.", code="DNS_ADDRESS_BLOCKED")
        return EgressBinding(
            tenant_id=tenant_id,
            connection_id=connection_id,
            scheme=parsed.scheme,
            hostname=host,
            port=port,
            base_path=parsed.path.rstrip("/") or "",
            resolved_addresses=addresses,
            allowed_private_hosts=frozenset(private_hosts),
        )

    def validate_url(self, binding: EgressBinding, url: str) -> None:
        try:
            parsed = urlsplit(str(url or ""))
            host = (parsed.hostname or "").lower().rstrip(".")
        except ValueError as exc:
            raise EgressPolicyError("La URL del request no es válida.", code="URL_INVALID") from exc
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise EgressPolicyError("El puerto del request no es válido.", code="PORT_INVALID") from exc
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise EgressPolicyError("La URL del request contiene componentes no permitidos.", code="URL_INVALID")
        if (parsed.scheme, host, port) != (binding.scheme, binding.hostname, binding.port):
            raise EgressPolicyError("El request intenta cambiar el destino de la connection.", code="DESTINATION_MISMATCH")
        if binding.base_path and not (parsed.path == binding.base_path or parsed.path.startswith(binding.base_path + "/")):
            raise EgressPolicyError("El request intenta salir del path base autorizado.", code="PATH_MISMATCH")
        try:
            current = self.resolver.resolve(binding.hostname, binding.port)
        except EgressPolicyError:
            raise
        except OSError as exc:
            raise EgressPolicyError("No se pudo resolver de nuevo el destino externo.", code="DNS_RESOLUTION_FAILED") from exc
        if not current:
            raise EgressPolicyError("El destino externo no tiene direcciones resolubles.", code="DNS_EMPTY")
        if set(current) != set(binding.resolved_addresses):
            raise EgressPolicyError("La resolución DNS cambió después de validar la connection.", code="DNS_REBINDING")
        if any(_blocked_address(address, hostname=binding.hostname, allowed_private_hosts=set(binding.allowed_private_hosts)) for address in current):
            raise EgressPolicyError("La resolución DNS actual contiene una dirección no permitida.", code="DNS_ADDRESS_BLOCKED")


@dataclass(frozen=True, slots=True)
class SecureHTTPResponse:
    status_code: int
    body: Mapping[str, Any]
    headers: Mapping[str, str] = field(default_factory=dict)
    body_bytes: int = 0


class SecureHTTPTransportError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: ExternalConnectorErrorCategory,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class SecureHTTPTransportConfig:
    tenant_id: int
    connection_id: int
    connector_key: str
    base_url: str
    credential_ref: str | None
    credential_name: str = "api_key"
    auth_type: str = "bearer"
    auth_header: str = "X-API-Key"
    auth_username: str = ""
    connection_enabled: bool = False
    connector_enabled: bool = False
    global_enabled: bool | None = None
    allow_http: bool = False
    allowed_private_hosts: tuple[str, ...] = ()
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 15.0
    total_timeout_seconds: float = 20.0
    max_request_bytes: int = 256 * 1024
    max_response_bytes: int = 1024 * 1024
    max_url_length: int = 4096
    max_headers: int = 64


@dataclass(frozen=True, slots=True)
class SecureHTTPAuditEvent:
    tenant_id: int
    connection_id: int
    connector_key: str
    operation: str
    event_type: str
    outcome: str
    correlation_id: str
    request_id: str
    destination_hostname: str | None = None
    http_status: int | None = None
    duration_ms: int | None = None
    error_category: str | None = None


class SecureHTTPTransport:
    """Single future network seam; no retries and redirects always disabled."""

    def __init__(
        self,
        config: SecureHTTPTransportConfig,
        *,
        secret_provider: SecretProvider | None = None,
        egress_policy: EgressPolicy | None = None,
        http_client: Any | None = None,
        audit_sink: Callable[[SecureHTTPAuditEvent], None] | None = None,
    ) -> None:
        self.config = config
        self.secret_provider = secret_provider or EnvironmentSecretProvider()
        self.egress_policy = egress_policy or EgressPolicy()
        self.http_client = http_client
        self.audit_sink = audit_sink
        self.binding = self.egress_policy.bind(
            tenant_id=config.tenant_id,
            connection_id=config.connection_id,
            base_url=config.base_url,
            allow_http=config.allow_http and os.getenv("APP_ENV", "development").lower() in {"development", "test"},
            allowed_private_hosts=config.allowed_private_hosts,
        )

    @staticmethod
    def global_gate_enabled() -> bool:
        return str(os.getenv("EXTERNAL_CONNECTOR_HTTP_ENABLED", "false")).strip().lower() in {"1", "true", "yes", "on"}

    def gate_status(self) -> dict[str, Any]:
        global_enabled = self.global_gate_enabled()
        if not self.config.connection_enabled:
            return {"status": "blocked", "reason": "connection_disabled"}
        if not self.config.connector_enabled:
            return {"status": "blocked", "reason": "connector_gate"}
        if not global_enabled:
            return {"status": "blocked", "reason": "global_gate"}
        if not self.config.credential_ref:
            return {"status": "blocked", "reason": "missing_credentials"}
        return {"status": "ready", "reason": "all_gates_pass"}

    def execute(self, request: Any) -> SecureHTTPResponse:
        request_id = str(getattr(request, "request_id", "") or uuid4().hex)
        context = current_context()
        correlation_id = str(getattr(request, "correlation_id", None) or context.get("correlation_id") or request_id)
        started = time.perf_counter()
        gate = self.gate_status()
        try:
            if gate["status"] != "ready":
                raise SecureHTTPTransportError(
                    f"HTTP externo bloqueado: {gate['reason']}.",
                    category=ExternalConnectorErrorCategory.CONFIGURATION,
                    code=str(gate["reason"]),
                )
            url = str(getattr(request, "url", ""))
            if len(url) > self.config.max_url_length:
                raise SecureHTTPTransportError("La URL externa supera el límite técnico.", category=ExternalConnectorErrorCategory.VALIDATION, code="URL_TOO_LARGE")
            self.egress_policy.validate_url(self.binding, url)
            raw_headers = {str(key): str(value) for key, value in (getattr(request, "headers", {}) or {}).items()}
            if len(raw_headers) > self.config.max_headers:
                raise SecureHTTPTransportError("El request supera el máximo de headers.", category=ExternalConnectorErrorCategory.VALIDATION, code="HEADERS_TOO_MANY")
            if any(key.lower() in {"authorization", "cookie", "set-cookie", "proxy-authorization", "x-api-key", "api-key", "apikey", "ocp-apim-subscription-key", "subscription-key"} for key in raw_headers):
                raise SecureHTTPTransportError("Las credenciales solo pueden inyectarse mediante SecretProvider.", category=ExternalConnectorErrorCategory.CONFIGURATION, code="RAW_CREDENTIAL_HEADER")
            body = getattr(request, "body", None)
            if body is None:
                body_bytes = 0
            else:
                import json

                body_bytes = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if body_bytes > self.config.max_request_bytes:
                raise SecureHTTPTransportError("El request supera el límite técnico.", category=ExternalConnectorErrorCategory.VALIDATION, code="REQUEST_TOO_LARGE")
            secret = self.secret_provider.resolve(
                tenant_id=self.config.tenant_id,
                connection_id=self.config.connection_id,
                credential_ref=str(self.config.credential_ref),
                credential_name=self.config.credential_name,
            )
            headers = dict(raw_headers)
            self._inject_credential(headers, secret.value)
            response = self._request(request, headers)
            status_code = int(getattr(response, "status_code", 0) or 0)
            if 300 <= status_code < 400:
                raise SecureHTTPTransportError("Redirect externo bloqueado.", category=ExternalConnectorErrorCategory.EGRESS_BLOCKED, code="REDIRECT_BLOCKED", status_code=status_code)
            if status_code in {401}:
                raise SecureHTTPTransportError("El destino externo rechazó la autenticación.", category=ExternalConnectorErrorCategory.AUTHENTICATION, code="HTTP_401", status_code=status_code)
            if status_code in {403}:
                raise SecureHTTPTransportError("El destino externo rechazó la autorización.", category=ExternalConnectorErrorCategory.AUTHORIZATION, code="HTTP_403", status_code=status_code)
            if status_code == 429:
                raise SecureHTTPTransportError("El destino externo aplicó rate limiting.", category=ExternalConnectorErrorCategory.RATE_LIMIT, code="HTTP_429", status_code=status_code)
            if status_code == 408:
                raise SecureHTTPTransportError("El destino externo agotó el tiempo de espera.", category=ExternalConnectorErrorCategory.TIMEOUT, code="HTTP_408", status_code=status_code)
            if status_code >= 500:
                raise SecureHTTPTransportError("El destino externo devolvió un error temporal.", category=ExternalConnectorErrorCategory.REMOTE_TEMPORARY, code=f"HTTP_{status_code}", status_code=status_code)
            if 400 <= status_code < 500:
                raise SecureHTTPTransportError("El destino externo rechazó el request.", category=ExternalConnectorErrorCategory.REMOTE_PERMANENT, code=f"HTTP_{status_code}", status_code=status_code)
            content = getattr(response, "content", b"") or b""
            content_length = getattr(response, "headers", {}).get("content-length") if getattr(response, "headers", None) else None
            if content_length and int(content_length) > self.config.max_response_bytes:
                raise SecureHTTPTransportError("La respuesta externa supera el límite técnico.", category=ExternalConnectorErrorCategory.VALIDATION, code="RESPONSE_TOO_LARGE", status_code=status_code)
            if len(content) > self.config.max_response_bytes:
                raise SecureHTTPTransportError("La respuesta externa supera el límite técnico.", category=ExternalConnectorErrorCategory.VALIDATION, code="RESPONSE_TOO_LARGE", status_code=status_code)
            try:
                body_payload = response.json()
            except Exception as exc:  # noqa: BLE001
                raise SecureHTTPTransportError("La respuesta externa no es JSON válido.", category=ExternalConnectorErrorCategory.REMOTE_PERMANENT, code="INVALID_JSON", status_code=status_code) from exc
            self._emit(
                SecureHTTPAuditEvent(
                    tenant_id=self.config.tenant_id,
                    connection_id=self.config.connection_id,
                    connector_key=self.config.connector_key,
                    operation=str(getattr(request, "operation", "unknown")),
                    event_type="http.request_attempted",
                    outcome="succeeded",
                    correlation_id=correlation_id,
                    request_id=request_id,
                    destination_hostname=self.binding.hostname,
                    http_status=status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000),
                )
            )
            return SecureHTTPResponse(status_code=status_code, body=body_payload if isinstance(body_payload, Mapping) else {}, headers=redact_http_headers(getattr(response, "headers", {}) or {}), body_bytes=len(content))
        except EgressPolicyError as exc:
            error = SecureHTTPTransportError(str(exc), category=ExternalConnectorErrorCategory.EGRESS_BLOCKED, code=exc.code)
            self._emit_blocked(request, request_id, correlation_id, started, error)
            raise error from exc
        except SecretProviderError as exc:
            error = SecureHTTPTransportError(str(exc), category=ExternalConnectorErrorCategory.CONFIGURATION, code="CREDENTIAL_UNAVAILABLE")
            self._emit_blocked(request, request_id, correlation_id, started, error)
            raise error from exc
        except SecureHTTPTransportError as exc:
            self._emit_blocked(request, request_id, correlation_id, started, exc)
            raise
        except TimeoutError as exc:
            error = SecureHTTPTransportError("Timeout en HTTP externo.", category=ExternalConnectorErrorCategory.TIMEOUT, code="TIMEOUT")
            self._emit_blocked(request, request_id, correlation_id, started, error)
            raise error from exc
        except OSError as exc:
            error = SecureHTTPTransportError("Error de red en HTTP externo.", category=ExternalConnectorErrorCategory.NETWORK, code="NETWORK")
            self._emit_blocked(request, request_id, correlation_id, started, error)
            raise error from exc
        except Exception as exc:  # noqa: BLE001
            error = SecureHTTPTransportError("Error inesperado en HTTP externo.", category=ExternalConnectorErrorCategory.UNKNOWN, code="UNKNOWN")
            self._emit_blocked(request, request_id, correlation_id, started, error)
            raise error from exc

    def _request(self, request: Any, headers: Mapping[str, str]) -> Any:
        client = self.http_client
        if client is None:
            import httpx

            timeout = httpx.Timeout(
                timeout=self.config.total_timeout_seconds,
                connect=self.config.connect_timeout_seconds,
                read=self.config.read_timeout_seconds,
            )
            with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as owned_client:
                return owned_client.request(
                    str(getattr(request, "method", "GET")),
                    str(getattr(request, "url", "")),
                    headers=headers,
                    params=getattr(request, "query", {}) or {},
                    json=getattr(request, "body", None),
                )
        return client.request(
            str(getattr(request, "method", "GET")),
            str(getattr(request, "url", "")),
            headers=headers,
            params=getattr(request, "query", {}) or {},
            json=getattr(request, "body", None),
            follow_redirects=False,
            timeout=self.config.total_timeout_seconds,
        )

    def _inject_credential(self, headers: dict[str, str], secret: str) -> None:
        auth_type = self.config.auth_type
        if auth_type == "bearer":
            headers["Authorization"] = f"Bearer {secret}"
        elif auth_type == "api_key":
            headers[self.config.auth_header] = secret
        elif auth_type == "basic":
            token = base64.b64encode(f"{self.config.auth_username}:{secret}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        elif auth_type == "custom_header":
            headers[self.config.auth_header] = secret
        else:
            raise SecureHTTPTransportError("Tipo de credencial no soportado.", category=ExternalConnectorErrorCategory.CONFIGURATION, code="AUTH_TYPE_UNSUPPORTED")

    def _emit(self, event: SecureHTTPAuditEvent) -> None:
        if self.audit_sink:
            self.audit_sink(event)

    def _emit_blocked(self, request: Any, request_id: str, correlation_id: str, started: float, error: SecureHTTPTransportError) -> None:
        self._emit(
            SecureHTTPAuditEvent(
                tenant_id=self.config.tenant_id,
                connection_id=self.config.connection_id,
                connector_key=self.config.connector_key,
                operation=str(getattr(request, "operation", "unknown")),
                event_type="http.request_blocked",
                outcome="blocked",
                correlation_id=correlation_id,
                request_id=request_id,
                destination_hostname=self.binding.hostname,
                http_status=error.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000),
                error_category=error.category.value,
            )
        )


def external_http_gate_status(connection: Any, *, connector_runtime_enabled: bool = False) -> dict[str, str]:
    if str(getattr(connection, "state", "")).upper() != "ACTIVE":
        return {"status": "blocked", "reason": "connection_disabled"}
    if not connector_runtime_enabled:
        return {"status": "blocked", "reason": "connector_gate"}
    if not SecureHTTPTransport.global_gate_enabled():
        return {"status": "blocked", "reason": "global_gate"}
    if not getattr(connection, "credentials_configured", False) or not getattr(connection, "secret_provider_ref", None):
        return {"status": "blocked", "reason": "missing_credentials"}
    return {"status": "ready", "reason": "all_gates_pass"}


def safe_destination_hostname(url: str) -> str | None:
    try:
        host = urlsplit(str(url or "")).hostname
    except ValueError:
        return None
    return host.lower().rstrip(".") if host else None


__all__ = [
    "DNSResolver",
    "EgressBinding",
    "EgressPolicy",
    "EgressPolicyError",
    "EnvironmentSecretProvider",
    "ExternalConnectorErrorCategory",
    "MappingSecretProvider",
    "SecretProvider",
    "SecretProviderError",
    "SecretValue",
    "SecureHTTPAuditEvent",
    "SecureHTTPResponse",
    "SecureHTTPTransport",
    "SecureHTTPTransportConfig",
    "SecureHTTPTransportError",
    "SystemDNSResolver",
    "external_http_gate_status",
    "safe_destination_hostname",
]
