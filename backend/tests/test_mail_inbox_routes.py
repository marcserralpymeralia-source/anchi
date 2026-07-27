from __future__ import annotations

import os
import unittest
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")
os.environ.setdefault("PERFORMANCE_PROFILING_ENABLED", "true")
os.environ.setdefault("ENABLE_PERFORMANCE_PROFILING", "true")

from app.db.models import Email  # noqa: E402
from scripts.performance_data import build_performance_fixture, performance_test_client  # noqa: E402


def _tenant_session(tenant_path: Path):
    engine = create_engine(f"sqlite:///{tenant_path.as_posix()}", connect_args={"check_same_thread": False})
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


class MailInboxRoutesTests(unittest.TestCase):
    def test_mail_routes_are_registered(self):
        routes = {(route.path, tuple(sorted(route.methods or []))) for route in __import__("app.main", fromlist=["app"]).app.routes if hasattr(route, "methods")}
        expected = {
            ("/mail", ("GET",)),
            ("/mail/{email_id}", ("GET",)),
            ("/mail/{email_id}/process", ("POST",)),
        }
        for item in expected:
            self.assertIn(item, routes)

    def test_mail_inbox_page_and_detail_are_available(self):
        fixture = build_performance_fixture("small")
        SessionLocal = _tenant_session(fixture.tenant_path)
        try:
            with SessionLocal() as db:
                email_id = db.scalar(select(Email.id).where(Email.company_id == fixture.company_id).order_by(Email.id))
            self.assertIsNotNone(email_id)

            with performance_test_client(fixture) as client:
                inbox = client.get("/mail")
                detail = client.get(f"/mail/{email_id}")

            self.assertEqual(inbox.status_code, 200)
            self.assertEqual(detail.status_code, 200)
            self.assertIn("Bandeja de entrada", inbox.text)
            self.assertIn("Detalle de correo", detail.text)
            self.assertNotIn("Internal Server Error", inbox.text)
            self.assertNotIn("Internal Server Error", detail.text)
        finally:
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
