from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import asyncio
import os
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, select, func
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import sessionmaker
from fastapi.responses import JSONResponse

import sys

os.environ.setdefault("APP_ENV", "development")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.security import hash_password  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import BackgroundJob, Customer, CustomerAlias, Email, EmailSettings, LLMSettings, Order, OrderLine, Product, ProductAlias  # noqa: E402
from app.jobs.service import cancel_job, claim_next_job, retry_job  # noqa: E402
from app.master.database import MasterBase  # noqa: E402
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser  # noqa: E402
from app.master.service import authenticate_master_user, load_tenant_context  # noqa: E402
from app.auth.dependencies import current_user  # noqa: E402
from app.auth.routes import login  # noqa: E402
from app.tenancy.database import get_tenant_db  # noqa: E402
from app.customers.routes import _soft_delete_customer  # noqa: E402
from app.products.routes import _soft_delete_product  # noqa: E402
from app.orders.routes import _soft_delete_order  # noqa: E402
from app.admin.diagnostics import company_diagnostics  # noqa: E402
from app.core.middleware import branding_middleware  # noqa: E402
from app.core.templating import templates  # noqa: E402
from app.core.app_factory import create_app  # noqa: E402
from app.core.app_factory import internal_server_error_response  # noqa: E402
from app.core.app_factory import sqlalchemy_error_response  # noqa: E402
from app.core.encryption import encrypt_secret  # noqa: E402
from app.pages.routes import dashboard  # noqa: E402
from app.settings.branding import branding_to_dict, default_branding_payload  # noqa: E402
from app.dashboard.service import _safe_sender_domain, _safe_sort_timestamp, email_workbench_item, suggest_customer_for_email, workbench_summary  # noqa: E402
from app.master_data.service import normalize_conflict_policy, upsert_customer, upsert_product  # noqa: E402
from app.settings.integrations import classify_integration_error, redact_email_config, validate_imap_config, validate_openai_config, validate_smtp_config  # noqa: E402


class FakeRequest:
    def __init__(self, session: dict | None = None, host: str = "localhost"):
        self.scope = {"session": session or {}}
        self.session = self.scope["session"]
        self.headers = {"host": host}
        self.query_params = {}
        self.state = SimpleNamespace()
        self.url = SimpleNamespace(path="/demo")
        self.method = "GET"


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

    def test_authenticate_master_user_prefers_membership_matching_email_domain(self):
        db = self.MasterSession()
        company_a = MasterCompany(id=1, name="Anchi", slug="anchi", active=True)
        company_b = MasterCompany(id=2, name="Mulet Hidalgo", slug="mulet-hidalgo", active=True)
        user = MasterUser(id=1, email="admin@mulet-hidalgo.local", full_name="Admin", password_hash=hash_password("admin123"), is_active=True)
        membership_a = CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=False)
        membership_b = CompanyMembership(id=2, user_id=1, company_id=2, role_key="Administrador", is_active=True, is_owner=True)
        tenant_a = MasterTenantDatabase(company_id=1, database_key="anchi", database_url=f"sqlite:///{self.tenant_path.as_posix()}", is_active=True, health_status="ok")
        tenant_b = MasterTenantDatabase(company_id=2, database_key="mulet-hidalgo", database_url=f"sqlite:///{self.tenant_path.as_posix()}", is_active=True, health_status="ok")
        db.add_all([company_a, company_b, user, membership_a, membership_b, tenant_a, tenant_b])
        db.commit()

        resolved = authenticate_master_user(db, "admin@mulet-hidalgo.local", "admin123")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.company_slug, "mulet-hidalgo")
        self.assertEqual(resolved.company_id, 2)
        db.close()

    def test_authenticate_master_user_repairs_missing_demo_account(self):
        db = self.MasterSession()
        company = MasterCompany(id=2, name="Mulet Hidalgo", slug="mulet-hidalgo", active=True)
        tenant = MasterTenantDatabase(company_id=2, database_key="mulet-hidalgo", database_url=f"sqlite:///{self.tenant_path.as_posix()}", is_active=True, health_status="ok")
        db.add_all([company, tenant])
        db.commit()

        get_settings.cache_clear()
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "demo",
                "MASTER_DATABASE_URL": "postgresql://user:pass@localhost/demo_master",
                "TENANT_DB_MODE": "external",
                "TENANT_DATABASE_URL": "postgresql://user:pass@localhost/demo_tenant",
            },
            clear=False,
        ):
            resolved = authenticate_master_user(db, "admin@mulet-hidalgo.local", "AnchiDemo2026!")
        get_settings.cache_clear()

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.company_slug, "mulet-hidalgo")
        repaired_user = db.scalar(select(MasterUser).where(MasterUser.email == "admin@mulet-hidalgo.local"))
        self.assertIsNotNone(repaired_user)
        repaired_membership = db.scalar(select(CompanyMembership).where(CompanyMembership.user_id == repaired_user.id, CompanyMembership.company_id == 2))
        self.assertIsNotNone(repaired_membership)
        db.close()

    def test_authenticate_master_user_accepts_demo_password_fallback(self):
        db = self.MasterSession()
        company = MasterCompany(id=1, name="Demo", slug="demo", active=True)
        user = MasterUser(id=1, email="admin@anchi.local", full_name="Admin", password_hash=hash_password("old-password"), is_active=True)
        membership = CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=True)
        tenant = MasterTenantDatabase(company_id=1, database_key="demo", database_url=f"sqlite:///{self.tenant_path.as_posix()}", is_active=True, health_status="ok")
        db.add_all([company, user, membership, tenant])
        db.commit()

        get_settings.cache_clear()
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "demo",
                "MASTER_DATABASE_URL": "postgresql://user:pass@localhost/demo_master",
                "TENANT_DB_MODE": "external",
                "TENANT_DATABASE_URL": "postgresql://user:pass@localhost/demo_tenant",
            },
            clear=False,
        ):
            resolved = authenticate_master_user(db, "admin@anchi.local", "AnchiDemo2026!")
        get_settings.cache_clear()
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.company_slug, "demo")
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
        self.assertEqual(ctx.exception.status_code, 303)
        self.assertEqual(ctx.exception.headers.get("Location"), "/login?next=%2Fdemo")
        db.close()

    def test_get_tenant_db_redirects_to_login_without_session(self):
        self.seed_master(with_tenant_db=False)
        db = self.MasterSession()
        request = FakeRequest(session={})
        with self.assertRaises(HTTPException) as ctx:
            next(get_tenant_db(request, db))
        self.assertEqual(ctx.exception.status_code, 303)
        self.assertEqual(ctx.exception.headers.get("Location"), "/login?next=%2Fdemo")
        db.close()

    def test_auth_dependencies_redirect_when_tenant_lookup_fails(self):
        self.seed_master(with_tenant_db=True)
        db = self.MasterSession()
        request = FakeRequest(session={"membership_id": 1, "user_id": 1, "company_id": 1, "company_slug": "demo"})
        with patch("app.auth.dependencies.load_tenant_context", side_effect=OperationalError("select 1", {}, Exception("boom"))):
            with self.assertRaises(HTTPException) as ctx:
                current_user(request, db)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(request.scope["session"]["membership_id"], 1)
        db.close()

    def test_branding_middleware_falls_back_on_sqlalchemy_schema_error(self):
        self.seed_master(with_tenant_db=True)
        request = FakeRequest(session={"membership_id": 1, "user_id": 1, "company_id": 1, "company_slug": "demo"})
        with patch("app.core.middleware.load_tenant_context", side_effect=ProgrammingError("select * from missing", {}, Exception("boom"))):
            with patch("app.core.middleware.MasterSessionLocal") as master_session_factory:
                fake_db = self.MasterSession()
                master_session_factory.return_value = fake_db
                async def _call_next(_request):
                    return JSONResponse({"ok": True})

                response = asyncio.run(branding_middleware(request, _call_next))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(getattr(request.state, "tenant", None), None)
        fake_db.close()

    def test_get_tenant_db_redirects_when_schema_provisioning_fails(self):
        self.seed_master(with_tenant_db=True)
        db = self.MasterSession()
        request = FakeRequest(session={"membership_id": 1, "user_id": 1, "company_id": 1, "company_slug": "demo"})
        with patch("app.tenancy.database.ensure_tenant_schema", side_effect=OperationalError("select 1", {}, Exception("boom"))):
            with self.assertRaises(HTTPException) as ctx:
                next(get_tenant_db(request, db))
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(request.scope["session"]["membership_id"], 1)
        db.close()

    def test_dashboard_falls_back_when_workbench_summary_fails(self):
        self.seed_master(with_tenant_db=True)
        request = FakeRequest(
            session={
                "membership_id": 1,
                "user_id": 1,
                "company_id": 1,
                "company_slug": "demo",
            }
        )
        request.state.branding = branding_to_dict(default_branding_payload())
        request.state.alert_center = SimpleNamespace(
            total=0,
            has_critical=False,
            high=0,
            medium=0,
            low=0,
            info=0,
        )
        fake_user = SimpleNamespace(
            company_id=1,
            role=SimpleNamespace(name="Administrador"),
        )
        fake_db = SimpleNamespace()

        with patch.dict(
            templates.env.globals,
            {
                "branding_css_vars": lambda payload: "",
                "app_settings": SimpleNamespace(environment="test"),
            },
        ):
            with patch(
                "app.pages.routes.is_setup_operational",
                return_value=True,
            ):
                with patch(
                    "app.pages.routes.workbench_summary",
                    side_effect=OperationalError(
                        "select 1",
                        {},
                        Exception("boom"),
                    ),
                ):
                    response = dashboard(
                        request,
                        db=fake_db,
                        user=fake_user,
                    )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["workbench"]["items"], [])
        self.assertEqual(response.context["pagination"]["total_items"], 0)

    def test_dashboard_normalizes_featured_item_legacy_fields(self):
        self.seed_master(with_tenant_db=True)
        request = FakeRequest(
            session={
                "membership_id": 1,
                "user_id": 1,
                "company_id": 1,
                "company_slug": "demo",
            }
        )
        request.state.branding = branding_to_dict(default_branding_payload())
        request.state.alert_center = SimpleNamespace(
            total=0,
            has_critical=False,
            high=0,
            medium=0,
            low=0,
            info=0,
        )
        fake_user = SimpleNamespace(
            company_id=1,
            role=SimpleNamespace(name="Administrador"),
        )
        fake_db = SimpleNamespace()

        fake_item = {
            "kind": "email",
            "received_at": datetime(
                2026,
                7,
                29,
                12,
                0,
                0,
                tzinfo=timezone.utc,
            ),
            "customer": "Cliente Demo",
            "channel": "Email",
            "score": None,
        }

        fake_workbench = {
            "items": [fake_item],
            "pagination": {
                "page": 1,
                "page_size": 25,
                "total": 1,
                "pages": 1,
            },
            "tab_counts": {
                "all": 1,
                "pending": 0,
                "review": 0,
                "ready": 0,
                "errors": 0,
                "no_order": 0,
            },
        }

        with patch.dict(
            templates.env.globals,
            {
                "branding_css_vars": lambda payload: "",
                "app_settings": SimpleNamespace(environment="test"),
            },
        ):
            with patch(
                "app.pages.routes.is_setup_operational",
                return_value=True,
            ):
                with patch(
                    "app.pages.routes.workbench_summary",
                    return_value=fake_workbench,
                ):
                    response = dashboard(
                        request,
                        db=fake_db,
                        user=fake_user,
                    )

        self.assertEqual(response.status_code, 200)

        featured = response.context["featured_process_item"]

        self.assertEqual(
            featured["date"],
            datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(featured["customer_name"], "Cliente Demo")
        self.assertEqual(featured["origin"], "Email")
        self.assertEqual(featured["score"], 0)
        self.assertEqual(featured["category_label"], "Sin analizar")
        self.assertEqual(featured["subject"], "Pedido compra")
        self.assertEqual(featured["detail_url"], "/")

    def test_safe_sort_timestamp_handles_missing_and_naive_datetimes(self):
        self.assertEqual(_safe_sort_timestamp(None), 0.0)
        self.assertEqual(_safe_sort_timestamp("not-a-datetime"), 0.0)
        naive_value = datetime(2026, 7, 29, 12, 0, 0)
        aware_value = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(_safe_sort_timestamp(naive_value), aware_value.timestamp())

    def test_email_workbench_item_handles_missing_sender(self):
        email = SimpleNamespace(
            id=1,
            sender=None,
            status="pending",
            detected_type=None,
            agent_status=None,
            has_pdf=False,
            body=None,
            has_attachments=False,
            processing_error=None,
            received_at=datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc),
            subject="Prueba",
        )
        item = email_workbench_item(email)
        self.assertEqual(item["sender_domain"], "")
        self.assertEqual(_safe_sender_domain(None), "")
        self.assertEqual(item["from_email"], None)

    def test_suggest_customer_for_email_handles_missing_sender(self):
        db = self.TenantSession()
        self.assertEqual(suggest_customer_for_email(db, 1, SimpleNamespace(sender=None)), "")
        self.assertEqual(suggest_customer_for_email(db, 1, SimpleNamespace(sender="")), "")
        db.close()

    def test_sqlalchemy_error_response_returns_operational_error_for_html_requests(self):
        request = FakeRequest(session={})
        request.headers["accept"] = "text/html"
        request.state.request_id = "req-1"
        request.state.correlation_id = "corr-1"
        response = sqlalchemy_error_response(request, OperationalError("select 1", {}, Exception("boom")))
        self.assertEqual(response.status_code, 503)
        self.assertIsNone(response.headers.get("location"))
        self.assertEqual(response.headers.get("x-request-id"), "req-1")
        self.assertEqual(response.headers.get("x-correlation-id"), "corr-1")

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

    def test_master_data_upserts_reuse_the_same_flow(self):
        self.assertEqual(normalize_conflict_policy("create_update"), "update_existing")
        self.assertEqual(normalize_conflict_policy("create_only"), "create_only")
        db = self.TenantSession()
        customer = upsert_customer(
            db,
            company_id=1,
            data={
                "code": "C001",
                "fiscal_name": "Cliente Uno",
                "primary_email": "uno@example.com",
                "associated_emails": "ventas@uno.example.com",
                "associated_phones": "666111222",
                "domains": "uno.example.com",
                "aliases": "Cliente Uno SL",
            },
            source="manual",
            actor_id=1,
        ).entity
        product = upsert_product(
            db,
            company_id=1,
            data={
                "reference": "P001",
                "name": "Caja Demo",
                "sale_unit": "cajas",
                "aliases": "Caja de prueba",
                "sale_price": "12.5",
            },
            source="manual",
            actor_id=1,
        ).entity
        db.commit()
        db.refresh(customer)
        db.refresh(product)
        self.assertEqual(customer.fiscal_name, "Cliente Uno")
        self.assertEqual(product.reference, "P001")

        updated_customer = upsert_customer(
            db,
            company_id=1,
            data={
                "code": "C001",
                "fiscal_name": "Cliente Uno Actualizado",
                "primary_email": "uno@example.com",
                "domains": "uno.example.com",
                "aliases": "Cliente Uno SL",
                "status": "active",
            },
            source="manual",
            actor_id=1,
            customer_id=customer.id,
        ).entity
        updated_product = upsert_product(
            db,
            company_id=1,
            data={
                "reference": "P001",
                "name": "Caja Demo Actualizada",
                "sale_unit": "cajas",
                "aliases": "Caja de prueba",
                "sale_price": "13.5",
            },
            source="manual",
            actor_id=1,
            product_id=product.id,
        ).entity
        db.commit()
        self.assertEqual(updated_customer.id, customer.id)
        self.assertEqual(updated_customer.fiscal_name, "Cliente Uno Actualizado")
        self.assertEqual(updated_product.id, product.id)
        self.assertEqual(updated_product.name, "Caja Demo Actualizada")
        self.assertEqual(db.scalar(select(func.count()).select_from(CustomerAlias)) or 0, 1)
        self.assertEqual(db.scalar(select(func.count()).select_from(ProductAlias)) or 0, 1)
        db.close()

    def test_integration_validation_and_redaction_helpers(self):
        db = self.TenantSession()
        db.add(EmailSettings(company_id=1, imap_host="imap.example.com", imap_username="demo@example.com", imap_password_encrypted=encrypt_secret("secret"), smtp_host="smtp.example.com", smtp_username="demo@example.com", smtp_password_encrypted=encrypt_secret("secret"), from_email="demo@example.com"))
        db.add(LLMSettings(company_id=1, provider="openai", api_key_encrypted=encrypt_secret("api-key")))
        db.commit()
        settings = db.get(EmailSettings, 1)
        llm = db.get(LLMSettings, 1)
        self.assertTrue(validate_imap_config(settings)["ok"])
        self.assertTrue(validate_smtp_config(settings)["ok"])
        self.assertTrue(validate_openai_config(llm)["ok"])
        self.assertEqual(classify_integration_error("timeout connecting to smtp"), "timeout")
        self.assertEqual(classify_integration_error("permission denied"), "permission_denied")
        self.assertEqual(redact_email_config(settings)["imap_password"], "••••••••")
        db.close()

    def test_request_id_is_added_to_responses_and_errors(self):
        request = FakeRequest()

        async def call_next(_request):
            return JSONResponse({"ok": True})

        fake_session = SimpleNamespace(close=lambda: None)
        with patch("app.core.middleware.MasterSessionLocal", return_value=fake_session), patch("app.core.middleware.load_tenant_context", return_value=None):
            response = asyncio.run(branding_middleware(request, call_next))

        request_id = response.headers.get("X-Request-ID")
        self.assertTrue(request_id)
        self.assertEqual(response.headers.get("X-Correlation-ID"), request_id)

        error_response = internal_server_error_response(request)
        self.assertEqual(error_response.headers.get("X-Request-ID"), request_id)
        self.assertEqual(error_response.headers.get("X-Correlation-ID"), request_id)
        self.assertIn(request_id, error_response.body.decode("utf-8"))

    def test_app_still_serves_login_when_master_db_is_unavailable(self):
        op_error = OperationalError("INIT", {}, Exception("boom"))

        with patch("app.core.lifespan.init_master_db", side_effect=op_error), patch(
            "app.core.middleware.MasterSessionLocal",
            side_effect=op_error,
        ):
            app = create_app()
            from fastapi.testclient import TestClient

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/login")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("internal_error", response.text.lower())

    def test_login_redirects_to_inicio_after_success(self):
        request = FakeRequest(session={})
        fake_user = SimpleNamespace(id=7, company_id=1, membership_id=9, company_slug="demo", email="admin@anchi.local")
        fake_db = SimpleNamespace(get=lambda _model, _id: SimpleNamespace(name="Demo"))
        with patch("app.auth.routes.authenticate_user", return_value=fake_user):
            response = login(request, email="admin@anchi.local", password="demo", master_db=fake_db)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers.get("location"), "/inicio")
        self.assertEqual(request.session["company_slug"], "demo")

    def test_root_redirects_to_inicio(self):
        app = create_app()
        from fastapi.testclient import TestClient

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers.get("location"), "/inicio")


if __name__ == "__main__":
    unittest.main()