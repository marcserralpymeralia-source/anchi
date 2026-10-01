from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.contracts import ConnectorCapability, ConnectorRegistry  # noqa: E402
from app.connectors.fake import FakeInboundConnector  # noqa: E402
from app.connectors.registry import IntegrationRegistry, IntegrationState  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import Company, Customer, ExternalIdentity, Order, Product  # noqa: E402
from app.integrations.service import (  # noqa: E402
    IntegrationConfigurationError,
    activate_connection,
    create_connection,
    disable_connection,
    health_check_connection,
    set_integration_role,
    set_order_route,
)
from app.migrations.registry import _apply_tenant_integration_registry  # noqa: E402
from app.orders.delivery import DeliveryProcessingError, process_order_delivery  # noqa: E402
from app.orders.modular import process_inbound_order_event  # noqa: E402


class IntegrationRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{(Path(self.tempdir.name) / 'tenant.sqlite').as_posix()}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        db = self.Session()
        db.add_all(
            [
                Company(id=1, name="Tenant A"),
                Company(id=2, name="Tenant B"),
            ]
        )
        db.commit()
        db.close()

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def test_registry_lists_without_instantiating_and_hides_fake_in_production(self):
        registry = IntegrationRegistry()
        self.assertEqual({item.key for item in registry.list_definitions(environment="test")}, {"email", "fake_erp", "fake_inbound", "sage_200_es", "whatsapp_catalog"})
        self.assertNotIn("fake_erp", {item.key for item in registry.list_definitions(environment="production")})
        self.assertIsNone(registry._factories.get("fake_erp"))

    def test_same_tenant_can_have_two_connections_of_same_type_and_resolution_is_lazy(self):
        db = self.Session()
        registry = IntegrationRegistry()
        first = create_connection(db, company_id=1, connector_key="fake_erp", name="ERP A", config={}, registry=registry)
        second = create_connection(db, company_id=1, connector_key="fake_erp", name="ERP B", config={}, registry=registry)
        db.commit()
        activate_connection(db, company_id=1, connection_id=first.id, registry=registry)
        activate_connection(db, company_id=1, connection_id=second.id, registry=registry)
        db.commit()
        self.assertIsNone(registry._factories.get("fake_erp"))
        first_instance = registry.resolve(db, company_id=1, connection_id=first.id)
        second_instance = registry.resolve(db, company_id=1, connection_id=second.id)
        self.assertIsNot(first_instance, second_instance)
        self.assertEqual(first_instance.identifier, "fake_erp")
        db.close()

    def test_required_config_and_secret_boundary_are_enforced_at_activation(self):
        db = self.Session()
        registry = IntegrationRegistry()
        sage = create_connection(db, company_id=1, connector_key="sage_200_es", name="Sage", config={}, registry=registry)
        self.assertEqual(sage.state, IntegrationState.MISCONFIGURED.value)
        with self.assertRaises(IntegrationConfigurationError):
            activate_connection(db, company_id=1, connection_id=sage.id, registry=registry)
        with self.assertRaises(IntegrationConfigurationError):
            create_connection(db, company_id=1, connector_key="fake_erp", name="No secrets", config={"api_key": "raw"}, registry=registry)
        db.close()

    def test_tenant_isolation_and_capability_validation(self):
        db = self.Session()
        registry = IntegrationRegistry()
        fake = create_connection(db, company_id=1, connector_key="fake_erp", name="ERP", config={}, registry=registry)
        activate_connection(db, company_id=1, connection_id=fake.id, registry=registry)
        db.commit()
        with self.assertRaises(KeyError):
            registry.resolve(db, company_id=2, connection_id=fake.id)
        role = set_integration_role(db, company_id=1, role_key="order_destination", connection_id=fake.id, registry=registry)
        self.assertEqual(role.connection_id, fake.id)
        with self.assertRaises(IntegrationConfigurationError) as error:
            set_integration_role(db, company_id=1, role_key="pricing_source", connection_id=fake.id, registry=registry)
        self.assertIn("pricing.read", str(error.exception))
        db.close()

    def test_inbound_source_connection_is_tenant_scoped_and_active(self):
        db = self.Session()
        registry = IntegrationRegistry()
        source = create_connection(db, company_id=2, connector_key="fake_inbound", name="Entrada B", config={}, registry=registry)
        activate_connection(db, company_id=2, connection_id=source.id, registry=registry)
        event = FakeInboundConnector().to_inbound_order_event(
            {"event_id": "evt-cross-tenant", "external_system": "fake_erp", "items": [{"sku": "A", "quantity": 1}]},
            company_id=1,
        )
        event = replace(event, source_connection_id=source.id)
        with self.assertRaisesRegex(ValueError, "no pertenece al tenant"):
            process_inbound_order_event(db, event, route=False)
        db.close()

    def test_connection_route_delivers_to_selected_instance_and_disabled_connection_is_blocked(self):
        db = self.Session()
        registry = IntegrationRegistry()
        customer = Customer(id=1, company_id=1, code="C001", fiscal_name="Cliente", status="active")
        product = Product(id=1, company_id=1, reference="ART001", name="Artículo", status="active")
        db.add_all([customer, product])
        db.flush()
        db.add_all(
            [
                ExternalIdentity(company_id=1, entity_type="customer", entity_id=1, external_system="fake_erp", external_id="C001"),
                ExternalIdentity(company_id=1, entity_type="product", entity_id=1, external_system="fake_erp", external_id="ART001"),
            ]
        )
        first = create_connection(db, company_id=1, connector_key="fake_erp", name="ERP A", config={}, registry=registry)
        second = create_connection(db, company_id=1, connector_key="fake_erp", name="ERP B", config={}, registry=registry)
        activate_connection(db, company_id=1, connection_id=first.id, registry=registry)
        activate_connection(db, company_id=1, connection_id=second.id, registry=registry)
        set_order_route(db, company_id=1, destination_connection_id=second.id, registry=registry)
        event = FakeInboundConnector().to_inbound_order_event(
            {"event_id": "evt-connection", "external_system": "fake_erp", "customer_external_id": "C001", "items": [{"sku": "ART001", "quantity": 2}]},
            company_id=1,
        )
        result = process_inbound_order_event(db, event)
        self.assertEqual(result.delivery.destination_connection_id, second.id)
        delivered = process_order_delivery(db, result.delivery)
        self.assertEqual(delivered.destination.external_order_id, "FAKE-ORDER-000001")
        disable_connection(db, company_id=1, connection_id=second.id)
        db.commit()
        with self.assertRaises(DeliveryProcessingError):
            process_order_delivery(db, result.delivery, registry=ConnectorRegistry([]))
        self.assertEqual(result.delivery.last_error_type, "integration_disabled")
        db.close()

    def test_health_is_on_demand_and_disabled_health_does_not_resolve(self):
        db = self.Session()
        registry = IntegrationRegistry()
        connection = create_connection(db, company_id=1, connector_key="fake_erp", name="ERP", config={}, registry=registry)
        activate_connection(db, company_id=1, connection_id=connection.id, registry=registry)
        health_check_connection(db, company_id=1, connection_id=connection.id, registry=registry)
        self.assertEqual(connection.health_status, "HEALTHY")
        disable_connection(db, company_id=1, connection_id=connection.id)
        health_check_connection(db, company_id=1, connection_id=connection.id, registry=registry)
        self.assertEqual(connection.health_status, "UNKNOWN")
        self.assertIsNone(registry._factories.get("fake_erp"))
        db.close()

    def test_sage_mock_can_be_selected_only_with_mock_capabilities_and_real_http_stays_blocked(self):
        db = self.Session()
        registry = IntegrationRegistry()
        connection = create_connection(
            db,
            company_id=1,
            connector_key="sage_200_es",
            name="Sage mock",
            config={"site": "SITE-DEMO", "real_http_enabled": False},
            registry=registry,
        )
        activate_connection(db, company_id=1, connection_id=connection.id, registry=registry)
        role = set_integration_role(db, company_id=1, role_key="order_destination", connection_id=connection.id, registry=registry)
        self.assertEqual(role.connection_id, connection.id)
        self.assertNotIn(ConnectorCapability.ORDERS_CREATE, registry.definition("sage_200_es").capabilities)
        blocked = create_connection(db, company_id=1, connector_key="sage_200_es", name="HTTP Sage", config={"site": "SITE-DEMO", "real_http_enabled": True}, registry=registry)
        with self.assertRaises(IntegrationConfigurationError):
            activate_connection(db, company_id=1, connection_id=blocked.id, registry=registry)
        db.close()


class IntegrationMigrationTests(unittest.TestCase):
    def test_incremental_migration_adds_connection_tables_and_route_columns(self):
        tempdir = tempfile.TemporaryDirectory()
        engine = create_engine(f"sqlite:///{(Path(tempdir.name) / 'legacy.sqlite').as_posix()}")
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql("CREATE TABLE companies (id INTEGER PRIMARY KEY)")
                conn.exec_driver_sql("CREATE TABLE orders (id INTEGER PRIMARY KEY, company_id INTEGER)")
                conn.exec_driver_sql("CREATE TABLE inbound_messages (id INTEGER PRIMARY KEY, company_id INTEGER)")
                conn.exec_driver_sql("CREATE TABLE order_routes (id INTEGER PRIMARY KEY, company_id INTEGER, destination_connector_key VARCHAR(120))")
                conn.exec_driver_sql("CREATE TABLE order_deliveries (id INTEGER PRIMARY KEY, company_id INTEGER, destination_connector_key VARCHAR(120))")
            actions = _apply_tenant_integration_registry(engine, dry_run=False)
            tables = set(inspect(engine).get_table_names())
            self.assertIn("integration_connections", tables)
            self.assertIn("tenant_integration_roles", tables)
            self.assertIn("destination_connection_id", {column["name"] for column in inspect(engine).get_columns("order_routes")})
            self.assertTrue(actions)
        finally:
            engine.dispose()
            tempdir.cleanup()


if __name__ == "__main__":
    unittest.main()
