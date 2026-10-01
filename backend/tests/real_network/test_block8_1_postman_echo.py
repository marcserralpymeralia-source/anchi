from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.connectors.contracts import ConnectorRegistry  # noqa: E402
from app.connectors.generic_rest import (  # noqa: E402
    GenericRESTClient,
    GenericRESTConfig,
    GenericRESTConnector,
    HTTPGenericRESTTransport,
)
from app.connectors.generic_rest_mapping import extract_response_mapping  # noqa: E402
from app.connectors.security import (  # noqa: E402
    EgressPolicy,
    EgressPolicyError,
    EnvironmentSecretProvider,
    SecureHTTPTransport,
    SecureHTTPTransportConfig,
    SecureHTTPTransportError,
    external_http_gate_status,
)
from app.core.observability import redact_http_headers  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import (  # noqa: E402
    AuditLog,
    Company,
    Customer,
    ExternalIdentity,
    IntegrationConnection,
    Order,
    OrderDelivery,
    OrderLine,
    Product,
)
from app.integrations.audit import append_secure_http_audit, list_integration_audit  # noqa: E402
from app.orders.delivery import process_order_delivery  # noqa: E402


RUN_REAL_NETWORK = str(os.getenv("RUN_REAL_NETWORK_TESTS", "false")).lower() in {"1", "true", "yes", "on"}


def _operations() -> dict[str, dict]:
    return {
        "orders.create": {
            "method": "POST",
            "path": "/post",
            "request_mapping": {
                "externalReference": "order.external_reference",
                "customerCode": "order.customer.external_id",
                "lines": {
                    "source": "order.lines",
                    "items": {
                        "sku": {"source": "line.product.external_id"},
                        "quantity": {"source": "line.quantity", "type": "number"},
                    },
                },
            },
            "response_mapping": {"external_order_id": "json.externalReference"},
            "idempotency": {"location": "header", "name": "Idempotency-Key"},
            "success_status_codes": [200],
        },
        "orders.read": {
            "method": "GET",
            "path": "/get",
            "response_mapping": {"external_order_id": "json.externalReference"},
            "success_status_codes": [200],
        },
    }


@unittest.skipUnless(RUN_REAL_NETWORK, "RUN_REAL_NETWORK_TESTS=true required; this module is opt-in and uses Internet")
class PostmanEchoValidationTests(unittest.TestCase):
    tenant_id = 8101
    connection_id = 8101
    secret_name = "test_credential"

    @classmethod
    def setUpClass(cls):
        if str(os.getenv("EXTERNAL_CONNECTOR_HTTP_ENABLED", "false")).lower() not in {"1", "true", "yes", "on"}:
            raise RuntimeError("Real network validation requires EXTERNAL_CONNECTOR_HTTP_ENABLED=true only for this command")
        cls.secret_env_name = f"ANCHI_SECRET_TENANT_{cls.tenant_id}_CONNECTION_{cls.connection_id}_TEST_CREDENTIAL"
        cls.original_secret = os.environ.get(cls.secret_env_name)
        cls.dummy_secret = f"anchi-http-validation-{uuid4().hex}"
        os.environ[cls.secret_env_name] = cls.dummy_secret

    @classmethod
    def tearDownClass(cls):
        if cls.original_secret is None:
            os.environ.pop(cls.secret_env_name, None)
        else:
            os.environ[cls.secret_env_name] = cls.original_secret
        os.environ["EXTERNAL_CONNECTOR_HTTP_ENABLED"] = "false"

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.tempdir.name, 'validation.sqlite')}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, expire_on_commit=False)
        self.db = self.Session()
        self.db.add(Company(id=self.tenant_id, name="ANCHI HTTP Validation Tenant"))
        self.connection = IntegrationConnection(
            id=self.connection_id,
            company_id=self.tenant_id,
            connector_key="generic_rest",
            name="Postman Echo Validation",
            state="ACTIVE",
            config_json=json.dumps({"base_url": "https://postman-echo.com", "development_only": True}),
            secret_provider_ref=f"secret://tenant-{self.tenant_id}/connection-{self.connection_id}/{self.secret_name}",
            requires_credentials=True,
            credentials_configured=True,
        )
        self.db.add(self.connection)
        self.db.commit()
        self.events = []
        self.correlation_id = f"corr-http-validation-{uuid4().hex}"

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.tempdir.cleanup()

    def _audit_sink(self, event):
        self.events.append(event)
        append_secure_http_audit(self.db, event)

    def _transport(self, *, connector_enabled: bool = True, base_url: str = "https://postman-echo.com", allowed_hosts=("postman-echo.com",)):
        config = SecureHTTPTransportConfig(
            tenant_id=self.tenant_id,
            connection_id=self.connection_id,
            connector_key="generic_rest",
            base_url=base_url,
            credential_ref=self.connection.secret_provider_ref,
            credential_name=self.secret_name,
            auth_type="custom_header",
            auth_header="X-ANCHI-Test-Credential",
            connection_enabled=True,
            connector_enabled=connector_enabled,
            allowed_hosts=allowed_hosts,
            max_response_bytes=1024 * 1024,
        )
        return SecureHTTPTransport(
            config,
            secret_provider=EnvironmentSecretProvider(),
            audit_sink=self._audit_sink,
        )

    def _generic_client(self):
        config = GenericRESTConfig(
            tenant_id=self.tenant_id,
            connection_id=self.connection_id,
            base_url="https://postman-echo.com",
            operations=_operations(),
            credential_ref=self.connection.secret_provider_ref,
            auth={"type": "custom_header", "header": "X-ANCHI-Test-Credential"},
        )
        return GenericRESTClient(config, HTTPGenericRESTTransport(self._transport()))

    def _get_request(self):
        return SimpleNamespace(
            operation="validation.get",
            method="GET",
            url="https://postman-echo.com/get",
            path="/get",
            headers={"Accept": "application/json"},
            query={"source": "anchi-block8-1"},
            body=None,
            request_id=f"req-{uuid4().hex}",
            correlation_id=self.correlation_id,
            requires_credential=False,
        )

    def test_get_real_network_path_and_persistent_audit(self):
        self.assertEqual(external_http_gate_status(self.connection, connector_runtime_enabled=True)["status"], "ready")
        try:
            response = self._transport().execute(self._get_request())
        except SecureHTTPTransportError as exc:
            if exc.category.value in {"NETWORK", "TIMEOUT", "UNKNOWN"}:
                self.skipTest(f"environment network unavailable: {exc.category.value}")
            raise
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body.get("url"), "https://postman-echo.com/get?source=anchi-block8-1")
        self.db.commit()
        logs = list_integration_audit(self.db, company_id=self.tenant_id)
        self.assertEqual([event.event_type for event in self.events[:2]], ["http.request_attempted", "http.request_succeeded"])
        self.assertTrue(any(log.action == "http.request_succeeded" for log in logs))
        for log in logs:
            self.assertNotIn(self.dummy_secret, log.message)

    def test_generic_rest_dry_run_matches_real_post_echo_and_maps_validation_id(self):
        client = self._generic_client()
        context = {
            "order": {
                "external_reference": "ANCHI-HTTP-TEST-001",
                "customer": {"external_id": "TEST-CUSTOMER"},
                "lines": [{"product": {"external_id": "TEST-SKU"}, "quantity": 2}],
            }
        }
        dry_request = client.build_request("orders.create", context, idempotency_key="ANCHI-HTTP-TEST-001")
        try:
            response = client.execute("orders.create", context, idempotency_key="ANCHI-HTTP-TEST-001")
        except SecureHTTPTransportError as exc:
            if exc.category.value in {"NETWORK", "TIMEOUT", "UNKNOWN"}:
                self.skipTest(f"environment network unavailable: {exc.category.value}")
            raise
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body.get("json"), dry_request.body)
        mapped = extract_response_mapping({"external_order_id": "json.externalReference"}, response.body)
        self.assertEqual(mapped["external_order_id"], "ANCHI-HTTP-TEST-001")
        echoed_headers = response.body.get("headers") or {}
        self.assertTrue(any(self.dummy_secret in str(value) for value in echoed_headers.values()))
        safe_headers = redact_http_headers(echoed_headers)
        self.assertNotIn(self.dummy_secret, json.dumps(safe_headers))
        self.db.commit()
        self.assertTrue(all(self.dummy_secret not in log.message for log in list_integration_audit(self.db, company_id=self.tenant_id)))

    def test_order_delivery_fixture_uses_real_generic_rest_transport(self):
        customer = Customer(id=1, company_id=self.tenant_id, code="TEST-CUSTOMER", fiscal_name="Synthetic Customer")
        product = Product(id=1, company_id=self.tenant_id, reference="TEST-SKU", name="Synthetic Product")
        order = Order(
            id=1,
            company_id=self.tenant_id,
            source_channel_key="test",
            source_mode="structured",
            source_provider="validation",
            source_external_event_id="HTTP-TEST-ORDER-001",
            customer_id=customer.id,
            validated_customer_id=customer.id,
            status="pedido_validado",
            validation_status="validated",
            export_status="ready_to_export",
        )
        line = OrderLine(
            company_id=self.tenant_id,
            order_id=order.id,
            product_id=product.id,
            validated_product_id=product.id,
            source_product_system="validation",
            source_product_external_id="TEST-SKU",
            quantity=2,
            unit="uds",
            unit_price_received=1,
            currency_received="EUR",
            validation_status="validated",
        )
        identity_system = f"generic_rest:{self.connection_id}"
        delivery = OrderDelivery(
            id=1,
            company_id=self.tenant_id,
            order_id=order.id,
            destination_connector_key="generic_rest",
            destination_connection_id=self.connection_id,
            idempotency_key="ANCHI-HTTP-TEST-001",
            status="queued",
            max_attempts=1,
        )
        self.db.add_all([customer, product, order, line, delivery])
        self.db.add_all(
            [
                ExternalIdentity(company_id=self.tenant_id, entity_type="customer", entity_id=customer.id, external_system=identity_system, external_id="TEST-CUSTOMER"),
                ExternalIdentity(company_id=self.tenant_id, entity_type="product", entity_id=product.id, external_system=identity_system, external_id="TEST-SKU"),
            ]
        )
        self.db.flush()
        connector_config = self._generic_client().config
        connector = GenericRESTConnector(config=connector_config, transport=HTTPGenericRESTTransport(self._transport()))
        try:
            result = process_order_delivery(self.db, delivery, registry=ConnectorRegistry([connector]))
        except SecureHTTPTransportError as exc:
            if exc.category.value in {"NETWORK", "TIMEOUT", "UNKNOWN"}:
                self.skipTest(f"environment network unavailable: {exc.category.value}")
            raise
        self.db.commit()
        self.assertEqual(result.delivery.status, "exported")
        self.assertEqual(order.export_status, "exported")
        self.assertTrue(any(log.action == "delivery.succeeded" for log in list_integration_audit(self.db, company_id=self.tenant_id)))

    def test_exact_allowlist_localhost_and_kill_switch_block_before_network(self):
        with self.assertRaisesRegex(EgressPolicyError, "allowlist"):
            self._transport(base_url="https://example.com")
        with self.assertRaises(EgressPolicyError):
            self._transport(base_url="https://127.0.0.1")
        transport = self._transport()
        with patch.dict(os.environ, {"EXTERNAL_CONNECTOR_HTTP_ENABLED": "false"}):
            with self.assertRaises(SecureHTTPTransportError) as blocked:
                transport.execute(self._get_request())
        self.assertEqual(blocked.exception.code, "global_gate")
        gated = self._transport(connector_enabled=False)
        with self.assertRaises(SecureHTTPTransportError) as connector_blocked:
            gated.execute(self._get_request())
        self.assertEqual(connector_blocked.exception.code, "connector_gate")


if __name__ == "__main__":
    unittest.main()
