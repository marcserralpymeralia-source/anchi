from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")

from app.core import lifespan as lifespan_module  # noqa: E402
from app.core.app_factory import create_app  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.core.encryption import encrypt_secret
from app.db.models import Company, Customer, Email, InputChannel, LLMSettings, Order, OrderLine, Product, utcnow
from app.settings.branding import get_or_create_branding
from app.settings.service import get_or_create_settings
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser  # noqa: E402
from scripts.performance_data import build_performance_fixture, temporary_performance_environment  # noqa: E402


def _session_factory(database_url: str):
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args)
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False)


class PendingOrdersAccessTests(unittest.TestCase):
    def _client_for(self, fixture):
        context = temporary_performance_environment(fixture)
        context.__enter__()
        app = create_app()
        lifespan_patches = (
            patch.object(lifespan_module, "start_email_sync_worker", lambda: None),
            patch.object(lifespan_module, "start_job_worker", lambda: None),
        )
        for item in lifespan_patches:
            item.__enter__()
        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()

        def cleanup():
            client.__exit__(None, None, None)
            for item in reversed(lifespan_patches):
                item.__exit__(None, None, None)
            context.__exit__(None, None, None)

        return client, cleanup

    def test_unauthenticated_pending_orders_redirects_to_login_with_next(self):
        fixture = build_performance_fixture("small")
        client, cleanup = self._client_for(fixture)
        try:
            response = client.get("/inicio", follow_redirects=False)

            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/login?next=%2Finicio")
        finally:
            cleanup()
            fixture.cleanup()

    def test_login_with_pending_orders_destination_sets_cookie_and_returns_to_pending_orders(self):
        fixture = build_performance_fixture("small")
        client, cleanup = self._client_for(fixture)
        try:
            login_response = client.post(
                "/login",
                data={"email": fixture.admin_email, "password": fixture.admin_password, "next": "/inicio"},
                follow_redirects=False,
            )
            self.assertEqual(login_response.status_code, 303)
            self.assertEqual(login_response.headers["location"], "/inicio")
            self.assertIn(f"{get_settings().session_cookie}=", login_response.headers.get("set-cookie", ""))

            pending_response = client.get("/inicio", follow_redirects=False)
            self.assertEqual(pending_response.status_code, 303)
            self.assertEqual(pending_response.headers["location"], "/")
        finally:
            cleanup()
            fixture.cleanup()

    def test_authenticated_pending_orders_redirects_to_root_bandeja(self):
        fixture = build_performance_fixture("small")
        client, cleanup = self._client_for(fixture)
        try:
            client.post("/login", data={"email": fixture.admin_email, "password": fixture.admin_password}, follow_redirects=False)
            response = client.get("/inicio", follow_redirects=False)

            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/")
        finally:
            cleanup()
            fixture.cleanup()

    def test_pending_orders_menu_uses_registered_relative_route(self):
        fixture = build_performance_fixture("small")
        client, cleanup = self._client_for(fixture)
        try:
            app_route_names = {route.name for route in client.app.routes}
            self.assertIn("dashboard", app_route_names)

            client.post("/login", data={"email": fixture.admin_email, "password": fixture.admin_password}, follow_redirects=False)
            response = client.get("/", follow_redirects=False)

            self.assertEqual(response.status_code, 200)
            self.assertIn('href="/"', response.text)
            self.assertNotIn('href="#"', response.text)
            self.assertNotIn('href="http://127.0.0.1:8000/"', response.text)
        finally:
            cleanup()
            fixture.cleanup()

    def test_workbench_order_detail_uses_compact_product_search(self):
        fixture = build_performance_fixture("small")
        client, cleanup = self._client_for(fixture)
        try:
            login = client.post(
                "/login",
                data={"email": fixture.admin_email, "password": fixture.admin_password},
                follow_redirects=False,
            )
            self.assertEqual(login.status_code, 303)

            _, TenantSession = _session_factory(fixture.tenant_database_url)
            with TenantSession() as db:
                customer = db.query(Customer).filter(Customer.company_id == 1).first()
                product = db.query(Product).filter(Product.company_id == 1).first()
                self.assertIsNotNone(customer)
                self.assertIsNotNone(product)

                order = Order(
                    company_id=1,
                    customer_id=customer.id,
                    validated_customer_id=customer.id,
                    customer_detected_name="Pedido de prueba UAT",
                    score=51,
                    status="pedido_pendiente_revision",
                    created_at=utcnow(),
                )
                db.add(order)
                db.flush()
                db.add(
                    OrderLine(
                        company_id=1,
                        order_id=order.id,
                        product_id=product.id,
                        validated_product_id=None,
                        original_text="12 cajas demo",
                        detected_product="Caja demo",
                        detected_reference="DEMO-REF",
                        quantity=12,
                        unit="cajas",
                        extraction_confidence=0.75,
                        line_score=42,
                        validation_status="pending",
                        doubt_reason="Linea pendiente de validar",
                    )
                )
                db.commit()
                order_id = order.id

            response = client.get(f"/workbench/item/order/{order_id}/detail")

            self.assertEqual(response.status_code, 200)
            self.assertIn('data-line-product-autocomplete', response.text)
            self.assertIn('data-line-product-input', response.text)
            self.assertIn('data-line-product-results', response.text)
            self.assertIn('data-line-product-clear', response.text)
            self.assertIn('La referencia del cliente es solo una pista', response.text)
            self.assertNotIn('data-line-product-select', response.text)
            self.assertNotIn('<option value="0">Sin referencia</option>', response.text)
            self.assertIn('Ref. interna', response.text)
            self.assertIn('Detectado:', response.text)
        finally:
            cleanup()
            fixture.cleanup()

    def test_next_rejects_external_url(self):
        fixture = build_performance_fixture("small")
        client, cleanup = self._client_for(fixture)
        try:
            response = client.post(
                "/login",
                data={"email": fixture.admin_email, "password": fixture.admin_password, "next": "https://example.com/pwn"},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/")
        finally:
            cleanup()
            fixture.cleanup()

    def test_inactive_membership_is_not_treated_as_anonymous(self):
        fixture = build_performance_fixture("small")
        master_engine, MasterSession = _session_factory(fixture.master_database_url)
        client, cleanup = self._client_for(fixture)
        try:
            login_response = client.post("/login", data={"email": fixture.admin_email, "password": fixture.admin_password}, follow_redirects=False)
            self.assertEqual(login_response.status_code, 303)
            with MasterSession() as db:
                membership = db.get(CompanyMembership, 1)
                membership.is_active = False
                db.commit()

            response = client.get("/inicio", follow_redirects=False)
            self.assertEqual(response.status_code, 403)
            self.assertNotEqual(response.headers.get("location"), "/login")
        finally:
            cleanup()
            master_engine.dispose()
            fixture.cleanup()

    def test_order_mutations_reject_cross_tenant_entities(self):
        fixture = build_performance_fixture("small")
        master_engine, MasterSession = _session_factory(fixture.master_database_url)
        tenant_engine, TenantSession = _session_factory(fixture.tenant_database_url)
        client, cleanup = self._client_for(fixture)
        try:
            with MasterSession() as db:
                db.add(MasterCompany(id=2, name="Tenant B", slug="tenant-b", active=True))
                db.add(
                    MasterUser(
                        id=2,
                        email="admin@tenant-b.local",
                        full_name="Admin B",
                        password_hash=hash_password("admin123"),
                        is_active=True,
                    )
                )
                db.add(
                    CompanyMembership(
                        id=2,
                        user_id=2,
                        company_id=2,
                        role_key="Administrador",
                        is_active=True,
                        is_owner=True,
                    )
                )
                db.add(
                    MasterTenantDatabase(
                        company_id=2,
                        database_key="tenant-b",
                        database_url=fixture.tenant_database_url,
                        is_active=True,
                        health_status="ok",
                    )
                )
                db.commit()

            with TenantSession() as db:
                if not db.get(Company, 2):
                    db.add(
                        Company(
                            id=2,
                            name="Tenant B",
                            legal_name="Tenant B SL",
                            country="España",
                            language="es",
                            timezone="Europe/Madrid",
                            active=True,
                        )
                    )
                    db.flush()

                customer_a = Customer(
                    company_id=1,
                    code="TA-CROSS-001",
                    fiscal_name="Cliente Tenant A Cross",
                )
                customer_b = Customer(
                    company_id=2,
                    code="TB-CROSS-001",
                    fiscal_name="Cliente Tenant B Cross",
                )
                product_a = Product(
                    company_id=1,
                    reference="TA-CROSS-P001",
                    name="Producto Tenant A Cross",
                )
                product_b = Product(
                    company_id=2,
                    reference="TB-CROSS-P001",
                    name="Producto Tenant B Cross",
                )
                db.add_all([customer_a, customer_b, product_a, product_b])
                db.flush()

                order_a = Order(
                    company_id=1,
                    customer_id=customer_a.id,
                    validated_customer_id=customer_a.id,
                    customer_detected_name="Pedido A",
                    score=60,
                    status="pending_review",
                    created_at=utcnow(),
                )
                order_a_other = Order(
                    company_id=1,
                    customer_id=customer_a.id,
                    validated_customer_id=customer_a.id,
                    customer_detected_name="Pedido A secundario",
                    score=60,
                    status="pending_review",
                    created_at=utcnow(),
                )
                db.add_all([order_a, order_a_other])
                db.flush()

                line_a = OrderLine(
                    company_id=1,
                    order_id=order_a.id,
                    product_id=product_a.id,
                    validated_product_id=product_a.id,
                    original_text="Linea A",
                    quantity=1,
                    unit="ud",
                    extraction_confidence=1,
                    line_score=100,
                    validation_status="validated",
                )
                line_other = OrderLine(
                    company_id=1,
                    order_id=order_a_other.id,
                    product_id=product_a.id,
                    validated_product_id=product_a.id,
                    original_text="Linea de otro pedido",
                    quantity=2,
                    unit="ud",
                    extraction_confidence=1,
                    line_score=100,
                    validation_status="validated",
                )
                db.add_all([line_a, line_other])
                db.commit()

                order_a_id = order_a.id
                order_a_other_id = order_a_other.id
                line_a_id = line_a.id
                line_other_id = line_other.id
                customer_a_id = customer_a.id
                customer_b_id = customer_b.id
                product_a_id = product_a.id
                product_b_id = product_b.id

            login = client.post(
                "/login",
                data={
                    "email": fixture.admin_email,
                    "password": fixture.admin_password,
                },
                follow_redirects=False,
            )
            self.assertEqual(login.status_code, 303)

            # Tenant A no puede asignar un cliente de Tenant B.
            response = client.post(
                f"/orders/{order_a_id}/customer",
                data={"validated_customer_id": customer_b_id},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303)

            with TenantSession() as db:
                order = db.get(Order, order_a_id)
                self.assertEqual(order.customer_id, customer_a_id)
                self.assertEqual(order.validated_customer_id, customer_a_id)

            # Tampoco puede hacerlo mediante el formulario general del pedido.
            response = client.post(
                f"/orders/{order_a_id}/update",
                data={
                    "validated_customer_id": customer_b_id,
                    "order_date": "",
                    "requested_delivery_date": "",
                    "notes": "No debe aplicarse",
                    "status": "",
                },
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303)

            with TenantSession() as db:
                order = db.get(Order, order_a_id)
                self.assertEqual(order.customer_id, customer_a_id)
                self.assertEqual(order.validated_customer_id, customer_a_id)
                self.assertNotEqual(order.notes, "No debe aplicarse")

            # Tenant A no puede sustituir el producto por uno de Tenant B.
            response = client.post(
                f"/orders/{order_a_id}/lines/{line_a_id}",
                data={
                    "validated_product_id": product_b_id,
                    "quantity": 9,
                    "unit": "caja",
                },
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303)

            with TenantSession() as db:
                line = db.get(OrderLine, line_a_id)
                self.assertEqual(line.product_id, product_a_id)
                self.assertEqual(line.validated_product_id, product_a_id)
                self.assertEqual(line.quantity, 1)

            # Tenant A no puede añadir una linea usando un producto de Tenant B.
            with TenantSession() as db:
                before_count = (
                    db.query(OrderLine)
                    .filter(OrderLine.order_id == order_a_id)
                    .count()
                )

            response = client.post(
                f"/orders/{order_a_id}/lines",
                data={
                    "validated_product_id": product_b_id,
                    "original_text": "Producto cruzado",
                    "quantity": 5,
                    "unit": "ud",
                },
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303)

            with TenantSession() as db:
                after_count = (
                    db.query(OrderLine)
                    .filter(OrderLine.order_id == order_a_id)
                    .count()
                )
                self.assertEqual(after_count, before_count)

            # Una linea de otro pedido no puede duplicarse bajo order_a.
            response = client.post(
                f"/orders/{order_a_id}/lines/{line_other_id}/duplicate",
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303)

            with TenantSession() as db:
                other_order_lines = (
                    db.query(OrderLine)
                    .filter(OrderLine.order_id == order_a_id)
                    .count()
                )
                self.assertEqual(other_order_lines, before_count)

            # Ni puede borrarse indicando un order_id al que no pertenece.
            response = client.post(
                f"/orders/{order_a_id}/lines/{line_other_id}/delete",
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303)

            with TenantSession() as db:
                self.assertIsNotNone(db.get(OrderLine, line_other_id))
                self.assertEqual(
                    db.get(OrderLine, line_other_id).order_id,
                    order_a_other_id,
                )
        finally:
            cleanup()
            master_engine.dispose()
            tenant_engine.dispose()
            fixture.cleanup()

    def test_pending_orders_are_scoped_by_authenticated_tenant(self):
        fixture = build_performance_fixture("small")
        master_engine, MasterSession = _session_factory(fixture.master_database_url)
        tenant_engine, TenantSession = _session_factory(fixture.tenant_database_url)
        client, cleanup = self._client_for(fixture)
        try:
            with MasterSession() as db:
                db.add(MasterCompany(id=2, name="Tenant B", slug="tenant-b", active=True))
                db.add(MasterUser(id=2, email="admin@tenant-b.local", full_name="Admin B", password_hash=hash_password("admin123"), is_active=True))
                db.add(CompanyMembership(id=2, user_id=2, company_id=2, role_key="Administrador", is_active=True, is_owner=True))
                db.add(MasterTenantDatabase(company_id=2, database_key="tenant-b", database_url=fixture.tenant_database_url, is_active=True, health_status="ok"))
                db.commit()
            with TenantSession() as db:
               company_b = Company(
                id=2,
                name="Tenant B",
                legal_name="Tenant B SL",
                country="España",
                language="es",
                timezone="Europe/Madrid",
                active=True,
            )
            db.add(company_b)
            db.flush()

            branding = get_or_create_branding(db, 2)
            branding.app_name = "Anchi"
            branding.company_name = "Tenant B"

            db.add(
                InputChannel(
                    company_id=2,
                    key="email",
                    name="Email",
                    channel_type="email",
                    is_active=True,
                )
            )
            db.add(Product(company_id=2, reference="TB-P001", name="Producto Tenant B"))
            db.add(Customer(company_id=2, code="TB-C001", fiscal_name="Cliente Tenant B"))

            llm = get_or_create_settings(db, LLMSettings, 2)
            llm.provider = "openai"
            llm.api_key_encrypted = encrypt_secret("tenant-b-performance-key")

            email_a = Email(
                company_id=1,
                sender="a@example.com",
                subject="Pedido visible tenant A",
                body="A",
                status="processed",
                agent_status="processed_doubtful",
                detected_type="pedido",
                received_at=utcnow(),
            )
            email_b = Email(
                company_id=2,
                sender="b@example.com",
                subject="Pedido visible tenant B",
                body="B",
                status="processed",
                agent_status="processed_doubtful",
                detected_type="pedido",
                received_at=utcnow(),
            )
            db.add_all([email_a, email_b])
            db.flush()

            db.add(
                Order(
                    company_id=1,
                    email_id=email_a.id,
                    customer_detected_name="Pedido visible tenant A",
                    score=65,
                    status="pending_review",
                    created_at=utcnow(),
                )
            )
            db.add(
                Order(
                    company_id=2,
                    email_id=email_b.id,
                    customer_detected_name="Pedido visible tenant B",
                    score=65,
                    status="pending_review",
                    created_at=utcnow(),
                )
            )
            db.commit()

            client.post("/login", data={"email": fixture.admin_email, "password": fixture.admin_password}, follow_redirects=False)
            tenant_a = client.get("/", follow_redirects=False)
            self.assertEqual(tenant_a.status_code, 200)
            self.assertIn("Pedido visible tenant A", tenant_a.text)
            self.assertNotIn("Pedido visible tenant B", tenant_a.text)

            client.post("/logout", follow_redirects=False)
            client.post("/login", data={"email": "admin@tenant-b.local", "password": "admin123"}, follow_redirects=False)
            tenant_b = client.get("/", follow_redirects=False)
            self.assertEqual(tenant_b.status_code, 200)
            self.assertIn("Pedido visible tenant B", tenant_b.text)
            self.assertNotIn("Pedido visible tenant A", tenant_b.text)
        finally:
            cleanup()
            master_engine.dispose()
            tenant_engine.dispose()
            fixture.cleanup()

    def test_session_cookie_configuration_is_consistent(self):
        get_settings.cache_clear()
        with patch.dict(os.environ, {"APP_ENV": "development"}, clear=False):
            app = create_app()
            session_middleware = next(middleware for middleware in app.user_middleware if middleware.cls.__name__ == "SessionMiddleware")
            self.assertEqual(session_middleware.kwargs["session_cookie"], get_settings().session_cookie)
            self.assertFalse(session_middleware.kwargs["https_only"])
            self.assertEqual(session_middleware.kwargs["same_site"], "lax")
        get_settings.cache_clear()

        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "SESSION_COOKIE_SECURE": "true",
                "MASTER_DATABASE_URL": "postgresql://user:pass@localhost/master",
                "TENANT_DB_MODE": "external",
                "TENANT_DATABASE_URL": "postgresql://user:pass@localhost/tenant",
                "ALLOWED_HOSTS": "anchi.example.com",
                "SECRET_KEY": "production-secret-key-with-enough-length-000000",
                "ENCRYPTION_KEY": "CKHCB4gFGn7kJVxowWH2pEdPucfPaZugSsMgoJU6eNE=",
                "DEFAULT_ADMIN_EMAIL": "ops@example.com",
                "DEFAULT_ADMIN_PASSWORD": "ProductionPassword2026!",
                "PERFORMANCE_PROFILING_ENABLED": "false",
                "ENABLE_PERFORMANCE_PROFILING": "false",
            },
            clear=False,
        ):
            app = create_app()
            session_middleware = next(middleware for middleware in app.user_middleware if middleware.cls.__name__ == "SessionMiddleware")
            self.assertTrue(session_middleware.kwargs["https_only"])
        get_settings.cache_clear()


if __name__ == "__main__":
    unittest.main()
