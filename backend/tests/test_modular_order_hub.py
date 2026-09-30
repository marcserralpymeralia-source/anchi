from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.contracts import ConnectorRegistry  # noqa: E402
from app.connectors.fake import FakeERPConnector, FakeInboundConnector  # noqa: E402
from app.connectors.identity import ExternalIdentityConflict, bind_external_identity, resolve_customer_by_external_identity  # noqa: E402
from app.connectors.routing import route_order  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import Company, Customer, ExternalIdentity, InboundMessage, Order, OrderLine, OrderRoute, Product  # noqa: E402
from app.migrations.registry import _apply_tenant_modular_order_hub  # noqa: E402
from app.orders.modular import process_inbound_order_event  # noqa: E402


class ModularOrderHubTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            f"sqlite:///{(Path(self.tempdir.name) / 'tenant.sqlite').as_posix()}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        self._seed()

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def _seed(self):
        db = self.Session()
        company = Company(id=1, name="Demo")
        customer = Customer(id=1, company_id=1, code="C001", fiscal_name="Cliente Fake", status="active")
        product = Product(id=1, company_id=1, reference="ART001", name="Artículo Fake", status="active")
        db.add_all([company, customer, product])
        db.flush()
        bind_external_identity(
            db,
            company_id=1,
            entity_type="customer",
            entity_id=customer.id,
            external_system="fake_erp",
            external_id="C001",
        )
        bind_external_identity(
            db,
            company_id=1,
            entity_type="product",
            entity_id=product.id,
            external_system="fake_erp",
            external_id="ART001",
        )
        db.add(OrderRoute(company_id=1, order_type="order", destination_connector_key="fake_erp"))
        db.commit()
        db.close()

    def _event(self, *, event_id: str = "evt-001", customer_id: str = "C001", sku: str = "ART001"):
        return FakeInboundConnector().to_inbound_order_event(
            {
                "event_id": event_id,
                "external_system": "fake_erp",
                "customer_external_id": customer_id,
                "items": [{"sku": sku, "quantity": 3, "unit": "uds", "unit_price": 12.5, "currency": "EUR"}],
            },
            company_id=1,
        )

    def test_structured_fake_input_creates_existing_order_and_routes_to_fake_erp(self):
        db = self.Session()
        registry = ConnectorRegistry([FakeERPConnector()])

        with patch("app.agent.platform.OrderExtractionAgent.extract", side_effect=AssertionError("structured no debe usar agente")) as agent_extract:
            result = process_inbound_order_event(db, self._event(), registry=registry)

        db.commit()
        self.assertEqual(result.order.source_mode, "structured")
        self.assertEqual(result.order.validation_status, "validated")
        self.assertEqual(result.order.status, "pedido_exportado")
        self.assertEqual(result.order.destination_external_order_id, "FAKE-ORDER-000001")
        self.assertEqual(result.order.export_status, "exported")
        self.assertEqual(result.order.lines[0].validated_product_id, 1)
        self.assertEqual(result.order.lines[0].unit_price_received, 12.5)
        agent_extract.assert_not_called()
        db.close()

    def test_unknown_customer_is_controlled_review(self):
        db = self.Session()
        result = process_inbound_order_event(db, self._event(customer_id="UNKNOWN"), registry=ConnectorRegistry([FakeERPConnector()]))
        db.commit()
        self.assertEqual(result.order.status, "pending_review")
        self.assertEqual(result.order.validation_status, "needs_review")
        self.assertEqual(result.order.review_status, "required")
        self.assertIn("Cliente externo no resuelto", result.order.review_reasons)
        self.assertIsNone(result.routing)
        db.close()

    def test_unknown_product_is_controlled_review(self):
        db = self.Session()
        result = process_inbound_order_event(db, self._event(sku="UNKNOWN"), registry=ConnectorRegistry([FakeERPConnector()]))
        db.commit()
        self.assertEqual(result.order.status, "pending_review")
        self.assertEqual(result.order.lines[0].validation_status, "pending")
        self.assertIn("Producto externo no resuelto", result.order.review_reasons)
        self.assertIsNone(result.routing)
        db.close()

    def test_external_identity_is_tenant_scoped(self):
        db = self.Session()
        company_two = Company(id=2, name="Otro tenant")
        customer_two = Customer(id=2, company_id=2, code="C001", fiscal_name="Cliente Dos", status="active")
        db.add_all([company_two, customer_two])
        db.flush()
        bind_external_identity(
            db,
            company_id=2,
            entity_type="customer",
            entity_id=customer_two.id,
            external_system="fake_erp",
            external_id="C001",
        )
        self.assertEqual(resolve_customer_by_external_identity(db, company_id=1, external_system="fake_erp", external_id="C001").id, 1)
        self.assertEqual(resolve_customer_by_external_identity(db, company_id=2, external_system="fake_erp", external_id="C001").id, 2)
        self.assertIsNone(resolve_customer_by_external_identity(db, company_id=2, external_system="other_erp", external_id="C001"))
        with self.assertRaises(ExternalIdentityConflict):
            bind_external_identity(
                db,
                company_id=1,
                entity_type="customer",
                entity_id=customer_two.id,
                external_system="fake_erp",
                external_id="C999",
            )
        db.close()

    def test_duplicate_inbound_event_does_not_create_second_order(self):
        db = self.Session()
        registry = ConnectorRegistry([FakeERPConnector()])
        first = process_inbound_order_event(db, self._event(), registry=registry)
        db.commit()
        second = process_inbound_order_event(db, self._event(), registry=registry)
        db.commit()
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(first.order.id, second.order.id)
        self.assertEqual(db.scalar(select(func.count()).select_from(Order)), 1)
        self.assertEqual(db.scalar(select(func.count()).select_from(ExternalIdentity)), 2)
        self.assertEqual(db.scalar(select(func.count()).select_from(OrderLine)), 1)
        self.assertEqual(db.scalar(select(func.count()).select_from(InboundMessage)), 1)
        db.close()

    def test_second_destination_attempt_is_idempotent(self):
        db = self.Session()
        fake_erp = FakeERPConnector()
        registry = ConnectorRegistry([fake_erp])
        result = process_inbound_order_event(db, self._event(), registry=registry)
        db.commit()
        first_external_id = result.order.destination_external_order_id
        second_routing = route_order(db, result.order, registry)
        db.commit()
        self.assertEqual(second_routing.destination.external_order_id, first_external_id)
        self.assertTrue(second_routing.destination.idempotent_replay)
        self.assertEqual(result.order.export_attempt_count, 1)
        self.assertEqual(fake_erp.created_orders, {"evt-001": "FAKE-ORDER-000001"})
        db.close()


class ModularOrderMigrationTests(unittest.TestCase):
    def test_incremental_migration_adds_modular_order_contract(self):
        tempdir = tempfile.TemporaryDirectory()
        engine = create_engine(f"sqlite:///{(Path(tempdir.name) / 'legacy.sqlite').as_posix()}")
        try:
            with engine.begin() as conn:
                conn.execute(text("CREATE TABLE companies (id INTEGER PRIMARY KEY)"))
                conn.execute(text("CREATE TABLE orders (id INTEGER PRIMARY KEY, company_id INTEGER, status VARCHAR(80))"))
                conn.execute(text("CREATE TABLE order_lines (id INTEGER PRIMARY KEY, company_id INTEGER, order_id INTEGER)"))
            actions = _apply_tenant_modular_order_hub(engine, dry_run=False)
            inspector = inspect(engine)
            order_columns = {item["name"] for item in inspector.get_columns("orders")}
            line_columns = {item["name"] for item in inspector.get_columns("order_lines")}
            self.assertIn("source_external_event_id", order_columns)
            self.assertIn("export_status", order_columns)
            self.assertIn("source_product_external_id", line_columns)
            self.assertIn("external_identities", inspector.get_table_names())
            self.assertIn("order_routes", inspector.get_table_names())
            self.assertTrue(actions)
        finally:
            engine.dispose()
            tempdir.cleanup()


if __name__ == "__main__":
    unittest.main()
