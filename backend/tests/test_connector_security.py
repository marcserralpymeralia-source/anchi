from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("EXTERNAL_CONNECTOR_HTTP_ENABLED", "true")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.security import (  # noqa: E402
    EgressPolicy,
    EgressPolicyError,
    ExternalConnectorErrorCategory,
    MappingSecretProvider,
    SecretProviderError,
    SecureHTTPTransport,
    SecureHTTPTransportConfig,
    SecureHTTPTransportError,
    external_http_gate_status,
)
from app.core.observability import redact_http_headers, redact_sensitive_data  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import AuditLog, Company, IntegrationConnection  # noqa: E402
from app.integrations.audit import append_integration_audit, append_secure_http_audit, integration_audit_to_dict, list_integration_audit  # noqa: E402


@dataclass
class FakeResponse:
    status_code: int = 200
    payload: dict | None = None
    content: bytes = b'{"ok":true}'
    headers: dict[str, str] | None = None

    def json(self):
        if self.payload is None:
            return json.loads(self.content.decode("utf-8"))
        return self.payload


class FakeClient:
    def __init__(self, response: FakeResponse | None = None):
        self.response = response or FakeResponse()
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.response


class FakeResolver:
    def __init__(self, addresses: tuple[str, ...] = ("93.184.216.34",), sequences: list[tuple[str, ...]] | None = None):
        self.addresses = addresses
        self.sequences = list(sequences or [])
        self.calls: list[tuple[str, int]] = []

    def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        self.calls.append((hostname, port))
        return self.sequences.pop(0) if self.sequences else self.addresses


def request(**overrides):
    values = {
        "operation": "orders.create",
        "method": "POST",
        "url": "https://erp.example/api/orders",
        "path": "/api/orders",
        "headers": {"Accept": "application/json"},
        "query": {},
        "body": {"reference": "A-1"},
        "request_id": "req-1",
        "correlation_id": "corr-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def transport(*, client=None, resolver=None, audit_sink=None, **overrides):
    config_values = {
        "tenant_id": 1,
        "connection_id": 7,
        "connector_key": "generic_rest",
        "base_url": "https://erp.example/api",
        "credential_ref": "secret://tenant-1/connection-7/api_key",
        "connection_enabled": True,
        "connector_enabled": True,
        "global_enabled": True,
    }
    config_values.update(overrides)
    config = SecureHTTPTransportConfig(**config_values)
    provider = MappingSecretProvider({"ANCHI_SECRET_TENANT_1_CONNECTION_7_API_KEY": "top-secret"})
    return SecureHTTPTransport(config, secret_provider=provider, egress_policy=EgressPolicy(resolver=resolver or FakeResolver()), http_client=client or FakeClient(), audit_sink=audit_sink)


class SecretProviderTests(unittest.TestCase):
    def test_resolves_only_exact_tenant_and_connection_scope(self):
        provider = MappingSecretProvider({"ANCHI_SECRET_TENANT_1_CONNECTION_7_API_KEY": "value"})
        self.assertEqual(provider.resolve(tenant_id=1, connection_id=7, credential_ref="secret://tenant-1/connection-7/api_key", credential_name="api_key").value, "value")

    def test_rejects_other_tenant(self):
        provider = MappingSecretProvider({"ANCHI_SECRET_TENANT_1_CONNECTION_7_API_KEY": "value"})
        with self.assertRaises(SecretProviderError):
            provider.resolve(tenant_id=2, connection_id=7, credential_ref="secret://tenant-1/connection-7/api_key", credential_name="api_key")

    def test_rejects_other_connection(self):
        provider = MappingSecretProvider({"ANCHI_SECRET_TENANT_1_CONNECTION_7_API_KEY": "value"})
        with self.assertRaises(SecretProviderError):
            provider.resolve(tenant_id=1, connection_id=8, credential_ref="secret://tenant-1/connection-7/api_key", credential_name="api_key")

    def test_rejects_credential_name_mismatch(self):
        provider = MappingSecretProvider({"ANCHI_SECRET_TENANT_1_CONNECTION_7_API_KEY": "value"})
        with self.assertRaises(SecretProviderError):
            provider.resolve(tenant_id=1, connection_id=7, credential_ref="secret://tenant-1/connection-7/api_key", credential_name="password")

    def test_rejects_missing_secret(self):
        provider = MappingSecretProvider({})
        with self.assertRaises(SecretProviderError):
            provider.resolve(tenant_id=1, connection_id=7, credential_ref="secret://tenant-1/connection-7/api_key", credential_name="api_key")

    def test_rejects_query_fragment_and_path_traversal_shapes(self):
        provider = MappingSecretProvider({"ANCHI_SECRET_TENANT_1_CONNECTION_7_API_KEY": "value"})
        for ref in ("secret://tenant-1/connection-7/api_key?x=1", "secret://tenant-1/connection-7/api_key#x", "secret://tenant-1/connection-7/../api_key"):
            with self.subTest(ref=ref), self.assertRaises(SecretProviderError):
                provider.resolve(tenant_id=1, connection_id=7, credential_ref=ref, credential_name="api_key")

    def test_secret_repr_is_redacted(self):
        provider = MappingSecretProvider({"ANCHI_SECRET_TENANT_1_CONNECTION_7_API_KEY": "top-secret"})
        secret = provider.resolve(tenant_id=1, connection_id=7, credential_ref="secret://tenant-1/connection-7/api_key", credential_name="api_key")
        self.assertNotIn("top-secret", repr(secret))


class EgressPolicyTests(unittest.TestCase):
    def test_requires_https_by_default(self):
        with self.assertRaisesRegex(EgressPolicyError, "HTTP externo"):
            EgressPolicy(resolver=FakeResolver()).bind(tenant_id=1, connection_id=7, base_url="http://erp.example/api")

    def test_allows_http_only_in_test_when_explicit(self):
        binding = EgressPolicy(resolver=FakeResolver()).bind(tenant_id=1, connection_id=7, base_url="http://erp.example/api", allow_http=True)
        self.assertEqual(binding.port, 80)

    def test_rejects_http_in_production_even_when_requested(self):
        with patch.dict(os.environ, {"APP_ENV": "production"}):
            with self.assertRaises(EgressPolicyError):
                SecureHTTPTransportConfig(tenant_id=1, connection_id=7, connector_key="x", base_url="http://erp.example", credential_ref=None)
                EgressPolicy(resolver=FakeResolver()).bind(tenant_id=1, connection_id=7, base_url="http://erp.example", allow_http=True)

    def test_rejects_credentials_query_and_fragment_in_origin(self):
        policy = EgressPolicy(resolver=FakeResolver())
        for value in ("https://user:pass@erp.example/api", "https://erp.example/api?x=1", "https://erp.example/api#frag"):
            with self.subTest(value=value), self.assertRaises(EgressPolicyError):
                policy.bind(tenant_id=1, connection_id=7, base_url=value)

    def test_rejects_non_default_ports(self):
        with self.assertRaises(EgressPolicyError):
            EgressPolicy(resolver=FakeResolver()).bind(tenant_id=1, connection_id=7, base_url="https://erp.example:8443/api")

    def test_rejects_local_and_metadata_hostnames(self):
        for host in ("localhost", "service.localhost", "metadata.google.internal", "instance-data"):
            with self.subTest(host=host), self.assertRaises(EgressPolicyError):
                EgressPolicy(resolver=FakeResolver()).bind(tenant_id=1, connection_id=7, base_url=f"https://{host}/api")

    def test_rejects_loopback_private_link_local_and_metadata_addresses(self):
        for address in ("127.0.0.1", "10.0.0.4", "169.254.169.254", "::1", "fe80::1"):
            with self.subTest(address=address), self.assertRaises(EgressPolicyError):
                EgressPolicy(resolver=FakeResolver((address,))).bind(tenant_id=1, connection_id=7, base_url="https://erp.example/api")

    def test_allows_explicit_private_host_exception_only(self):
        binding = EgressPolicy(resolver=FakeResolver(("10.0.0.4",))).bind(tenant_id=1, connection_id=7, base_url="https://erp.internal/api", allowed_private_hosts=("erp.internal",))
        self.assertEqual(binding.hostname, "erp.internal")

    def test_rejects_mixed_safe_and_unsafe_dns_answers(self):
        with self.assertRaises(EgressPolicyError):
            EgressPolicy(resolver=FakeResolver(("93.184.216.34", "10.0.0.4"))).bind(tenant_id=1, connection_id=7, base_url="https://erp.example/api")

    def test_rejects_dns_resolution_failure_and_empty_answers(self):
        class BrokenResolver(FakeResolver):
            def resolve(self, hostname, port):
                raise OSError("dns down")

        with self.assertRaises(EgressPolicyError):
            EgressPolicy(resolver=BrokenResolver()).bind(tenant_id=1, connection_id=7, base_url="https://erp.example/api")
        with self.assertRaises(EgressPolicyError):
            EgressPolicy(resolver=FakeResolver(())).bind(tenant_id=1, connection_id=7, base_url="https://erp.example/api")

    def test_rejects_dns_rebinding_between_bind_and_request(self):
        resolver = FakeResolver(sequences=[("93.184.216.34",), ("10.0.0.4",)])
        binding = EgressPolicy(resolver=resolver).bind(tenant_id=1, connection_id=7, base_url="https://erp.example/api")
        with self.assertRaisesRegex(EgressPolicyError, "DNS"):
            EgressPolicy(resolver=resolver).validate_url(binding, "https://erp.example/api/orders")

    def test_rejects_destination_host_port_scheme_path_and_url_injection_changes(self):
        policy = EgressPolicy(resolver=FakeResolver())
        binding = policy.bind(tenant_id=1, connection_id=7, base_url="https://erp.example/api")
        for value in ("https://evil.example/api/orders", "https://erp.example/orders", "http://erp.example/api/orders", "https://erp.example:443.evil/api", "https://erp.example/api/orders?next=x"):
            with self.subTest(value=value), self.assertRaises(EgressPolicyError):
                policy.validate_url(binding, value)


class SecureTransportTests(unittest.TestCase):
    def test_global_gate_defaults_closed_and_makes_no_http_call(self):
        client = FakeClient()
        with patch.dict(os.environ, {"EXTERNAL_CONNECTOR_HTTP_ENABLED": "false"}):
            config = SecureHTTPTransportConfig(tenant_id=1, connection_id=7, connector_key="x", base_url="https://erp.example", credential_ref="secret://tenant-1/connection-7/api_key", connection_enabled=True, connector_enabled=True)
            current = SecureHTTPTransport(config, secret_provider=MappingSecretProvider({}), egress_policy=EgressPolicy(resolver=FakeResolver()), http_client=client)
            with self.assertRaises(SecureHTTPTransportError) as caught:
                current.execute(request(url="https://erp.example/"))
        self.assertEqual(caught.exception.code, "global_gate")
        self.assertEqual(client.calls, [])

    def test_connection_and_connector_gates_are_independent(self):
        for overrides, reason in (({"connection_enabled": False}, "connection_disabled"), ({"connector_enabled": False}, "connector_gate")):
            current = transport(**overrides)
            with self.assertRaises(SecureHTTPTransportError) as caught:
                current.execute(request())
            self.assertEqual(caught.exception.code, reason)

    def test_missing_credential_gate_blocks_before_provider(self):
        current = transport(credential_ref=None)
        with self.assertRaises(SecureHTTPTransportError) as caught:
            current.execute(request())
        self.assertEqual(caught.exception.code, "missing_credentials")

    def test_resolves_credential_late_and_injects_only_at_transport(self):
        client = FakeClient()
        current = transport(client=client)
        response = current.execute(request())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.calls[0]["headers"]["Authorization"], "Bearer top-secret")

    def test_rejects_raw_credential_headers(self):
        current = transport()
        with self.assertRaisesRegex(SecureHTTPTransportError, "SecretProvider"):
            current.execute(request(headers={"Authorization": "Bearer leaked"}))

    def test_redirects_are_never_followed(self):
        client = FakeClient(FakeResponse(status_code=302, headers={"location": "https://evil.example"}, content=b"{}"))
        current = transport(client=client)
        with self.assertRaisesRegex(SecureHTTPTransportError, "Redirect"):
            current.execute(request())
        self.assertFalse(client.calls[0]["follow_redirects"])

    def test_timeout_maps_to_timeout_category(self):
        class TimeoutClient(FakeClient):
            def request(self, method, url, **kwargs):
                raise TimeoutError("slow")

        current = transport(client=TimeoutClient())
        with self.assertRaises(SecureHTTPTransportError) as caught:
            current.execute(request())
        self.assertEqual(caught.exception.category, ExternalConnectorErrorCategory.TIMEOUT)

    def test_no_retry_on_network_failure(self):
        class NetworkClient(FakeClient):
            def request(self, method, url, **kwargs):
                self.calls.append({"method": method})
                raise OSError("offline")

        client = NetworkClient()
        with self.assertRaises(SecureHTTPTransportError):
            transport(client=client).execute(request())
        self.assertEqual(len(client.calls), 1)

    def test_status_taxonomy_maps_auth_rate_limit_and_remote_failures(self):
        cases = ((401, ExternalConnectorErrorCategory.AUTHENTICATION), (403, ExternalConnectorErrorCategory.AUTHORIZATION), (429, ExternalConnectorErrorCategory.RATE_LIMIT), (500, ExternalConnectorErrorCategory.REMOTE_TEMPORARY), (422, ExternalConnectorErrorCategory.REMOTE_PERMANENT))
        for status, category in cases:
            with self.subTest(status=status):
                with self.assertRaises(SecureHTTPTransportError) as caught:
                    transport(client=FakeClient(FakeResponse(status_code=status))).execute(request())
                self.assertEqual(caught.exception.category, category)

    def test_request_and_response_limits_are_enforced(self):
        with self.assertRaisesRegex(SecureHTTPTransportError, "request supera"):
            transport(max_request_bytes=5).execute(request(body={"too": "large"}))
        with self.assertRaisesRegex(SecureHTTPTransportError, "respuesta externa supera"):
            transport(client=FakeClient(FakeResponse(content=b"123456")), max_response_bytes=5).execute(request())

    def test_json_response_is_required(self):
        with self.assertRaisesRegex(SecureHTTPTransportError, "JSON válido"):
            transport(client=FakeClient(FakeResponse(content=b"not-json"))).execute(request())

    def test_http_audit_events_include_correlation_and_safe_destination(self):
        events = []
        current = transport(client=FakeClient(), audit_sink=events.append)
        current.execute(request())
        self.assertEqual(events[0].event_type, "http.request_attempted")
        self.assertEqual(events[0].correlation_id, "corr-1")
        self.assertEqual(events[0].destination_hostname, "erp.example")

    def test_blocked_audit_event_does_not_include_secret_or_headers(self):
        events = []
        current = transport(client=FakeClient(), audit_sink=events.append, credential_ref=None)
        with self.assertRaises(SecureHTTPTransportError):
            current.execute(request())
        self.assertEqual(events[0].event_type, "http.request_blocked")
        self.assertNotIn("top-secret", repr(events[0]))

    def test_external_gate_status_is_safe_and_connection_scoped(self):
        connection = SimpleNamespace(state="ACTIVE", credentials_configured=True, secret_provider_ref="secret://tenant-1/connection-7/api_key")
        with patch.dict(os.environ, {"EXTERNAL_CONNECTOR_HTTP_ENABLED": "true"}):
            self.assertEqual(external_http_gate_status(connection, connector_runtime_enabled=True)["status"], "ready")
        connection.state = "DISABLED"
        self.assertEqual(external_http_gate_status(connection, connector_runtime_enabled=True)["reason"], "connection_disabled")


class RedactionAndAuditTests(unittest.TestCase):
    def test_redacts_http_header_variants(self):
        safe = redact_http_headers({"Authorization": "Bearer secret", "X-API-Key": "secret", "Ocp-Apim-Subscription-Key": "secret", "Accept": "application/json"})
        self.assertEqual(safe["Authorization"], "[redacted]")
        self.assertEqual(safe["X-API-Key"], "[redacted]")
        self.assertEqual(safe["Ocp-Apim-Subscription-Key"], "[redacted]")
        self.assertEqual(safe["Accept"], "application/json")

    def test_redacts_nested_secret_values(self):
        self.assertNotIn("secret-value", json.dumps(redact_sensitive_data({"nested": {"api_key": "secret-value"}})))

    def test_audit_reuses_existing_tenant_model_without_raw_credentials(self):
        with tempfile.TemporaryDirectory() as tempdir:
            engine = create_engine(f"sqlite:///{Path(tempdir, 'tenant.sqlite')}")
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine)
            db = Session()
            db.add(Company(id=1, name="Tenant"))
            append_integration_audit(db, company_id=1, connection_id=7, connector_key="generic_rest", event_type="http.request_blocked", outcome="blocked", message="Authorization: Bearer top-secret-token-1234567890", metadata={"api_key": "top-secret-token-1234567890", "http_status": 403})
            db.commit()
            log = db.scalar(select(AuditLog).where(AuditLog.company_id == 1))
            self.assertIsNotNone(log)
            self.assertNotIn("top-secret-token-1234567890", log.message)
            self.assertEqual(integration_audit_to_dict(log)["metadata"]["outcome"], "blocked")
            self.assertEqual(len(list_integration_audit(db, company_id=1)), 1)
            db.close()
            engine.dispose()

    def test_audit_list_is_tenant_scoped(self):
        with tempfile.TemporaryDirectory() as tempdir:
            engine = create_engine(f"sqlite:///{Path(tempdir, 'tenant.sqlite')}")
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine)
            db = Session()
            db.add_all([Company(id=1, name="A"), Company(id=2, name="B")])
            append_integration_audit(db, company_id=1, event_type="connection.created", outcome="configured", message="A")
            append_integration_audit(db, company_id=2, event_type="connection.created", outcome="configured", message="B")
            db.commit()
            self.assertEqual([item.company_id for item in list_integration_audit(db, company_id=1)], [1])
            db.close()
            engine.dispose()

    def test_secure_http_event_has_persistent_audit_adapter(self):
        with tempfile.TemporaryDirectory() as tempdir:
            engine = create_engine(f"sqlite:///{Path(tempdir, 'tenant.sqlite')}")
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine)
            db = Session()
            db.add(Company(id=1, name="Tenant"))
            event = SimpleNamespace(
                tenant_id=1,
                connection_id=7,
                connector_key="generic_rest",
                event_type="http.request_blocked",
                outcome="blocked",
                operation="orders.create",
                correlation_id="corr-1",
                request_id="req-1",
                destination_hostname="erp.example",
                http_status=403,
                duration_ms=4,
                error_category="AUTHORIZATION",
            )
            append_secure_http_audit(db, event, delivery_id=9, order_id=11)
            db.commit()
            log = db.scalar(select(AuditLog).where(AuditLog.company_id == 1))
            self.assertEqual(log.entity_type, "order_delivery")
            self.assertEqual(integration_audit_to_dict(log)["metadata"]["correlation_id"], "corr-1")
            db.close()
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
