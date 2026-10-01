from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.registry import IntegrationRegistry  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import AuditLog, Company, Customer, ExternalIdentity, IntegrationConnection, OrderRoute, Product, TenantIntegrationRole  # noqa: E402
from app.integrations.onboarding import (  # noqa: E402
    activate_onboarding,
    execute_onboarding_dry_run,
    pause_onboarding,
    readiness,
    save_onboarding_mapping,
    save_onboarding_role,
    save_onboarding_route,
    start_onboarding,
    test_onboarding_connection,
)
from app.integrations.readiness import IntegrationReadinessError, IntegrationReadinessService  # noqa: E402
from app.integrations.service import IntegrationConfigurationError, create_connection  # noqa: E402


class OnboardingServiceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.tempdir.name, 'tenant.sqlite')}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, expire_on_commit=False)
        self.db = self.Session()
        self.db.add(Company(id=1, name="Tenant A"))
        self.db.commit()
        self.registry = IntegrationRegistry()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.tempdir.cleanup()

    def _connection(self, connector_key: str, *, company_id: int = 1, state: str = "CONFIGURED", config: dict | None = None, credentials: bool = False, name: str | None = None) -> IntegrationConnection:
        connection = create_connection(
            self.db,
            company_id=company_id,
            connector_key=connector_key,
            name=name or connector_key,
            config=config or ({"scenario": "success"} if connector_key == "fake_erp" else {}),
            secret_provider_ref=f"secret://tenant-{company_id}/connection-999/api_key" if credentials else None,
            credentials_configured=credentials,
            registry=self.registry,
        )
        connection.state = state
        self.db.flush()
        return connection

    def _basic_fake_setup(self):
        source = self._connection("fake_inbound", name="WhatsApp ventas")
        destination = self._connection("fake_erp", name="ERP mock")
        save_onboarding_role(self.db, company_id=1, role_key="order_destination", connection_id=destination.id, registry=self.registry)
        save_onboarding_route(self.db, company_id=1, source_connection_id=source.id, destination_connection_id=destination.id, registry=self.registry)
        self.db.commit()
        return source, destination

    def _generic_config(self):
        return {
            "base_url": "https://mock.example/api",
            "operations": {
                "orders.create": {
                    "method": "POST",
                    "path": "/orders",
                    "request_mapping": {
                        "externalReference": "order.external_reference",
                        "customerCode": "order.customer.external_id",
                        "lines": {"source": "order.lines", "items": {"sku": "line.product.external_id", "quantity": {"source": "line.quantity", "type": "number"}}},
                    },
                    "response_mapping": {"external_order_id": "orderNumber"},
                    "idempotency": {"location": "header", "name": "Idempotency-Key"},
                }
            },
            "auth": {"type": "bearer"},
        }

    def test_empty_readiness_is_not_started_and_blocked(self):
        result = readiness(self.db, company_id=1, registry=self.registry)
        self.assertEqual(result["status"], "NOT_STARTED")
        self.assertIn("source_missing", {item["code"] for item in result["blocking_issues"]})

    def test_start_onboarding_is_audited_without_persisted_wizard(self):
        result = start_onboarding(self.db, company_id=1, registry=self.registry)
        self.db.commit()
        self.assertEqual(result["status"], "NOT_STARTED")
        self.assertEqual(self.db.scalar(select(AuditLog).where(AuditLog.action == "onboarding.started")).action, "onboarding.started")

    def test_source_and_destination_are_derived_from_route(self):
        source, destination = self._basic_fake_setup()
        result = readiness(self.db, company_id=1, registry=self.registry)
        self.assertEqual(result["source"]["id"], source.id)
        self.assertEqual(result["destination"]["id"], destination.id)
        self.assertTrue(result["route_valid"])

    def test_credentials_missing_is_blocker(self):
        source = self._connection("fake_inbound")
        destination = self._connection("generic_rest", config=self._generic_config(), credentials=False)
        save_onboarding_route(self.db, company_id=1, source_connection_id=source.id, destination_connection_id=destination.id, registry=self.registry)
        self.db.commit()
        codes = {item["code"] for item in readiness(self.db, company_id=1, registry=self.registry)["blocking_issues"]}
        self.assertIn("credentials_missing", codes)

    def test_invalid_role_capability_is_blocker(self):
        source, destination = self._basic_fake_setup()
        role = self.db.scalar(select(TenantIntegrationRole).where(TenantIntegrationRole.role_key == "order_destination"))
        role.connection_id = source.id
        self.db.commit()
        codes = {item["code"] for item in readiness(self.db, company_id=1, registry=self.registry)["blocking_issues"]}
        self.assertIn("role_capability_missing", codes)

    def test_fake_scenario_reaches_ready_for_activation_and_active(self):
        source, destination = self._basic_fake_setup()
        execute_onboarding_dry_run(self.db, company_id=1, connection_id=destination.id, registry=self.registry)
        test_onboarding_connection(self.db, company_id=1, connection_id=destination.id, registry=self.registry)
        self.db.flush()
        before = readiness(self.db, company_id=1, registry=self.registry)
        self.assertEqual(before["status"], "READY_FOR_ACTIVATION")
        after = activate_onboarding(self.db, company_id=1, acknowledge=True, registry=self.registry)
        self.db.commit()
        self.assertEqual(after["status"], "ACTIVE")
        self.assertEqual(self.db.get(IntegrationConnection, source.id).state, "ACTIVE")
        self.assertTrue(self.db.scalar(select(OrderRoute).where(OrderRoute.company_id == 1)).is_active)

    def test_activation_requires_explicit_acknowledgement(self):
        with self.assertRaises(IntegrationReadinessError):
            activate_onboarding(self.db, company_id=1, acknowledge=False, registry=self.registry)

    def test_blocked_readiness_cannot_activate(self):
        with self.assertRaises(IntegrationReadinessError) as raised:
            activate_onboarding(self.db, company_id=1, acknowledge=True, registry=self.registry)
        self.assertTrue(raised.exception.blockers)

    def test_activation_is_atomic_when_one_connection_fails(self):
        source, destination = self._basic_fake_setup()
        execute_onboarding_dry_run(self.db, company_id=1, connection_id=destination.id, registry=self.registry)
        test_onboarding_connection(self.db, company_id=1, connection_id=destination.id, registry=self.registry)
        self.db.commit()
        from app.integrations import onboarding as onboarding_module

        real_activate = onboarding_module.activate_connection

        def activate_source_then_fail(db, *, company_id, connection_id, registry=None):
            if connection_id == source.id:
                return real_activate(db, company_id=company_id, connection_id=connection_id, registry=registry)
            raise IntegrationConfigurationError("Fallo sintético del segundo activation.")

        with patch.object(onboarding_module, "activate_connection", side_effect=activate_source_then_fail):
            with self.assertRaises(IntegrationConfigurationError):
                activate_onboarding(self.db, company_id=1, acknowledge=True, registry=self.registry)
        self.assertEqual(self.db.get(IntegrationConnection, source.id).state, "CONFIGURED")

    def test_pause_preserves_configuration_and_can_resume(self):
        source, destination = self._basic_fake_setup()
        execute_onboarding_dry_run(self.db, company_id=1, connection_id=destination.id, registry=self.registry)
        test_onboarding_connection(self.db, company_id=1, connection_id=destination.id, registry=self.registry)
        activate_onboarding(self.db, company_id=1, acknowledge=True, registry=self.registry)
        self.db.commit()
        paused = pause_onboarding(self.db, company_id=1, registry=self.registry)
        self.db.commit()
        self.assertEqual(paused["status"], "READY_FOR_ACTIVATION")
        self.assertEqual(self.db.get(IntegrationConnection, destination.id).state, "DISABLED")
        resumed = activate_onboarding(self.db, company_id=1, acknowledge=True, registry=self.registry)
        self.assertEqual(resumed["status"], "ACTIVE")
        self.assertEqual(self.db.get(IntegrationConnection, source.id).state, "ACTIVE")

    def test_generic_mock_can_be_ready_with_identity_mappings(self):
        source = self._connection("fake_inbound")
        customer = Customer(id=1, company_id=1, code="C1", fiscal_name="Synthetic")
        product = Product(id=1, company_id=1, reference="P1", name="Synthetic")
        self.db.add_all([customer, product])
        self.db.flush()
        destination = self._connection("generic_rest", config=self._generic_config(), credentials=True, name="Generic mock")
        save_onboarding_mapping(self.db, company_id=1, connection_id=destination.id, entity_type="customer", entity_id=customer.id, external_id="EXT-C1", registry=self.registry)
        save_onboarding_mapping(self.db, company_id=1, connection_id=destination.id, entity_type="product", entity_id=product.id, external_id="EXT-P1", registry=self.registry)
        save_onboarding_role(self.db, company_id=1, role_key="order_destination", connection_id=destination.id, registry=self.registry)
        save_onboarding_route(self.db, company_id=1, source_connection_id=source.id, destination_connection_id=destination.id, registry=self.registry)
        execute_onboarding_dry_run(self.db, company_id=1, connection_id=destination.id, registry=self.registry)
        test_onboarding_connection(self.db, company_id=1, connection_id=destination.id, registry=self.registry)
        self.db.commit()
        result = readiness(self.db, company_id=1, registry=self.registry)
        self.assertEqual(result["status"], "READY_FOR_ACTIVATION")
        self.assertFalse(result["contract_verified"])
        self.assertTrue(result["warnings"])

    def test_generic_unresolved_identity_blocks_activation(self):
        source = self._connection("fake_inbound")
        customer = Customer(id=1, company_id=1, code="C1", fiscal_name="Synthetic")
        self.db.add(customer)
        self.db.flush()
        destination = self._connection("generic_rest", config=self._generic_config(), credentials=True)
        save_onboarding_role(self.db, company_id=1, role_key="order_destination", connection_id=destination.id, registry=self.registry)
        save_onboarding_route(self.db, company_id=1, source_connection_id=source.id, destination_connection_id=destination.id, registry=self.registry)
        self.db.commit()
        codes = {item["code"] for item in readiness(self.db, company_id=1, registry=self.registry)["blocking_issues"]}
        self.assertIn("identity_unresolved", codes)

    def test_sage_and_odoo_are_configurable_but_contract_blocked(self):
        source = self._connection("fake_inbound")
        for key, config in (("sage_200_es", {"site": "TEST", "base_url": "https://sage.example"}), ("odoo", {"base_url": "https://odoo.example"})):
            destination = self._connection(key, config=config, credentials=True, name=key)
            save_onboarding_role(self.db, company_id=1, role_key="order_destination", connection_id=destination.id, registry=self.registry)
            save_onboarding_route(self.db, company_id=1, source_connection_id=source.id, destination_connection_id=destination.id, registry=self.registry)
            self.db.commit()
            result = readiness(self.db, company_id=1, registry=self.registry)
            self.assertIn("contract_unverified", {item["code"] for item in result["blocking_issues"]})
            self.db.delete(destination)
            self.db.query(TenantIntegrationRole).filter(TenantIntegrationRole.company_id == 1).delete()
            self.db.query(OrderRoute).filter(OrderRoute.company_id == 1).delete()
            self.db.commit()

    def test_mapping_is_tenant_scoped(self):
        destination = self._connection("generic_rest", config=self._generic_config(), credentials=True)
        self.db.add(Company(id=2, name="Tenant B"))
        self.db.commit()
        with self.assertRaises(Exception):
            save_onboarding_mapping(self.db, company_id=2, connection_id=destination.id, entity_type="customer", entity_id=1, external_id="LEAK", registry=self.registry)

    def test_warnings_do_not_block_optional_roles(self):
        source, destination = self._basic_fake_setup()
        execute_onboarding_dry_run(self.db, company_id=1, connection_id=destination.id, registry=self.registry)
        test_onboarding_connection(self.db, company_id=1, connection_id=destination.id, registry=self.registry)
        result = readiness(self.db, company_id=1, registry=self.registry)
        self.assertEqual(result["status"], "READY_FOR_ACTIVATION")
        self.assertTrue(result["warnings"])


if __name__ == "__main__":
    unittest.main()
