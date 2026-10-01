from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.contracts import ConnectorCapability, ConnectorDeliveryError, ConnectorRegistry  # noqa: E402
from app.connectors.generic_rest import (  # noqa: E402
    GenericRESTConfig,
    GenericRESTConnector,
    GenericRESTTransportError,
    HTTPGenericRESTTransport,
    MockGenericRESTTransport,
    build_generic_rest_mock_connector,
)
from app.connectors.generic_rest_mapping import (  # noqa: E402
    GenericRESTConfigError,
    configured_capability_values,
    evaluate_mapping,
    identity_namespace,
    validate_generic_rest_config,
)
from app.connectors.identity import bind_external_identity  # noqa: E402
from app.connectors.registry import IntegrationRegistry, build_runtime_registry  # noqa: E402
from app.connectors.whatsapp_catalog import WhatsAppCatalogInboundConnector  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import Company, Customer, ExternalIdentity, IntegrationConnection, Order, OrderLine, OrderRoute, Product  # noqa: E402
from app.integrations.service import IntegrationConfigurationError, activate_connection, create_connection  # noqa: E402
from app.orders.delivery import DeliveryProcessingError, enqueue_order_delivery, process_order_delivery  # noqa: E402
from app.orders.modular import process_inbound_order_event  # noqa: E402


def _operation_set(*, beta: bool = False, include_reads: bool = True) -> dict:
    if beta:
        operations = {
            "orders.create": {
                "method": "POST",
                "path": "/sales-orders",
                "request_mapping": {
                    "client": "order.customer.external_id",
                    "externalRef": "order.idempotency_key",
                    "items": {
                        "source": "order.lines",
                        "items": {
                            "sku": "line.product.external_id",
                            "qty": {"source": "line.quantity", "type": "number"},
                        },
                    },
                },
                "response_mapping": {"external_order_id": "salesOrderNo", "status": "state"},
                "idempotency": {"location": "body", "name": "externalRef"},
            },
            "orders.read": {
                "method": "GET",
                "path": "/sales-orders/by-ref/{reference}",
                "path_params": {"reference": "order.idempotency_key"},
                "response_mapping": {"external_order_id": "salesOrderNo"},
            },
        }
        if include_reads:
            operations.update(
                {
                    "customers.read": {
                        "method": "GET",
                        "path": "/clients/{client}",
                        "path_params": {"client": "order.customer.external_id"},
                        "response_mapping": {"external_id": "client"},
                    },
                    "products.read": {
                        "method": "GET",
                        "path": "/items/{sku}",
                        "path_params": {"sku": "line.product.external_id"},
                        "response_mapping": {"external_id": "sku"},
                    },
                }
            )
        return operations
    operations = {
        "orders.create": {
            "method": "POST",
            "path": "/orders",
            "request_headers": {"X-Client-Version": "block7"},
            "request_mapping": {
                "customerCode": "order.customer.external_id",
                "externalReference": "order.idempotency_key",
                "lines": {
                    "source": "order.lines",
                    "items": {
                        "articleCode": "line.product.external_id",
                        "quantity": {"source": "line.quantity", "type": "number"},
                        "unit": {"source": "line.unit", "optional": True, "default": "uds"},
                    },
                },
                "comments": {"source": "order.notes", "optional": True, "default": ""},
            },
            "response_mapping": {"external_order_id": "orderNumber", "status": "status"},
            "idempotency": {"location": "header", "name": "Idempotency-Key"},
        },
        "orders.read": {
            "method": "GET",
            "path": "/orders/by-reference/{reference}",
            "path_params": {"reference": "order.idempotency_key"},
            "response_mapping": {"external_order_id": "orderNumber"},
        },
    }
    if include_reads:
        operations.update(
            {
                "customers.read": {
                    "method": "GET",
                    "path": "/customers/{customer_id}",
                    "path_params": {"customer_id": "order.customer.external_id"},
                    "response_mapping": {"external_id": "code"},
                },
                "products.read": {
                    "method": "GET",
                    "path": "/products/{product_sku}",
                    "path_params": {"product_sku": "line.product.external_id"},
                    "response_mapping": {"external_id": "sku"},
                },
            }
        )
    return operations


def _config(*, connection_id: int = 1, beta: bool = False, scenario: str | None = None, include_reads: bool = True) -> dict:
    return {
        "tenant_id": 1,
        "connection_id": connection_id,
        "base_url": "https://beta.example/api" if beta else "https://acme.example/api",
        "operations": _operation_set(beta=beta, include_reads=include_reads),
        "auth": {"type": "bearer"},
        "mock_scenario": scenario or ("acme_b" if beta else "acme_a"),
    }


class GenericRESTConnectorTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{(Path(self.tempdir.name) / 'tenant.sqlite').as_posix()}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, expire_on_commit=False)
        self.first_connection, _ = self._seed_tenant()

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def _seed_tenant(self, *, second_connection: bool = False) -> tuple[IntegrationConnection, IntegrationConnection | None]:
        db = self.Session()
        db.add_all(
            [
                Company(id=1, name="Tenant A"),
                Customer(id=1, company_id=1, code="C001", fiscal_name="Cliente Acme"),
                Product(id=1, company_id=1, reference="P001", name="Producto Acme"),
            ]
        )
        first = IntegrationConnection(
            company_id=1,
            connector_key="generic_rest",
            name="Acme ERP",
            state="ACTIVE",
            config_json=json.dumps(_config(), sort_keys=True),
            secret_provider_ref="secret://generic/1",
            requires_credentials=True,
            credentials_configured=False,
        )
        db.add(first)
        db.flush()
        second = None
        if second_connection:
            second = IntegrationConnection(
                company_id=1,
                connector_key="generic_rest",
                name="Beta ERP",
                state="ACTIVE",
                config_json=json.dumps(_config(connection_id=2, beta=True), sort_keys=True),
                secret_provider_ref="secret://generic/2",
                requires_credentials=True,
                credentials_configured=False,
            )
            db.add(second)
            db.flush()
        identity_targets = [(first.id, identity_namespace(first.id), 1, 1)]
        if second is not None:
            identity_targets.append((second.id, identity_namespace(second.id), 1, 1))
        for connection_id, external_system, customer_id, product_id in identity_targets:
            bind_external_identity(db, company_id=1, entity_type="customer", entity_id=customer_id, external_system=external_system, external_id="C001")
            bind_external_identity(db, company_id=1, entity_type="product", entity_id=product_id, external_system=external_system, external_id="SKU-001")
        bind_external_identity(db, company_id=1, entity_type="customer", entity_id=1, external_system="meta_whatsapp", external_id="34600000001")
        bind_external_identity(db, company_id=1, entity_type="product", entity_id=1, external_system="meta_whatsapp_catalog", external_id="WA-PROD-001")
        db.add(OrderRoute(company_id=1, order_type="order", destination_connector_key="generic_rest", destination_connection_id=first.id))
        db.commit()
        db.close()
        return first, second

    def _new_order(self, db, *, event_id: str = "event"):
        order = Order(
            company_id=1,
            source_channel_key="whatsapp",
            source_mode="structured",
            source_provider="meta",
            source_external_event_id=event_id,
            customer_id=1,
            validated_customer_id=1,
            status="pedido_validado",
            validation_status="validated",
            export_status="ready_to_export",
            notes="Notas demo",
        )
        db.add(order)
        db.flush()
        db.add(
            OrderLine(
                company_id=1,
                order_id=order.id,
                product_id=1,
                validated_product_id=1,
                source_product_system="meta_whatsapp_catalog",
                source_product_external_id="WA-PROD-001",
                quantity=2,
                unit="uds",
                unit_price_received=10,
                currency_received="EUR",
                validation_status="validated",
            )
        )
        db.flush()
        return order

    def _add_second_connection(self) -> IntegrationConnection:
        db = self.Session()
        second = IntegrationConnection(
            company_id=1,
            connector_key="generic_rest",
            name="Beta ERP",
            state="ACTIVE",
            config_json=json.dumps(_config(connection_id=2, beta=True), sort_keys=True),
            secret_provider_ref="secret://generic/2",
            requires_credentials=True,
            credentials_configured=False,
        )
        db.add(second)
        db.flush()
        bind_external_identity(db, company_id=1, entity_type="customer", entity_id=1, external_system=identity_namespace(second.id), external_id="C001")
        bind_external_identity(db, company_id=1, entity_type="product", entity_id=1, external_system=identity_namespace(second.id), external_id="SKU-001")
        db.commit()
        db.close()
        return second

    def test_registry_registers_generic_rest_without_global_capabilities(self):
        definition = IntegrationRegistry().definition("generic_rest")
        self.assertEqual(definition.capabilities, frozenset())
        self.assertEqual(definition.mock_capabilities, frozenset())
        self.assertTrue(definition.mock_only)
        self.assertEqual(definition.factory_path, "app.connectors.registry:build_generic_rest_mock_connector")

    def test_registry_listing_is_lazy(self):
        code = """
import sys
from app.connectors.registry import IntegrationRegistry
items = IntegrationRegistry().list_definitions(environment='test')
assert 'generic_rest' in {item.key for item in items}
assert 'app.connectors.generic_rest' not in sys.modules
"""
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dynamic_capabilities_are_derived_per_connection(self):
        db = self.Session()
        registry = IntegrationRegistry()
        full = create_connection(db, company_id=1, connector_key="generic_rest", name="full", config=_config(), registry=registry)
        limited_config = _config(connection_id=2, include_reads=False)
        limited_config["operations"] = {"orders.create": limited_config["operations"]["orders.create"]}
        limited = create_connection(db, company_id=1, connector_key="generic_rest", name="limited", config=limited_config, registry=registry)
        self.assertEqual({item.value for item in registry.capabilities_for(full)}, {"customers.read", "products.read", "orders.create", "orders.read"})
        self.assertEqual({item.value for item in registry.capabilities_for(limited)}, {"orders.create"})
        db.close()

    def test_invalid_config_is_misconfigured_and_cannot_activate(self):
        db = self.Session()
        connection = create_connection(db, company_id=1, connector_key="generic_rest", name="invalid", config={"base_url": "http://127.0.0.1", "operations": {} })
        self.assertEqual(connection.state, "MISCONFIGURED")
        with self.assertRaises(IntegrationConfigurationError):
            activate_connection(db, company_id=1, connection_id=connection.id)
        db.close()

    def test_secret_is_not_stored_in_config(self):
        db = self.Session()
        with self.assertRaises(IntegrationConfigurationError):
            create_connection(db, company_id=1, connector_key="generic_rest", name="secret", config={**_config(), "auth": {"type": "bearer", "token": "raw"}})
        db.close()

    def test_mapping_rejects_code_and_absolute_url(self):
        config = _config()
        config["operations"]["orders.create"]["request_mapping"] = {"run": {"source": "__import__('os')"}}
        self.assertTrue(any("no está permitido" in error for error in validate_generic_rest_config(config)))
        config = _config()
        config["operations"]["orders.create"]["path"] = "https://attacker.example/orders"
        self.assertTrue(any("URL absoluta" in error for error in validate_generic_rest_config(config)))

    def test_ssrf_localhost_metadata_private_and_scheme_controls(self):
        for base_url in ("http://localhost/api", "http://169.254.169.254/latest", "https://10.0.0.4/api"):
            self.assertTrue(validate_generic_rest_config({"base_url": base_url, "operations": _operation_set()}))
        self.assertTrue(validate_generic_rest_config({"base_url": "ftp://erp.example", "operations": _operation_set()}))
        allowed = {"base_url": "https://10.0.0.4/api", "private_network_allowlist": ["10.0.0.4"], "operations": _operation_set()}
        self.assertFalse(validate_generic_rest_config(allowed))

    def test_mapping_dsl_supports_fields_arrays_constants_defaults_and_types(self):
        mapping = {
            "customerCode": "order.customer.external_id",
            "constant": {"constant": "ACME"},
            "lines": {"source": "order.lines", "items": {"sku": "line.product.external_id", "qty": {"source": "line.quantity", "type": "integer"}}},
            "notes": {"source": "order.notes", "optional": True, "default": "none"},
        }
        result = evaluate_mapping(mapping, {"order": {"customer": {"external_id": "C001"}, "lines": [{"product": {"external_id": "SKU"}, "quantity": "2"}], "notes": None}}, label="request")
        self.assertEqual(result, {"customerCode": "C001", "constant": "ACME", "lines": [{"sku": "SKU", "qty": 2}], "notes": "none"})

    def test_duplicate_list_mapping_is_rejected(self):
        config = _config()
        config["operations"]["orders.create"]["request_mapping"] = [{"target": "x", "mapping": "order.notes"}, {"target": "x", "mapping": "order.id"}]
        self.assertTrue(any("duplicado" in error for error in validate_generic_rest_config(config)))

    def test_external_identity_namespace_is_connection_specific(self):
        first, second = self.first_connection, self._add_second_connection()
        self.assertNotEqual(identity_namespace(first.id), identity_namespace(second.id))
        db = self.Session()
        systems = {row.external_system for row in db.scalars(select(ExternalIdentity)).all()}
        self.assertIn(identity_namespace(first.id), systems)
        self.assertIn(identity_namespace(second.id), systems)
        db.close()

    def test_dry_run_builds_payload_without_transport_execution(self):
        db = self.Session()
        order = self._new_order(db)
        connector = build_generic_rest_mock_connector(_config())
        dry_run = connector.dry_run_order(db, order, idempotency_key="delivery-1")
        self.assertEqual(dry_run["body"]["customerCode"], "C001")
        self.assertEqual(dry_run["body"]["lines"][0]["articleCode"], "SKU-001")
        self.assertEqual(dry_run["headers"]["Idempotency-Key"], "delivery-1")
        self.assertEqual(connector.transport.requests, [])
        db.close()

    def test_http_transport_is_prepared_but_blocked(self):
        with self.assertRaisesRegex(GenericRESTTransportError, "bloqueado"):
            HTTPGenericRESTTransport().execute(None)  # type: ignore[arg-type]

    def test_audit_record_is_safe_and_excludes_payload_and_credentials(self):
        db = self.Session()
        order = self._new_order(db)
        connector = build_generic_rest_mock_connector(_config())
        connector.create_order(db, order, idempotency_key="audit-key")
        self.assertEqual(len(connector.client.audit_records), 1)
        record = connector.client.audit_records[0]
        self.assertEqual(record.connection_id, 1)
        self.assertEqual(record.operation, "orders.create")
        self.assertEqual(record.result_category, "success")
        self.assertNotIn("C001", vars(record) if hasattr(record, "__dict__") else {})
        self.assertNotIn("secret", repr(record).lower())
        db.close()

    def test_acme_order_create_uses_only_configured_mapping(self):
        db = self.Session()
        order = self._new_order(db)
        connector = build_generic_rest_mock_connector(_config())
        result = connector.create_order(db, order, idempotency_key="delivery-acme")
        self.assertEqual(result.external_order_id, "ACME-ORDER-000001")
        request = connector.transport.requests[-1]
        self.assertEqual(request.body["customerCode"], "C001")
        self.assertEqual(request.body["lines"][0]["articleCode"], "SKU-001")
        db.close()

    def test_beta_schema_works_without_connector_code_change(self):
        first = self.first_connection
        second = self._add_second_connection()
        db = self.Session()
        order = self._new_order(db)
        connector = build_generic_rest_mock_connector(_config(connection_id=second.id, beta=True))
        result = connector.create_order(db, order, idempotency_key="delivery-beta")
        self.assertEqual(result.external_order_id, "BETA-ORDER-000001")
        self.assertIn("client", connector.transport.requests[-1].body)
        self.assertNotIn("customerCode", connector.transport.requests[-1].body)
        db.close()

    def test_product_and_customer_lookups_use_declared_operations(self):
        db = self.Session()
        order = self._new_order(db)
        connector = build_generic_rest_mock_connector(_config())
        self.assertEqual(connector.lookup_customer(db, entity_id=1, company_id=1)["code"], "C001")
        self.assertEqual(connector.lookup_product(db, entity_id=1, company_id=1)["sku"], "SKU-001")
        self.assertEqual([request.operation for request in connector.transport.requests], ["customers.read", "products.read"])
        db.close()

    def test_authoritative_pricing_is_not_computed_by_connector(self):
        config = _config()
        config["operations"]["pricing.read"] = {
            "method": "GET",
            "path": "/prices/{reference}",
            "path_params": {"reference": "order.idempotency_key"},
            "response_mapping": {"authoritative_price": "price"},
        }
        self.assertIn("pricing.read", configured_capability_values(config))
        self.assertEqual(validate_generic_rest_config(config), [])

    def test_inventory_is_optional_and_does_not_block_order_create(self):
        config = _config(include_reads=False)
        connector = build_generic_rest_mock_connector(config)
        self.assertNotIn(ConnectorCapability.INVENTORY_READ, connector.configured_capabilities)
        self.assertIn(ConnectorCapability.ORDERS_CREATE, connector.configured_capabilities)

    def test_retryable_and_permanent_statuses_are_mapped(self):
        db = self.Session()
        order = self._new_order(db)
        for scenario, retryable in (("acme_a_rate_limited", True), ("acme_a_invalid_payload", False)):
            connector = build_generic_rest_mock_connector(_config(scenario=scenario))
            with self.assertRaises(ConnectorDeliveryError) as raised:
                connector.create_order(db, order, idempotency_key=scenario)
            self.assertEqual(raised.exception.retryable, retryable)
        db.close()

    def test_idempotency_header_and_body_are_supported(self):
        db = self.Session()
        order = self._new_order(db)
        header_connector = build_generic_rest_mock_connector(_config())
        header_connector.create_order(db, order, idempotency_key="header-key")
        self.assertEqual(header_connector.transport.requests[-1].headers["Idempotency-Key"], "header-key")
        beta_connector = build_generic_rest_mock_connector(_config(beta=True))
        beta_connector.create_order(db, order, idempotency_key="body-key")
        self.assertEqual(beta_connector.transport.requests[-1].body["externalRef"], "body-key")
        db.close()

    def test_remote_success_then_timeout_reconciles_without_duplicate(self):
        db = self.Session()
        order = self._new_order(db)
        connector = build_generic_rest_mock_connector(_config(scenario="acme_a_timeout_after_create"))
        with self.assertRaises(ConnectorDeliveryError) as raised:
            connector.create_order(db, order, idempotency_key="timeout-key")
        self.assertTrue(raised.exception.remote_commit_unknown)
        self.assertEqual(len(connector.transport.created_orders), 1)
        connector.transport.failure = "success"
        self.assertEqual(connector.find_order_by_idempotency_key(db, idempotency_key="timeout-key"), "ACME-ORDER-000001")
        self.assertEqual(len(connector.transport.created_orders), 1)
        db.close()

    def test_same_delivery_uses_order_delivery_reliability_once(self):
        first = self.first_connection
        db = self.Session()
        order = self._new_order(db, event_id="delivery")
        delivery = enqueue_order_delivery(db, order)
        db.commit()
        runtime = build_runtime_registry(db, company_id=1, connection_ids={first.id})
        result = process_order_delivery(db, delivery, registry=runtime)
        db.commit()
        self.assertEqual(result.destination.external_order_id, "ACME-ORDER-000001")
        self.assertEqual(delivery.status, "exported")
        second_result = process_order_delivery(db, delivery, registry=runtime)
        self.assertTrue(second_result.destination.idempotent_replay)
        db.close()

    def test_connection_resolution_is_tenant_scoped_and_disabled_does_not_execute(self):
        first = self.first_connection
        db = self.Session()
        with self.assertRaises(KeyError):
            IntegrationRegistry().resolve(db, company_id=999, connection_id=first.id)
        connection = db.get(IntegrationConnection, first.id)
        connection.state = "DISABLED"
        with self.assertRaises(RuntimeError):
            IntegrationRegistry().resolve(db, company_id=1, connection_id=first.id)
        db.close()

    def test_whatsapp_catalog_structured_flow_remains_zero_llm(self):
        first = self.first_connection
        db = self.Session()
        event = WhatsAppCatalogInboundConnector().to_inbound_order_event(
            {"sender": "34600000001", "message": {"id": "wamid.generic", "from": "34600000001", "type": "order", "order": {"catalog_id": "CAT-1", "product_items": [{"product_retailer_id": "WA-PROD-001", "quantity": 2, "item_price": "10", "currency": "EUR"}]} }},
            company_id=1,
        )
        with patch("app.agent.platform.OrderExtractionAgent.extract", side_effect=AssertionError("structured path must not use LLM")) as extraction:
            result = process_inbound_order_event(db, event, route=True)
        extraction.assert_not_called()
        self.assertEqual(result.delivery.destination_connection_id, first.id)
        db.close()

    def test_sage_odoo_and_reliability_modules_are_not_reimplemented(self):
        self.assertTrue(Path("backend/app/connectors/sage200es.py").exists())
        self.assertTrue(Path("backend/app/connectors/odoo.py").exists())
        self.assertTrue(Path("backend/app/orders/delivery.py").exists())
        self.assertNotIn("sage", GenericRESTConnector.__module__)
        self.assertNotIn("odoo", GenericRESTConnector.__module__)

    def test_response_external_id_mapping_is_not_hardcoded_to_acme(self):
        response = {"salesOrderNo": "BETA-ORDER-1"}
        config = _config(beta=True)
        mapping = config["operations"]["orders.create"]["response_mapping"]
        from app.connectors.generic_rest_mapping import extract_response_mapping

        self.assertEqual(extract_response_mapping(mapping, response)["external_order_id"], "BETA-ORDER-1")

    def test_payload_and_line_limits_are_enforced(self):
        config = _config()
        config["max_lines"] = 1
        connector = build_generic_rest_mock_connector(config)
        db = self.Session()
        order = self._new_order(db)
        db.add(OrderLine(company_id=1, order_id=order.id, validated_product_id=1, quantity=1, validation_status="validated"))
        db.flush()
        with self.assertRaises(GenericRESTConfigError):
            connector.dry_run_order(db, order)
        db.close()


if __name__ == "__main__":
    unittest.main()
