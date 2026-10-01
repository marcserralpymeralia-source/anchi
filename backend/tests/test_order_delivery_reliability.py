from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.contracts import ConnectorRegistry, ConnectorStatus  # noqa: E402
from app.connectors.fake import FakeERPConnector, FakeInboundConnector  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import BackgroundJob, Company, Customer, Order, OrderDelivery, OrderDeliveryAttempt, OrderRoute, Product  # noqa: E402
from app.jobs.service import claim_next_job, finish_job  # noqa: E402
from app.orders.delivery import (  # noqa: E402
    DeliveryProcessingError,
    enqueue_order_delivery,
    process_order_delivery,
    retry_delay_seconds,
    retry_order_delivery,
)
from app.orders.modular import process_inbound_order_event  # noqa: E402
from app.connectors.identity import bind_external_identity  # noqa: E402
from app.workers.jobs_worker import _process_job  # noqa: E402
from app.orders.routes import retry_failed_delivery  # noqa: E402


class OrderDeliveryReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            f"sqlite:///{(Path(self.tempdir.name) / 'tenant.sqlite').as_posix()}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        self._seed_tenant(1, "C001", "ART001", "Cliente Uno", "Producto Uno", "fake_erp")

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def _seed_tenant(self, company_id, customer_code, product_code, customer_name, product_name, connector_key):
        db = self.Session()
        company = Company(id=company_id, name=f"Tenant {company_id}")
        customer = Customer(id=company_id, company_id=company_id, code=customer_code, fiscal_name=customer_name, status="active")
        product = Product(id=company_id, company_id=company_id, reference=product_code, name=product_name, status="active")
        db.add_all([company, customer, product])
        db.flush()
        bind_external_identity(
            db,
            company_id=company_id,
            entity_type="customer",
            entity_id=customer.id,
            external_system="fake_erp",
            external_id=customer_code,
        )
        bind_external_identity(
            db,
            company_id=company_id,
            entity_type="product",
            entity_id=product.id,
            external_system="fake_erp",
            external_id=product_code,
        )
        db.add(OrderRoute(company_id=company_id, order_type="order", destination_connector_key=connector_key))
        db.commit()
        db.close()

    def _event(self, event_id="evt-reliability-1", company_id=1, customer_id="C001", sku="ART001"):
        return FakeInboundConnector().to_inbound_order_event(
            {
                "event_id": event_id,
                "external_system": "fake_erp",
                "customer_external_id": customer_id,
                "items": [{"sku": sku, "quantity": 2, "unit": "uds"}],
            },
            company_id=company_id,
        )

    def _create_order(self, db, event_id="evt-reliability-1"):
        result = process_inbound_order_event(db, self._event(event_id), route=True)
        delivery = result.delivery
        self.assertIsNotNone(delivery)
        return result.order, delivery

    def test_validated_order_creates_one_outbox_and_one_worker_job(self):
        db = self.Session()
        order, delivery = self._create_order(db)
        duplicate = enqueue_order_delivery(db, order)
        self.assertEqual(delivery.id, duplicate.id)
        self.assertEqual(db.scalar(select(func.count()).select_from(OrderDelivery)), 1)
        self.assertEqual(
            db.scalar(select(func.count()).select_from(BackgroundJob).where(BackgroundJob.job_type == "deliver_order")),
            1,
        )
        self.assertEqual(order.export_status, "ready_to_export")
        db.close()

    def test_worker_success_persists_external_id_and_attempt(self):
        db = self.Session()
        order, delivery = self._create_order(db, "evt-worker-success")
        job = claim_next_job(db, owner="worker-a", company_id=1, job_types={"deliver_order"})
        self.assertIsNotNone(job)
        with patch("app.workers.jobs_worker.default_destination_registry", return_value=ConnectorRegistry([FakeERPConnector()])):
            result = _process_job(db, job)
        finish_job(db, job, result)
        self.assertEqual(order.export_status, "exported")
        self.assertEqual(order.destination_external_order_id, "FAKE-ORDER-000001")
        self.assertEqual(delivery.status, "exported")
        self.assertEqual(delivery.attempt_count, 1)
        self.assertEqual(db.scalar(select(func.count()).select_from(OrderDeliveryAttempt)), 1)
        db.close()

    def test_retryable_timeout_schedules_retry_without_sleep(self):
        db = self.Session()
        order, delivery = self._create_order(db, "evt-timeout-before")
        fake = FakeERPConnector(scenario="timeout_before_create")
        with self.assertRaises(DeliveryProcessingError) as raised:
            process_order_delivery(db, delivery, registry=ConnectorRegistry([fake]))
        db.commit()
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(delivery.status, "retrying")
        self.assertEqual(delivery.last_error_type, "timeout")
        self.assertEqual(order.export_status, "retry_scheduled")
        self.assertIsNotNone(delivery.next_attempt_at)
        fake.set_scenario("success")
        process_order_delivery(db, delivery, registry=ConnectorRegistry([fake]))
        db.commit()
        self.assertEqual(delivery.status, "exported")
        self.assertEqual(delivery.attempt_count, 2)
        db.close()

    def test_backoff_is_exponential_and_capped(self):
        self.assertEqual(retry_delay_seconds(1, base_seconds=5, max_seconds=20), 5)
        self.assertEqual(retry_delay_seconds(2, base_seconds=5, max_seconds=20), 10)
        self.assertEqual(retry_delay_seconds(3, base_seconds=5, max_seconds=20), 20)
        self.assertEqual(retry_delay_seconds(4, base_seconds=5, max_seconds=20), 20)

    def test_permanent_validation_error_does_not_retry(self):
        db = self.Session()
        order, delivery = self._create_order(db, "evt-permanent")
        fake = FakeERPConnector(scenario="permanent_validation_error")
        with self.assertRaises(DeliveryProcessingError) as raised:
            process_order_delivery(db, delivery, registry=ConnectorRegistry([fake]))
        db.commit()
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(delivery.status, "failed")
        self.assertEqual(order.status, "error_exportacion")
        self.assertEqual(order.export_status, "export_failed")
        self.assertEqual(fake.created_orders, {})
        db.close()

    def test_max_retries_moves_delivery_to_terminal_failure(self):
        db = self.Session()
        order, delivery = self._create_order(db, "evt-max-retries")
        delivery.max_attempts = 2
        fake = FakeERPConnector(scenario="temporary_unavailable")
        registry = ConnectorRegistry([fake])
        with self.assertRaises(DeliveryProcessingError):
            process_order_delivery(db, delivery, registry=registry)
        db.commit()
        with self.assertRaises(DeliveryProcessingError) as raised:
            process_order_delivery(db, delivery, registry=registry)
        db.commit()
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(delivery.status, "failed")
        self.assertEqual(order.export_status, "export_failed")
        self.assertEqual(delivery.attempt_count, 2)
        db.close()

    def test_manual_retry_reopens_failed_delivery_without_new_outbox(self):
        db = self.Session()
        order, delivery = self._create_order(db, "evt-manual-retry")
        fake = FakeERPConnector(scenario="temporary_unavailable")
        with self.assertRaises(DeliveryProcessingError):
            process_order_delivery(db, delivery, registry=ConnectorRegistry([fake]))
        db.commit()
        with self.assertRaises(DeliveryProcessingError):
            process_order_delivery(db, delivery, registry=ConnectorRegistry([fake]))
        db.commit()
        fake.set_scenario("success")
        retried = retry_order_delivery(db, company_id=1, delivery_id=delivery.id)
        self.assertEqual(retried.id, delivery.id)
        process_order_delivery(db, delivery, registry=ConnectorRegistry([fake]))
        db.commit()
        self.assertEqual(delivery.status, "exported")
        self.assertEqual(delivery.attempt_count, 3)
        self.assertEqual(db.scalar(select(func.count()).select_from(OrderDelivery)), 1)
        db.close()

    def test_manual_retry_route_is_tenant_scoped_and_returns_delivery_identity(self):
        db = self.Session()
        _order, delivery = self._create_order(db, "evt-manual-route")
        delivery.max_attempts = 1
        fake = FakeERPConnector(scenario="temporary_unavailable")
        with self.assertRaises(DeliveryProcessingError):
            process_order_delivery(db, delivery, registry=ConnectorRegistry([fake]))
        db.commit()

        response = retry_failed_delivery(
            delivery.id,
            SimpleNamespace(headers={"accept": "application/json"}),
            reason="Destino recuperado en prueba de operación",
            db=db,
            user=SimpleNamespace(id=7, company_id=1, role=SimpleNamespace(name="Supervisor")),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.body.decode("utf-8")
        self.assertIn('"delivery_id":', payload)
        self.assertIn('"status":"queued"', payload)
        self.assertEqual(db.scalar(select(func.count()).select_from(OrderDelivery)), 1)
        self.assertEqual(delivery.status, "queued")
        db.close()

    def test_remote_success_then_local_timeout_reconciles_without_duplicate(self):
        db = self.Session()
        order, delivery = self._create_order(db, "evt-remote-success-timeout")
        fake = FakeERPConnector(scenario="timeout_after_create")
        registry = ConnectorRegistry([fake])
        with self.assertRaises(DeliveryProcessingError) as raised:
            process_order_delivery(db, delivery, registry=registry)
        db.commit()
        self.assertTrue(raised.exception.remote_commit_unknown)
        self.assertEqual(len(fake.created_orders), 1)
        process_order_delivery(db, delivery, registry=registry)
        db.commit()
        self.assertEqual(delivery.status, "exported")
        self.assertTrue(db.scalar(select(OrderDeliveryAttempt).where(OrderDeliveryAttempt.attempt_number == 2)).status == "reconciled")
        self.assertEqual(len(fake.created_orders), 1)
        db.close()

    def test_health_failure_is_retryable_and_recovery_is_deterministic(self):
        db = self.Session()
        order, delivery = self._create_order(db, "evt-health")
        fake = FakeERPConnector()
        fake.set_health(ConnectorStatus.UNAVAILABLE, "caído para test")
        with self.assertRaises(DeliveryProcessingError):
            process_order_delivery(db, delivery, registry=ConnectorRegistry([fake]))
        db.commit()
        self.assertEqual(delivery.status, "retrying")
        fake.set_health(ConnectorStatus.HEALTHY)
        process_order_delivery(db, delivery, registry=ConnectorRegistry([fake]))
        db.commit()
        self.assertEqual(order.export_status, "exported")
        db.close()

    def test_rate_limited_is_retryable_and_misconfigured_is_terminal(self):
        db = self.Session()
        _order, rate_limited_delivery = self._create_order(db, "evt-rate-limited")
        rate_limited = FakeERPConnector(scenario="rate_limited")
        with self.assertRaises(DeliveryProcessingError) as raised:
            process_order_delivery(db, rate_limited_delivery, registry=ConnectorRegistry([rate_limited]))
        db.commit()
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(rate_limited_delivery.last_error_type, "rate_limited")

        _order, misconfigured_delivery = self._create_order(db, "evt-misconfigured")
        misconfigured = FakeERPConnector()
        misconfigured.set_health(ConnectorStatus.MISCONFIGURED, "configuración inválida")
        with self.assertRaises(DeliveryProcessingError) as raised:
            process_order_delivery(db, misconfigured_delivery, registry=ConnectorRegistry([misconfigured]))
        db.commit()
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(misconfigured_delivery.status, "failed")
        db.close()

    def test_two_workers_claim_one_delivery_job(self):
        db_one = self.Session()
        _order, _delivery = self._create_order(db_one, "evt-concurrency")
        db_two = self.Session()
        first = claim_next_job(db_one, owner="worker-one", company_id=1, job_types={"deliver_order"})
        second = claim_next_job(db_two, owner="worker-two", company_id=1, job_types={"deliver_order"})
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        db_one.close()
        db_two.close()

    def test_tenant_cannot_consume_another_tenant_delivery(self):
        db = self.Session()
        _order, delivery = self._create_order(db, "evt-tenant-scope")
        self._seed_tenant(2, "C002", "ART002", "Cliente Dos", "Producto Dos", "fake_erp")
        from app.orders.delivery import get_order_delivery

        self.assertIsNone(get_order_delivery(db, company_id=2, delivery_id=delivery.id))
        self.assertIsNone(claim_next_job(db, owner="tenant-two-worker", company_id=2, job_types={"deliver_order"}))
        db.close()

    def test_connector_a_failure_does_not_block_connector_b(self):
        db = self.Session()
        self._seed_tenant(2, "C002", "ART002", "Cliente Dos", "Producto Dos", "fake_erp_b")
        order_a, delivery_a = self._create_order(db, "evt-connector-a")
        order_b_result = process_inbound_order_event(db, self._event("evt-connector-b", company_id=2, customer_id="C002", sku="ART002"))
        order_b = order_b_result.order
        delivery_b = order_b_result.delivery
        fake_a = FakeERPConnector(identifier="fake_erp", scenario="temporary_unavailable")
        fake_b = FakeERPConnector(identifier="fake_erp_b")
        registry = ConnectorRegistry([fake_a, fake_b])
        with self.assertRaises(DeliveryProcessingError):
            process_order_delivery(db, delivery_a, registry=registry)
        process_order_delivery(db, delivery_b, registry=registry)
        db.commit()
        self.assertEqual(order_a.export_status, "retry_scheduled")
        self.assertEqual(order_b.export_status, "exported")
        db.close()


if __name__ == "__main__":
    unittest.main()
