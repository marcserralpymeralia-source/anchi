from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.contracts import ConnectorCapability, ConnectorRegistry  # noqa: E402
from app.connectors.identity import bind_external_identity  # noqa: E402
from app.connectors.sage200es import (  # noqa: E402
    MockSage200ESTransport,
    Sage200ESConfig,
    Sage200ESConnector,
    Sage200ESIdentity,
    Sage200ESPriceResolution,
    Sage200ESSalesOrderMapper,
    SAGE200ES_EXTERNAL_SYSTEM,
    UNVERIFIED_SAGE_CONTRACT,
)
from app.connectors.whatsapp_catalog import WhatsAppCatalogInboundConnector  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import Company, Customer, ExternalIdentity, Order, OrderRoute, Product  # noqa: E402
from app.orders.delivery import DeliveryProcessingError, process_order_delivery  # noqa: E402
from app.orders.modular import process_inbound_order_event  # noqa: E402


class Sage200ESConnectorTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            f"sqlite:///{(Path(self.tempdir.name) / 'tenant.sqlite').as_posix()}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        fixture = json.loads((Path(__file__).parent / "fixtures" / "sage200es_mock_contract.json").read_text())
        self.transport = MockSage200ESTransport(
            sites=fixture["sites"],
            customers=fixture["customers"],
            products=fixture["products"],
            rates=fixture["rates"],
            price_offers=fixture["price_offers"],
        )
        self.config = Sage200ESConfig(tenant_id=1, site="SITE-DEMO", base_url="https://mock.invalid")
        self.connector = Sage200ESConnector(config=self.config, transport=self.transport)
        self._seed()

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def _seed(self):
        db = self.Session()
        company = Company(id=1, name="Tenant Sage PoC")
        customer = Customer(id=1, company_id=1, code="C001", fiscal_name="Cliente Sage PoC", status="active")
        product = Product(id=1, company_id=1, reference="ART001", name="Producto Sage PoC", status="active")
        db.add_all([company, customer, product])
        db.flush()
        bind_external_identity(
            db,
            company_id=1,
            entity_type="customer",
            entity_id=customer.id,
            external_system=SAGE200ES_EXTERNAL_SYSTEM,
            external_id="SAGE-CUST-001",
            external_subtype="internal_id",
            metadata={"reference": "C001"},
        )
        bind_external_identity(
            db,
            company_id=1,
            entity_type="product",
            entity_id=product.id,
            external_system=SAGE200ES_EXTERNAL_SYSTEM,
            external_id="SAGE-PROD-001",
            external_subtype="internal_id",
            metadata={"reference": "ART001"},
        )
        bind_external_identity(
            db,
            company_id=1,
            entity_type="customer",
            entity_id=customer.id,
            external_system="meta_whatsapp",
            external_id="34600000000",
        )
        bind_external_identity(
            db,
            company_id=1,
            entity_type="product",
            entity_id=product.id,
            external_system="meta_whatsapp_catalog",
            external_id="ART001",
        )
        db.add(OrderRoute(company_id=1, order_type="order", destination_connector_key="sage_200_es"))
        db.commit()
        db.close()

    def _event(self, event_id: str = "wamid.sage.001"):
        return WhatsAppCatalogInboundConnector().to_inbound_order_event(
            {
                "sender": "34600000000",
                "message": {
                    "id": event_id,
                    "from": "34600000000",
                    "type": "order",
                    "order": {
                        "catalog_id": "CATALOG-DEMO",
                        "product_items": [
                            {
                                "product_retailer_id": "ART001",
                                "quantity": 2,
                                "item_price": "12.50",
                                "currency": "EUR",
                            }
                        ],
                    },
                },
            },
            company_id=1,
        )

    def _registry(self) -> ConnectorRegistry:
        return ConnectorRegistry([self.connector])

    def _create_order(self, event_id: str = "wamid.sage.001"):
        db = self.Session()
        with patch("app.agent.platform.OrderExtractionAgent.extract", side_effect=AssertionError("Sage structured flow no usa LLM")) as extract:
            result = process_inbound_order_event(db, self._event(event_id), registry=self._registry())
        db.commit()
        extract.assert_not_called()
        self.assertIsNotNone(result.delivery)
        return db, result.order, result.delivery

    def test_identifier_and_capabilities_are_explicitly_mock_only(self):
        self.assertEqual(self.connector.identifier, "sage_200_es")
        self.assertEqual(self.connector.capabilities, frozenset())
        self.assertEqual(
            self.connector.mock_capabilities,
            frozenset(
                {
                    ConnectorCapability.CUSTOMERS_READ,
                    ConnectorCapability.PRODUCTS_READ,
                    ConnectorCapability.PRICING_READ,
                    ConnectorCapability.ORDERS_CREATE,
                    ConnectorCapability.ORDERS_READ,
                }
            ),
        )

    def test_tenant_site_config_is_isolated_and_redacted(self):
        summary = Sage200ESConfig(
            tenant_id=1,
            site="SITE-DEMO",
            subscription_key="fake-subscription-key",
            authorization="fake-authorization",
            nonce="fake-nonce",
        ).redacted_summary()
        self.assertEqual(summary["tenant_id"], 1)
        self.assertEqual(summary["site"], "SITE-DEMO")
        self.assertTrue(summary["has_subscription_key"])
        self.assertNotIn("fake-subscription-key", summary)
        self.assertNotIn("fake-authorization", summary)

    def test_product_external_identity_resolves_internal_id_and_reference(self):
        db, order, _delivery = self._create_order()
        identity = db.scalar(
            select(ExternalIdentity).where(
                ExternalIdentity.entity_type == "product",
                ExternalIdentity.external_system == SAGE200ES_EXTERNAL_SYSTEM,
            )
        )
        self.assertEqual(identity.external_id, "SAGE-PROD-001")
        self.assertEqual(json.loads(identity.metadata_json)["reference"], "ART001")
        self.assertEqual(self.connector.lookup_product("SAGE-PROD-001")["reference"], "ART001")
        db.close()

    def test_missing_product_mapping_does_not_create_remote_order(self):
        db, order, delivery = self._create_order("wamid.sage.missing-product")
        identity = db.scalar(
            select(ExternalIdentity).where(
                ExternalIdentity.company_id == 1,
                ExternalIdentity.entity_type == "product",
                ExternalIdentity.external_system == SAGE200ES_EXTERNAL_SYSTEM,
            )
        )
        db.delete(identity)
        db.flush()
        with self.assertRaises(DeliveryProcessingError) as raised:
            process_order_delivery(db, delivery, registry=self._registry())
        db.commit()
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(self.transport.created_orders, {})
        self.assertEqual(order.export_status, "export_failed")
        db.close()

    def test_customer_external_identity_resolves_without_phone(self):
        db, order, _delivery = self._create_order("wamid.sage.customer")
        self.assertEqual(order.validated_customer_id, 1)
        self.assertEqual(self.connector.lookup_customer("SAGE-CUST-001")["reference"], "C001")
        db.close()

    def test_missing_customer_mapping_does_not_create_remote_order(self):
        db, order, delivery = self._create_order("wamid.sage.missing-customer")
        identity = db.scalar(
            select(ExternalIdentity).where(
                ExternalIdentity.company_id == 1,
                ExternalIdentity.entity_type == "customer",
                ExternalIdentity.external_system == SAGE200ES_EXTERNAL_SYSTEM,
            )
        )
        db.delete(identity)
        db.flush()
        with self.assertRaises(DeliveryProcessingError) as raised:
            process_order_delivery(db, delivery, registry=self._registry())
        db.commit()
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(self.transport.created_orders, {})
        self.assertEqual(order.export_status, "export_failed")
        db.close()

    def test_source_price_is_not_authoritative_by_default(self):
        resolution = self.transport.resolve_price(
            site="SITE-DEMO",
            customer_internal_id="SAGE-CUST-001",
            product_internal_id="UNKNOWN-PRODUCT",
            source_price=12.5,
            currency="EUR",
        )
        self.assertEqual(resolution.source_price, 12.5)
        self.assertIsNone(resolution.authoritative_price)
        self.assertFalse(resolution.difference)

    def test_pricing_mock_can_return_authoritative_price_and_difference(self):
        resolution = self.transport.resolve_price(
            site="SITE-DEMO",
            customer_internal_id="SAGE-CUST-001",
            product_internal_id="SAGE-PROD-001",
            source_price=12.5,
            currency="EUR",
        )
        self.assertEqual(resolution.authoritative_price, 10.5)
        self.assertEqual(resolution.currency, "EUR")
        self.assertTrue(resolution.difference)

    def test_mapper_is_pure_and_deterministic(self):
        db, order, _delivery = self._create_order("wamid.sage.mapper")
        customer = Sage200ESIdentity("SAGE-CUST-001", "C001", "internal_id")
        product = Sage200ESIdentity("SAGE-PROD-001", "ART001", "internal_id")
        price = Sage200ESPriceResolution(12.5, 10.5, "EUR", "mock_customer_rate")
        mapper = Sage200ESSalesOrderMapper()
        first = mapper.map_order(
            order,
            customer=customer,
            products={order.lines[0].id: product},
            prices={order.lines[0].id: price},
            external_reference="ANCHI-1-1",
            site="SITE-DEMO",
        ).as_payload()
        second = mapper.map_order(
            order,
            customer=customer,
            products={order.lines[0].id: product},
            prices={order.lines[0].id: price},
            external_reference="ANCHI-1-1",
            site="SITE-DEMO",
        ).as_payload()
        self.assertEqual(first, second)
        self.assertEqual(first["contract_status"], UNVERIFIED_SAGE_CONTRACT)
        self.assertEqual(first["lines"][0]["source_price"], 12.5)
        self.assertEqual(first["lines"][0]["authoritative_price"], 10.5)
        db.close()

    def test_external_reference_is_stable(self):
        db, order, _delivery = self._create_order("wamid.sage.reference")
        self.assertEqual(self.connector.external_reference_for_order(order), f"ANCHI-1-{order.id}")
        self.assertEqual(self.connector.external_reference_for_order(order), self.connector.external_reference_for_order(order))
        db.close()

    def test_successful_create_returns_external_order_id_and_exports_e2e(self):
        db, order, delivery = self._create_order()
        result = process_order_delivery(db, delivery, registry=self._registry())
        db.commit()
        self.assertEqual(result.destination.external_order_id, "SAGE-ORDER-000001")
        self.assertEqual(order.destination_external_order_id, "SAGE-ORDER-000001")
        self.assertEqual(order.export_status, "exported")
        self.assertEqual(delivery.status, "exported")
        self.assertEqual(self.transport.last_order_payload["contract_status"], UNVERIFIED_SAGE_CONTRACT)
        db.close()

    def test_429_is_retryable(self):
        db, _order, delivery = self._create_order("wamid.sage.429")
        self.transport.set_scenario("rate_limited")
        with self.assertRaises(DeliveryProcessingError) as raised:
            process_order_delivery(db, delivery, registry=self._registry())
        db.commit()
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(delivery.last_error_type, "rate_limited")
        db.close()

    def test_503_is_retryable(self):
        db, _order, delivery = self._create_order("wamid.sage.503")
        self.transport.set_scenario("unavailable")
        with self.assertRaises(DeliveryProcessingError) as raised:
            process_order_delivery(db, delivery, registry=self._registry())
        db.commit()
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(delivery.last_error_type, "sage_unavailable")
        db.close()

    def test_timeout_before_create_is_retryable(self):
        db, _order, delivery = self._create_order("wamid.sage.timeout-before")
        self.transport.set_scenario("timeout_before_create")
        with self.assertRaises(DeliveryProcessingError) as raised:
            process_order_delivery(db, delivery, registry=self._registry())
        db.commit()
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(delivery.last_error_type, "timeout")
        db.close()

    def test_validation_error_is_permanent(self):
        db, _order, delivery = self._create_order("wamid.sage.validation")
        self.transport.set_scenario("validation_error")
        with self.assertRaises(DeliveryProcessingError) as raised:
            process_order_delivery(db, delivery, registry=self._registry())
        db.commit()
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(delivery.last_error_type, "sage_validation_error")
        db.close()

    def test_remote_create_then_timeout_reconciles_same_order(self):
        db, order, delivery = self._create_order("wamid.sage.timeout-after")
        self.transport.set_scenario("timeout_after_create")
        with self.assertRaises(DeliveryProcessingError) as raised:
            process_order_delivery(db, delivery, registry=self._registry())
        db.commit()
        self.assertTrue(raised.exception.remote_commit_unknown)
        self.assertEqual(len(self.transport.created_orders), 1)
        process_order_delivery(db, delivery, registry=self._registry())
        db.commit()
        self.assertEqual(delivery.status, "exported")
        self.assertEqual(order.destination_external_order_id, "SAGE-ORDER-000001")
        self.assertEqual(len(self.transport.created_orders), 1)
        db.close()

    def test_second_create_with_same_key_does_not_duplicate(self):
        db, order, delivery = self._create_order("wamid.sage.replay")
        first = self.connector.create_order(db, order, idempotency_key=delivery.idempotency_key)
        second = self.connector.create_order(db, order, idempotency_key=delivery.idempotency_key)
        self.assertEqual(first.external_order_id, second.external_order_id)
        self.assertEqual(len(self.transport.created_orders), 1)
        db.close()

    def test_tenant_config_cannot_deliver_other_tenant_order(self):
        db = self.Session()
        other = Order(company_id=2, validated_customer_id=1, status="pedido_validado")
        db.add(other)
        db.flush()
        errors = self.connector.validate_order(db, other)
        self.assertIn("otro tenant", " ".join(errors))
        self.assertEqual(self.transport.created_orders, {})
        db.close()

    def test_real_http_gate_is_disabled(self):
        connector = Sage200ESConnector(
            config=Sage200ESConfig(tenant_id=1, site="SITE-DEMO", real_http_enabled=True),
            transport=self.transport,
        )
        self.assertEqual(connector.health.status.value, "misconfigured")
        self.assertIn("HTTP real", connector.health.message)

    def test_whatsapp_catalog_flow_remains_structured_and_without_llm(self):
        db, order, _delivery = self._create_order("wamid.sage.no-llm")
        self.assertEqual(order.source_channel_key, "whatsapp")
        self.assertEqual(order.source_mode, "structured")
        self.assertEqual(order.source_provider, "meta")
        db.close()

    def test_fixture_is_explicitly_unverified(self):
        self.assertEqual(self.transport.products["SAGE-PROD-001"]["reference"], "ART001")
        self.assertEqual(UNVERIFIED_SAGE_CONTRACT, "UNVERIFIED_SAGE_CONTRACT")

    def test_rates_and_price_offers_are_mock_only(self):
        self.assertEqual(self.connector.client.read_rates()[0]["rate_id"], "RATE-BASE")
        self.assertEqual(self.connector.client.read_price_offers()[0]["authoritative_price"], 10.5)
        self.assertEqual(self.connector.capabilities, frozenset())

    def test_sites_context_is_read_through_transport(self):
        self.assertEqual(self.connector.client.read_sites()[0]["internal_id"], "SITE-DEMO")
        self.assertEqual(self.config.site, "SITE-DEMO")

    def test_inventory_is_not_a_prerequisite_for_order_creation(self):
        self.assertNotIn(ConnectorCapability.INVENTORY_READ, self.connector.mock_capabilities)
        db, order, delivery = self._create_order("wamid.sage.no-inventory")
        result = process_order_delivery(db, delivery, registry=self._registry())
        db.commit()
        self.assertEqual(result.destination.external_order_id, "SAGE-ORDER-000001")
        db.close()

    def test_order_mapping_keeps_site_customer_and_product_identity_separate(self):
        db, order, delivery = self._create_order("wamid.sage.mapping")
        self.connector.create_order(db, order, idempotency_key=delivery.idempotency_key)
        payload = self.transport.last_order_payload
        self.assertEqual(payload["site"], "SITE-DEMO")
        self.assertEqual(payload["customer"]["internal_id"], "SAGE-CUST-001")
        self.assertEqual(payload["lines"][0]["product"]["internal_id"], "SAGE-PROD-001")
        self.assertEqual(payload["anchi_context"]["external_reference_field"], UNVERIFIED_SAGE_CONTRACT)
        db.close()

    def test_reconciliation_lookup_returns_remote_id(self):
        db, order, delivery = self._create_order("wamid.sage.lookup")
        created = self.connector.create_order(db, order, idempotency_key=delivery.idempotency_key)
        self.assertEqual(
            self.connector.find_order_by_idempotency_key(db, idempotency_key=delivery.idempotency_key),
            created.external_order_id,
        )
        db.close()


if __name__ == "__main__":
    unittest.main()
