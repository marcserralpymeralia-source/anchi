from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from scripts.performance_data import build_performance_fixture, performance_test_client, temporary_performance_environment  # noqa: E402
from app.main import app  # noqa: E402
from app.core.app_factory import create_app  # noqa: E402
from app.core import lifespan as lifespan_module  # noqa: E402
from app.db.models import Email, Order  # noqa: E402


class OperationalNavigationTests(unittest.TestCase):
    def test_canonical_operator_routes_are_registered(self):
        routes = {(route.path, tuple(sorted(route.methods or []))) for route in app.routes if hasattr(route, "methods")}
        expected = {
            ("/entries", ("GET",)),
            ("/entries/{entry_id}", ("GET",)),
            ("/entries/{entry_id}/process", ("POST",)),
            ("/entries/{entry_id}/resolve", ("GET",)),
            ("/orders/{order_id}/save", ("POST",)),
            ("/orders/{order_id}/reprocess", ("POST",)),
            ("/orders/{order_id}/validate", ("POST",)),
            ("/orders/{order_id}/export", ("POST",)),
            ("/orders/{order_id}/discard", ("POST",)),
            ("/knowledge", ("GET",)),
        }
        for item in expected:
            self.assertIn(item, routes)

    def test_main_navigation_exposes_only_operational_sections(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                response = client.get("/inicio")

            self.assertEqual(response.status_code, 200)
            for label in ("Pedidos pendientes", "Entradas", "Productos", "Clientes", "Configuracion"):
                self.assertIn(label, response.text)
            for hidden_label in ("Conocimiento", "Histórico", "Bandeja de entrada", "Jobs", "Usuarios", "Importación rápida"):
                self.assertNotIn(f'class="nav-label">{hidden_label}</span>', response.text)
        finally:
            fixture.cleanup()

    def test_entries_canonical_route_is_available_and_knowledge_redirects_to_customers(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                entries = client.get("/entries")
                knowledge = client.get("/knowledge", follow_redirects=False)

            self.assertEqual(entries.status_code, 200)
            self.assertIn("Entradas", entries.text)
            self.assertIn("Importar entrada", entries.text)
            self.assertNotIn("Vista técnica", entries.text)
            self.assertEqual(knowledge.status_code, 303)
            self.assertEqual(knowledge.headers["location"], "/customers?view=knowledge")
        finally:
            fixture.cleanup()

    def test_entries_redirects_to_login_without_session(self):
        fixture = build_performance_fixture("small")
        try:
            with temporary_performance_environment(fixture):
                test_app = create_app()
                with patch.object(lifespan_module, "start_email_sync_worker", lambda: None), patch.object(lifespan_module, "start_job_worker", lambda: None):
                    with TestClient(test_app, raise_server_exceptions=False) as client:
                        response = client.get("/entries", follow_redirects=False)

            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/login")
        finally:
            fixture.cleanup()

    def test_entries_keeps_tenant_scope(self):
        fixture = build_performance_fixture("small")
        engine = create_engine(fixture.tenant_database_url, connect_args={"check_same_thread": False})
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        try:
            with SessionLocal() as db:
                db.add(
                    Email(
                        company_id=999,
                        sender="tenant-b@example.com",
                        subject="Pedido tenant B invisible",
                        body="No debe aparecer en tenant A",
                        status="pending",
                        agent_status="not_processed",
                    )
                )
                db.commit()
            with performance_test_client(fixture) as client:
                response = client.get("/entries")

            self.assertEqual(response.status_code, 200)
            self.assertNotIn("Pedido tenant B invisible", response.text)
        finally:
            engine.dispose()
            fixture.cleanup()

    def test_resolve_entry_with_order_renders_review_screen(self):
        fixture = build_performance_fixture("small")
        engine = create_engine(fixture.tenant_database_url, connect_args={"check_same_thread": False})
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        try:
            with SessionLocal() as db:
                order = db.get(Order, fixture.order_ids[0])
                self.assertIsNotNone(order)
                email_id = order.email_id
            with performance_test_client(fixture) as client:
                response = client.get(f"/entries/email-{email_id}/resolve", follow_redirects=False)

            self.assertEqual(response.status_code, 200)
            self.assertIn("Propuesta del agente", response.text)
            self.assertIn("Pedido / correo / documento recibido", response.text)
        finally:
            engine.dispose()
            fixture.cleanup()

    def test_primary_operational_buttons_do_not_use_empty_links(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                dashboard = client.get("/inicio")
                entries = client.get("/entries")

            self.assertEqual(dashboard.status_code, 200)
            self.assertEqual(entries.status_code, 200)
            self.assertNotIn('href="#"', dashboard.text)
            self.assertNotIn('href="#"', entries.text)
            self.assertNotIn('action=""', dashboard.text)
            self.assertNotIn('action=""', entries.text)
        finally:
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
