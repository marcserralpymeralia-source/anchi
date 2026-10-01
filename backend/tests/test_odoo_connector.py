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
from app.connectors.identity import bind_external_identity  # noqa: E402
from app.connectors.odoo import (  # noqa: E402
    ODOO_EXTERNAL_SYSTEM,
    ODOO_IDEMPOTENCY_FIELD,
    ODOO_MODEL_CUSTOMER,
    ODOO_MODEL_PRODUCT_VARIANT,
    ODOO_MODEL_SALES_ORDER,
    ODOO_SOURCE_DOCUMENTATION,
    UNVERIFIED_ODOO_CONTRACT,
    MockOdooJSON2Transport,
    OdooExternalIdentity,
    OdooJSON2Config,
    OdooSalesOrderMapper,
    build_odoo_mock_connector,
)
from app.connectors.registry import IntegrationRegistry, build_runtime_registry  # noqa: E402
from app.connectors.whatsapp_catalog import WhatsAppCatalogInboundConnector  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import Company, Customer, ExternalIdentity, IntegrationConnection, Order, OrderLine, OrderRoute, Product  # noqa: E402
from app.integrations.service import IntegrationConfigurationError, activate_connection, create_connection  # noqa: E402
from app.orders.delivery import DeliveryProcessingError, enqueue_order_delivery, process_order_delivery  # noqa: E402
from app.orders.modular import process_inbound_order_event  # noqa: E402


class OdooConnectorTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            f"sqlite:///{(Path(self.tempdir.name) / 'tenant.sqlite').as_posix()}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, expire_on_commit=False)
        self._seed_tenant(1)

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def _seed_tenant(self, company_id: int, *, include_route: bool = True) -> IntegrationConnection:
        db = self.Session()
        company = Company(id=company_id, name=f"Tenant {company_id}")
        customer = Customer(id=company_id, company_id=company_id, code=f"C{company_id:03d}", fiscal_name=f"Cliente {company_id}")
        product = Product(id=company_id, company_id=company_id, reference=f"P{company_id:03d}", name=f"Producto {company_id}")
        config = {
            "base_url": "https://odoo.mock.invalid",
            "database": f"tenant_{company_id}",
            "language": "es_ES",
            "real_http_enabled": False,
        }
        connection = IntegrationConnection(
            company_id=company_id,
            connector_key="odoo",
            name=f"Odoo {company_id}",
            state="ACTIVE",
            config_json=json.dumps(config, sort_keys=True),
            secret_provider_ref=f"secret://odoo/{company_id}",
            requires_credentials=True,
            credentials_configured=False,
        )
        db.add_all([company, customer, product, connection])
        db.flush()
        bind_external_identity(
            db,
            company_id=company_id,
            entity_type="customer",
            entity_id=customer.id,
            external_system="meta_whatsapp",
            external_id=f"3460000000{company_id}",
        )
        bind_external_identity(
            db,
            company_id=company_id,
            entity_type="product",
            entity_id=product.id,
            external_system="meta_whatsapp_catalog",
            external_id=f"WA-PROD-{company_id:03d}",
        )
        bind_external_identity(
            db,
            company_id=company_id,
            entity_type="customer",
            entity_id=customer.id,
            external_system=ODOO_EXTERNAL_SYSTEM,
            external_id=f"ODOO-CUST-{company_id:03d}",
            external_subtype=ODOO_MODEL_CUSTOMER,
        )
        bind_external_identity(
            db,
            company_id=company_id,
            entity_type="product",
            entity_id=product.id,
            external_system=ODOO_EXTERNAL_SYSTEM,
            external_id=f"ODOO-PROD-{company_id:03d}",
            external_subtype=ODOO_MODEL_PRODUCT_VARIANT,
            metadata={"product_tmpl_id": f"ODOO-TMPL-{company_id:03d}", "variant_code": "DEFAULT"},
        )
        if include_route:
            db.add(
                OrderRoute(
                    company_id=company_id,
                    order_type="order",
                    destination_connector_key="odoo",
                    destination_connection_id=connection.id,
                )
            )
        db.commit()
        db.close()
        return connection

    def _connector(self, scenario: str = "success", tenant_id: int = 1):
        return build_odoo_mock_connector(
            {
                "tenant_id": tenant_id,
                "base_url": "https://odoo.mock.invalid",
                "database": f"tenant_{tenant_id}",
                "mock_scenario": scenario,
            }
        )

    def _new_order_and_delivery(self, *, company_id: int = 1, event_suffix: str = "one"):
        db = self.Session()
        order = Order(
            company_id=company_id,
            source_channel_key="whatsapp",
            source_mode="structured",
            source_provider="meta",
            source_external_event_id=f"wamid.odoo.{event_suffix}",
            customer_id=company_id,
            validated_customer_id=company_id,
            status="pedido_validado",
            validation_status="validated",
            export_status="ready_to_export",
            notes="fixture",
        )
        db.add(order)
        db.flush()
        db.add(
            OrderLine(
                company_id=company_id,
                order_id=order.id,
                product_id=company_id,
                validated_product_id=company_id,
                source_product_system="meta_whatsapp_catalog",
                source_product_external_id=f"WA-PROD-{company_id:03d}",
                original_text="producto",
                detected_reference=f"P{company_id:03d}",
                detected_product=f"Producto {company_id}",
                quantity=2,
                unit="uds",
                unit_price_received=10.0,
                currency_received="EUR",
                validation_status="validated",
            )
        )
        db.flush()
        delivery = enqueue_order_delivery(db, order)
        self.assertIsNotNone(delivery)
        db.commit()
        return db, order, delivery

    def test_registry_registers_odoo_with_only_mock_capabilities(self):
        definition = IntegrationRegistry().definition("odoo")
        self.assertEqual(definition.display_name, "Odoo 19 JSON-2")
        self.assertEqual(definition.capabilities, frozenset())
        self.assertIn(ConnectorCapability.ORDERS_CREATE, definition.mock_capabilities)
        self.assertNotIn(ConnectorCapability.INVENTORY_READ, definition.mock_capabilities)
        self.assertTrue(definition.mock_only)
        self.assertEqual(definition.factory_path, "app.connectors.odoo:build_odoo_mock_connector")

    def test_registry_listing_does_not_import_odoo_or_sage_runtime(self):
        code = """
import sys
from app.connectors.registry import IntegrationRegistry
registry = IntegrationRegistry()
assert 'app.connectors.odoo' not in sys.modules
assert 'app.connectors.sage200es' not in sys.modules
assert 'odoo' in {item.key for item in registry.list_definitions(environment='test')}
assert 'app.connectors.odoo' not in sys.modules
assert 'app.connectors.sage200es' not in sys.modules
"""
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_secret_reference_boundary_and_mock_activation(self):
        db = self.Session()
        with self.assertRaises(IntegrationConfigurationError):
            create_connection(
                db,
                company_id=1,
                connector_key="odoo",
                name="Odoo raw secret",
                config={"base_url": "https://odoo.invalid", "api_key": "never-store"},
            )
        connection = create_connection(
            db,
            company_id=1,
            connector_key="odoo",
            name="Odoo principal",
            config={"base_url": "https://odoo.invalid", "database": "demo"},
            secret_provider_ref="secret://odoo/demo",
        )
        activate_connection(db, company_id=1, connection_id=connection.id)
        self.assertEqual(connection.state, "ACTIVE")
        self.assertFalse(connection.credentials_configured)
        self.assertEqual(connection.secret_provider_ref, "secret://odoo/demo")
        db.rollback()
        db.close()

    def test_real_http_is_blocked_and_config_summary_is_redacted(self):
        config = OdooJSON2Config(
            tenant_id=1,
            base_url="https://odoo.invalid",
            credential_ref="secret://odoo/demo",
            real_http_enabled=True,
        )
        self.assertTrue(any("HTTP real" in error for error in config.validation_errors()))
        summary = config.redacted_summary()
        self.assertNotIn("secret://odoo/demo", summary)
        self.assertTrue(summary["has_credential_ref"])

    def test_json2_request_shape_has_database_and_never_a_plaintext_key(self):
        transport = MockOdooJSON2Transport()
        connector = self._connector()
        connector.client.transport = transport
        self.assertIsNotNone(connector.client.read_customer("ODOO-CUST-001"))
        request = transport.requests[0]
        self.assertEqual(request.path, "/json/2/res.partner/read")
        self.assertEqual(request.headers["X-Odoo-Database"], "tenant_1")
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(request.body["ids"], ["ODOO-CUST-001"])

    def test_customer_and_product_mappings_are_external_identity_only(self):
        db, order, _delivery = self._new_order_and_delivery(event_suffix="identity")
        connector = self._connector()
        self.assertEqual(connector.validate_order(db, order), [])
        customer_identity = db.scalar(
            select(ExternalIdentity).where(
                ExternalIdentity.company_id == 1,
                ExternalIdentity.entity_type == "customer",
                ExternalIdentity.external_system == ODOO_EXTERNAL_SYSTEM,
            )
        )
        self.assertEqual(customer_identity.external_id, "ODOO-CUST-001")
        db.close()

    def test_product_variant_identity_is_not_collapsed_to_template(self):
        db, order, _delivery = self._new_order_and_delivery(event_suffix="variants")
        second_product = Product(id=2, company_id=1, reference="P002", name="Producto rojo")
        db.add(second_product)
        db.flush()
        bind_external_identity(
            db,
            company_id=1,
            entity_type="product",
            entity_id=second_product.id,
            external_system=ODOO_EXTERNAL_SYSTEM,
            external_id="ODOO-PROD-002",
            external_subtype=ODOO_MODEL_PRODUCT_VARIANT,
            metadata={"product_tmpl_id": "ODOO-TMPL-001", "variant_code": "RED"},
        )
        second_line = OrderLine(
            company_id=1,
            order_id=order.id,
            product_id=2,
            validated_product_id=2,
            source_product_external_id="WA-PROD-002",
            quantity=1,
            unit="uds",
            unit_price_received=11.0,
            currency_received="EUR",
            validation_status="validated",
        )
        db.add(second_line)
        db.flush()
        mapper = OdooSalesOrderMapper()
        dto = mapper.map_order(
            order,
            customer=OdooExternalIdentity("ODOO-CUST-001", ODOO_MODEL_CUSTOMER),
            products={
                order.lines[0].id: OdooExternalIdentity("ODOO-PROD-001", ODOO_MODEL_PRODUCT_VARIANT, variant_template_id="ODOO-TMPL-001"),
                second_line.id: OdooExternalIdentity("ODOO-PROD-002", ODOO_MODEL_PRODUCT_VARIANT, variant_template_id="ODOO-TMPL-001"),
            },
            prices={},
            external_reference="ANCHI-1-variants",
        )
        self.assertEqual([line["product_id"] for line in dto.lines], ["ODOO-PROD-001", "ODOO-PROD-002"])
        self.assertEqual({line["product_template_id"] for line in dto.lines}, {"ODOO-TMPL-001"})
        db.close()

    def test_mapper_is_deterministic_and_keeps_source_vs_authoritative_price(self):
        db, order, _delivery = self._new_order_and_delivery(event_suffix="pricing")
        connector = self._connector()
        first = connector._build_dto(db, order).as_payload()
        second = connector._build_dto(db, order).as_payload()
        self.assertEqual(first, second)
        self.assertEqual(first["order_line"][0]["source_price"], 10.0)
        self.assertEqual(first["order_line"][0]["authoritative_price"], 8.7)
        self.assertEqual(first["order_line"][0]["price_unit"], 8.7)
        self.assertEqual(first["state_semantics"], "draft")
        self.assertNotIn("action_confirm", json.dumps(first))
        db.close()

    def test_successful_order_returns_external_id_and_uses_verified_outbox(self):
        db, order, delivery = self._new_order_and_delivery(event_suffix="success")
        result = process_order_delivery(db, delivery, registry=ConnectorRegistry([self._connector()]))
        db.commit()
        self.assertEqual(result.destination.external_order_id, "ODOO-ORDER-000001")
        self.assertEqual(order.destination_external_order_id, "ODOO-ORDER-000001")
        self.assertEqual(delivery.status, "exported")
        db.close()

    def test_missing_customer_does_not_export(self):
        db, order, delivery = self._new_order_and_delivery(event_suffix="missing-customer")
        identity = db.scalar(
            select(ExternalIdentity).where(
                ExternalIdentity.company_id == 1,
                ExternalIdentity.entity_type == "customer",
                ExternalIdentity.external_system == ODOO_EXTERNAL_SYSTEM,
            )
        )
        db.delete(identity)
        db.flush()
        connector = self._connector()
        with self.assertRaises(DeliveryProcessingError) as raised:
            process_order_delivery(db, delivery, registry=ConnectorRegistry([connector]))
        db.commit()
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(connector.client.transport.created_orders, {})
        self.assertEqual(order.export_status, "export_failed")
        db.close()

    def test_missing_product_does_not_export(self):
        db, order, delivery = self._new_order_and_delivery(event_suffix="missing-product")
        identity = db.scalar(
            select(ExternalIdentity).where(
                ExternalIdentity.company_id == 1,
                ExternalIdentity.entity_type == "product",
                ExternalIdentity.external_system == ODOO_EXTERNAL_SYSTEM,
            )
        )
        db.delete(identity)
        db.flush()
        connector = self._connector()
        with self.assertRaises(DeliveryProcessingError):
            process_order_delivery(db, delivery, registry=ConnectorRegistry([connector]))
        db.commit()
        self.assertEqual(connector.client.transport.created_orders, {})
        db.close()

    def test_temporary_failure_reuses_block2_retry_without_connector_retry(self):
        db, order, delivery = self._new_order_and_delivery(event_suffix="temporary")
        transport = MockOdooJSON2Transport(scenario="temporary_failure")
        connector = build_odoo_mock_connector({"tenant_id": 1, "base_url": "https://odoo.invalid", "mock_scenario": "temporary_failure"})
        connector.client.transport = transport
        with self.assertRaises(DeliveryProcessingError) as raised:
            process_order_delivery(db, delivery, registry=ConnectorRegistry([connector]))
        db.commit()
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(delivery.last_error_type, "odoo_unavailable")
        self.assertEqual(len(transport.created_orders), 0)
        transport.set_scenario("success")
        process_order_delivery(db, delivery, registry=ConnectorRegistry([connector]))
        db.commit()
        self.assertEqual(delivery.status, "exported")
        self.assertEqual(delivery.attempt_count, 2)
        db.close()

    def test_permission_and_invalid_payload_are_permanent(self):
        for scenario, expected_type in (("permission_denied", "permission_denied"), ("invalid_payload", "permanent_validation_error")):
            db, _order, delivery = self._new_order_and_delivery(event_suffix=scenario)
            connector = self._connector(scenario)
            with self.assertRaises(DeliveryProcessingError) as raised:
                process_order_delivery(db, delivery, registry=ConnectorRegistry([connector]))
            db.commit()
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(delivery.last_error_type, expected_type)
            db.close()

    def test_remote_success_then_timeout_reconciles_without_duplicate(self):
        db, _order, delivery = self._new_order_and_delivery(event_suffix="timeout-after")
        connector = self._connector("timeout_after_create")
        with self.assertRaises(DeliveryProcessingError) as raised:
            process_order_delivery(db, delivery, registry=ConnectorRegistry([connector]))
        db.commit()
        self.assertTrue(raised.exception.remote_commit_unknown)
        transport = connector.client.transport
        self.assertEqual(len(transport.created_orders), 1)
        process_order_delivery(db, delivery, registry=ConnectorRegistry([connector]))
        db.commit()
        self.assertEqual(delivery.status, "exported")
        self.assertEqual(delivery.destination_external_order_id, "ODOO-ORDER-000001")
        self.assertEqual(len(transport.created_orders), 1)
        db.close()

    def test_tenant_a_sage_and_tenant_b_odoo_are_isolated(self):
        db = self.Session()
        odoo_b = self._seed_tenant(2, include_route=False)
        sage_a = IntegrationConnection(
            company_id=1,
            connector_key="sage_200_es",
            name="Sage principal",
            state="ACTIVE",
            config_json=json.dumps({"site": "SITE-DEMO", "real_http_enabled": False}),
            requires_credentials=True,
        )
        db.add(sage_a)
        db.commit()
        with self.assertRaises(KeyError):
            IntegrationRegistry().resolve(db, company_id=1, connection_id=odoo_b.id)
        runtime = build_runtime_registry(db, company_id=2, connection_ids={odoo_b.id})
        self.assertIsNotNone(runtime.get_for_connection(odoo_b.id, "odoo"))
        self.assertIsNone(runtime.get_for_connection(sage_a.id, "sage_200_es"))
        db.close()

    def test_multiple_destinations_select_only_the_configured_connection(self):
        db = self.Session()
        second = IntegrationConnection(
            company_id=1,
            connector_key="odoo",
            name="Odoo secundario",
            state="ACTIVE",
            config_json=json.dumps({"base_url": "https://odoo.secondary.invalid", "real_http_enabled": False}),
            requires_credentials=True,
        )
        db.add(second)
        db.flush()
        route = db.scalar(select(OrderRoute).where(OrderRoute.company_id == 1, OrderRoute.order_type == "order"))
        route.destination_connection_id = second.id
        db.commit()
        db.close()
        db, _order, delivery = self._new_order_and_delivery(event_suffix="second-destination")
        self.assertEqual(delivery.destination_connection_id, second.id)
        process_order_delivery(db, delivery)
        db.commit()
        self.assertEqual(delivery.destination_external_order_id, "ODOO-ORDER-000001")
        db.close()

    def test_whatsapp_catalog_to_odoo_end_to_end_without_whatsapp_changes(self):
        db = self.Session()
        event = WhatsAppCatalogInboundConnector().to_inbound_order_event(
            {
                "sender": "34600000001",
                "message": {
                    "id": "wamid.odoo.e2e",
                    "from": "34600000001",
                    "type": "order",
                    "order": {
                        "catalog_id": "CATALOG-DEMO",
                        "product_items": [
                            {
                                "product_retailer_id": "WA-PROD-001",
                                "quantity": 2,
                                "item_price": "10.00",
                                "currency": "EUR",
                            }
                        ],
                    },
                },
            },
            company_id=1,
        )
        with patch("app.agent.platform.OrderExtractionAgent.extract", side_effect=AssertionError("Odoo structured path must not use LLM")) as extract:
            result = process_inbound_order_event(db, event, route=True)
        self.assertIsNotNone(result.delivery)
        extract.assert_not_called()
        process_order_delivery(db, result.delivery)
        db.commit()
        self.assertEqual(result.delivery.destination_external_order_id, "ODOO-ORDER-000001")
        self.assertEqual(result.order.export_status, "exported")
        db.close()

    def test_contract_markers_and_dynamic_doc_requirement_are_explicit(self):
        connector = self._connector()
        self.assertEqual(connector.identifier, "odoo")
        self.assertEqual(UNVERIFIED_ODOO_CONTRACT, "UNVERIFIED_ODOO_CONTRACT")
        self.assertEqual(ODOO_SOURCE_DOCUMENTATION, "https://www.odoo.com/documentation/19.0/developer/reference/external_api.html")
        db, order, _delivery = self._new_order_and_delivery(event_suffix="contract")
        payload = connector._build_dto(db, order).as_payload()
        self.assertEqual(payload["contract_status"], UNVERIFIED_ODOO_CONTRACT)
        self.assertEqual(payload["source_documentation"], ODOO_SOURCE_DOCUMENTATION)
        self.assertEqual(ODOO_IDEMPOTENCY_FIELD, "client_order_ref")
        self.assertEqual(ODOO_MODEL_SALES_ORDER, "sale.order")
        db.close()


if __name__ == "__main__":
    unittest.main()
