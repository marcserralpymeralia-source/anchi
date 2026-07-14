from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import asyncio
import os
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from fastapi.responses import JSONResponse

import sys

os.environ.setdefault("APP_ENV", "development")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.security import hash_password  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import BackgroundJob, Customer, Order, OrderLine, Product  # noqa: E402
from app.jobs.service import cancel_job, claim_next_job, retry_job  # noqa: E402
from app.master.database import MasterBase  # noqa: E402
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser  # noqa: E402
from app.master.service import authenticate_master_user, load_tenant_context  # noqa: E402
from app.tenancy.database import get_tenant_db  # noqa: E402
from app.customers.routes import _soft_delete_customer  # noqa: E402
from app.products.routes import _soft_delete_product  # noqa: E402
from app.orders.routes import _soft_delete_order  # noqa: E402
from app.admin.diagnostics import company_diagnostics  # noqa: E402
from app.core.middleware import branding_middleware  # noqa: E402
from app.core.app_factory import internal_server_error_response  # noqa: E402


class FakeRequest:
    def __init__(self, session: dict | None = None, host: str = "localhost"):
        self.scope = {"session": session or {}}
        self.headers = {"host": host}
        self.state = SimpleNamespace()


class CoreSecurityAndJobsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        self.master_path = base / "master.sqlite"
        self.tenant_path = base / "tenant.sqlite"
        self.master_engine = create_engine(f"sqlite:///{self.master_path.as_posix()}", connect_args={"check_same_thread": False})
        self.tenant_engine = create_engine(f"sqlite:///{self.tenant_path.as_posix()}", connect_args={"check_same_thread": False})
        MasterBase.metadata.create_all(self.master_engine)
        Base.metadata.create_all(self.tenant_engine)
        self.MasterSession = sessionmaker(bind=self.master_engine, autoflush=False, autocommit=False)
        self.TenantSession = sessionmaker(bind=self.tenant_engine, autoflush=False, autocommit=False)

    def tearDown(self):
        self.master_engine.dispose()
        self.tenant_engine.dispose()
        self.tempdir.cleanup()

    def seed_master(self, *, active: bool = True, with_tenant_db: bool = True):
        db = self.MasterSession()
        company = MasterCompany(id=1, name="Demo", slug="demo", active=True)
        user = MasterUser(id=1, email="admin@anchi.local", full_name="Admin Demo", password_hash=hash_password("admin123"), is_active=active)
        membership = CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=True)
        db.add_all([company, user, membership])
        if with_tenant_db:
            db.add(MasterTenantDatabase(company_id=1, database_key="demo", database_url=f"sqlite:///{self.tenant_path.as_posix()}", is_active=True, health_status="ok"))
        db.commit()
        db.close()

    def test_authenticate_master_user_valid_and_invalid(self):
        self.seed_master()
        db = self.MasterSession()
        user = authenticate_master_user(db, "admin@anchi.local", "admin123")
        self.assertIsNotNone(user)
        self.assertEqual(user.company_id, 1)
        self.assertEqual(user.company_slug, "demo")
        self.assertEqual(user.role.name, "Administrador")
        self.assertIsNone(authenticate_master_user(db, "admin@anchi.local", "wrong"))
        self.assertIsNone(authenticate_master_user(db, "missing@example.com", "admin123"))
        db.close()

    def test_load_tenant_context_and_missing_tenant_db(self):
        self.seed_master(with_tenant_db=False)
        db = self.MasterSession()
        request = FakeRequest(session={"membership_id": 1, "user_id": 1, "company_id": 1, "company_slug": "demo"})
        context = load_tenant_context(request, db)
        self.assertIsNotNone(context)
        self.assertEqual(context.company.slug, "demo")
        self.assertIsNone(context.company.database_url)
        self.assertIsNone(context.user.database_url)

        request_missing = FakeRequest(session={"company_id": 1})
        with self.assertRaises(HTTPException) as ctx:
            next(get_tenant_db(request_missing, db))
        self.assertEqual(ctx.exception.status_code, 503)
        db.close()

    def test_get_tenant_db_redirects_to_login_without_session(self):
        self.seed_master(with_tenant_db=False)
        db = self.MasterSession()
        request = FakeRequest(session={})
        with self.assertRaises(HTTPException) as ctx:
            next(get_tenant_db(request, db))
        self.assertEqual(ctx.exception.status_code, 303)
        self.assertEqual(ctx.exception.headers.get("Location"), "/login")
        db.close()

    def test_load_tenant_context_rejects_cross_company_session(self):
        db = self.MasterSession()
        company_a = MasterCompany(id=1, name="Demo A", slug="demo-a", active=True)
        company_b = MasterCompany(id=2, name="Demo B", slug="demo-b", active=True)
        user = MasterUser(id=1, email="admin@anchi.local", full_name="Admin Demo", password_hash=hash_password("admin123"), is_active=True)
        membership_a = CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=True)
        membership_b = CompanyMembership(id=2, user_id=1, company_id=2, role_key="Administrador", is_active=True, is_owner=False)
        db.add_all([company_a, company_b, user, membership_a, membership_b])
        db.commit()

        request = FakeRequest(session={"membership_id": 2, "user_id": 1, "company_id": 1, "company_slug": "demo-a"})
        self.assertIsNone(load_tenant_context(request, db))
        db.close()

    def test_claim_next_job_is_exclusive_and_retryable(self):
        db = self.TenantSession()
        job = BackgroundJob(company_id=1, job_type="process_email", status="queued", payload_json="{}", max_retries=2)
        db.add(job)
        db.commit()

        claimed = claim_next_job(db, owner="worker-a", job_types={"process_email"})
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.status, "running")
        self.assertEqual(claimed.lock_owner, "worker-a")
        self.assertEqual(claimed.attempt_count, 1)

        second_claim = claim_next_job(db, owner="worker-b", job_types={"process_email"})
        self.assertIsNone(second_claim)

        db.refresh(claimed)
        claimed.status = "retrying"
        claimed.lock_until = claimed.lock_until.replace(year=claimed.lock_until.year - 1)
        claimed.next_retry_at = claimed.lock_until
        db.commit()
        reclaimed = claim_next_job(db, owner="worker-c", job_types={"process_email"})
        self.assertIsNotNone(reclaimed)
        self.assertEqual(reclaimed.lock_owner, "worker-c")
        db.close()

    def test_retry_and_cancel_job(self):
        db = self.TenantSession()
        job = BackgroundJob(company_id=1, job_type="import_file", status="failed", payload_json="{}", max_retries=3)
        db.add(job)
        db.commit()

        retried = retry_job(db, 1, job.id)
        self.assertIsNotNone(retried)
        self.assertEqual(retried.status, "queued")
        self.assertEqual(retried.retry_count, 1)

        cancelled = cancel_job(db, 1, job.id)
        self.assertIsNotNone(cancelled)
        self.assertEqual(cancelled.status, "cancelled")
        db.close()

    def test_soft_delete_flags_and_default_filters(self):
        db = self.TenantSession()
        customer = Customer(company_id=1, code="C1", fiscal_name="Cliente Demo", commercial_name="Cliente Demo")
        product = Product(company_id=1, reference="P1", name="Producto Demo", description="Producto Demo", status="active")
        order = Order(company_id=1, status="pedido_pendiente_revision", score=42, customer_detected_name="Cliente Demo")
        db.add_all([customer, product, order])
        db.commit()
        db.refresh(customer)
        db.refresh(product)
        db.refresh(order)

        user = SimpleNamespace(id=99, company_id=1, role=SimpleNamespace(name="Administrador"))
        _soft_delete_customer(db, customer, user)
        _soft_delete_product(db, product, user)
        _soft_delete_order(db, order, user)

        self.assertIsNotNone(customer.deleted_at)
        self.assertEqual(customer.status, "deleted")
        self.assertIsNotNone(product.deleted_at)
        self.assertEqual(product.status, "deleted")
        self.assertIsNotNone(order.deleted_at)
        self.assertEqual(order.status, "deleted")

        active_customers = db.scalars(select(Customer).where(Customer.company_id == 1, Customer.deleted_at.is_(None))).all()
        active_products = db.scalars(select(Product).where(Product.company_id == 1, Product.deleted_at.is_(None))).all()
        active_orders = db.scalars(select(Order).where(Order.company_id == 1, Order.deleted_at.is_(None))).all()
        self.assertEqual(active_customers, [])
        self.assertEqual(active_products, [])
        self.assertEqual(active_orders, [])
        db.close()

    def test_company_diagnostics_reads_tenant_state(self):
        self.seed_master()
        tenant_db = self.TenantSession()
        tenant_db.add_all([
            Customer(company_id=1, code="C1", fiscal_name="Cliente Demo", commercial_name="Cliente Demo"),
            Product(company_id=1, reference="P1", name="Producto Demo", description="Producto Demo", status="active"),
            Order(company_id=1, status="pedido_confirmado", score=91, customer_detected_name="Cliente Demo"),
        ])
        tenant_db.commit()
        tenant_db.close()

        master_db = self.MasterSession()
        data = company_diagnostics(master_db, 1)
        self.assertEqual(data["company_name"], "Demo")
        self.assertEqual(data["tenant_database_status"], "ok")
        self.assertGreaterEqual(data["customers_total"], 1)
        self.assertGreaterEqual(data["products_total"], 1)
        self.assertGreaterEqual(data["orders_total"], 1)
        self.assertIn("schema_report", data)
        master_db.close()

    def test_request_id_is_added_to_responses_and_errors(self):
        request = FakeRequest()

        async def call_next(_request):
            return JSONResponse({"ok": True})

        fake_session = SimpleNamespace(close=lambda: None)
        with patch("app.core.middleware.MasterSessionLocal", return_value=fake_session), patch("app.core.middleware.load_tenant_context", return_value=None):
            response = asyncio.run(branding_middleware(request, call_next))

        request_id = response.headers.get("X-Request-ID")
        self.assertTrue(request_id)

        error_response = internal_server_error_response(request)
        self.assertEqual(error_response.headers.get("X-Request-ID"), request_id)
        self.assertIn(request_id, error_response.body.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
