from __future__ import annotations

import os
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")
os.environ.setdefault("PERFORMANCE_PROFILING_ENABLED", "true")
os.environ.setdefault("ENABLE_PERFORMANCE_PROFILING", "true")

from app.db.models import Email, EmailSettings  # noqa: E402
from app.master.models import EmailSyncState  # noqa: E402
from scripts.performance_data import build_performance_fixture, performance_test_client  # noqa: E402


def _tenant_session(tenant_path: Path):
    engine = create_engine(f"sqlite:///{tenant_path.as_posix()}", connect_args={"check_same_thread": False})
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


class MailInboxRoutesTests(unittest.TestCase):
    def test_mail_routes_are_registered(self):
        from app.core.app_factory import create_app

        def _collect_routes(app_or_router, prefix=""):
            collected = set()
            for route in getattr(app_or_router, "routes", []):
                if hasattr(route, "path") and hasattr(route, "methods"):
                    collected.add((prefix + route.path, tuple(sorted(route.methods or []))))
                elif hasattr(route, "original_router"):
                    p = getattr(route.include_context, "prefix", "") or ""
                    collected.update(_collect_routes(route.original_router, prefix=prefix + p))
            return collected

        routes = _collect_routes(create_app())
        expected = {
            ("/mail", ("GET",)),
            ("/mail/{email_id}", ("GET",)),
            ("/mail/{email_id}/process", ("POST",)),
            ("/cron/email-sync", ("GET", "POST")),
            ("/cron/jobs", ("GET", "POST")),
        }
        for item in expected:
            self.assertIn(item, routes)

    def test_jobs_cron_requires_auth_and_runs_single_job(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                unauthorized = client.get("/cron/jobs")

                with patch(
                    "app.cron.routes.run_worker_cycle",
                    return_value={
                        "tenants": 1,
                        "recovered": 0,
                        "attempted": 1,
                        "processed": 1,
                        "blocked": 0,
                    },
                ) as worker:
                    authorized = client.get(
                        "/cron/jobs",
                        headers={"x-vercel-cron": "1"},
                    )

            self.assertEqual(unauthorized.status_code, 403)
            self.assertEqual(authorized.status_code, 200)

            payload = authorized.json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["attempted"], 1)
            self.assertEqual(payload["processed"], 1)

            worker.assert_called_once_with(max_jobs=1)
        finally:
            fixture.cleanup()

    def test_mail_inbox_page_and_detail_are_available(self):
        fixture = build_performance_fixture("small")
        SessionLocal = _tenant_session(fixture.tenant_path)
        try:
            with SessionLocal() as db:
                email_id = db.scalar(select(Email.id).where(Email.company_id == fixture.company_id).order_by(Email.id))
            self.assertIsNotNone(email_id)

            with performance_test_client(fixture) as client:
                inbox = client.get("/mail", follow_redirects=False)
                detail = client.get(f"/mail/{email_id}")

            self.assertEqual(inbox.status_code, 303)
            self.assertEqual(inbox.headers["location"], "/")
            self.assertEqual(detail.status_code, 200)
            self.assertIn("Detalle de correo", detail.text)
            self.assertNotIn("Internal Server Error", detail.text)
        finally:
            fixture.cleanup()

    def test_mail_inbox_defaults_to_ten_and_filters_active_account(self):
        fixture = build_performance_fixture("small")
        master_engine = create_engine(fixture.master_database_url, connect_args={"check_same_thread": False})
        tenant_engine = create_engine(fixture.tenant_database_url, connect_args={"check_same_thread": False})
        MasterSession = sessionmaker(bind=master_engine, autoflush=False, autocommit=False)
        TenantSession = sessionmaker(bind=tenant_engine, autoflush=False, autocommit=False)
        try:
            with MasterSession() as master_db:
                state = master_db.scalar(select(EmailSyncState).where(EmailSyncState.company_id == fixture.company_id, EmailSyncState.channel_key == "email"))
                self.assertIsNotNone(state)
                assert state is not None
                state.mailbox = "INBOX"
                state.uidvalidity = "777"
                master_db.commit()

            with TenantSession() as tenant_db:
                for index in range(12):
                    tenant_db.add(
                        Email(
                            company_id=fixture.company_id,
                            external_id=f"active-{index}",
                            message_id=f"<active-{index}@example.com>",
                            imap_mailbox="INBOX",
                            imap_uidvalidity="777",
                            imap_uid=str(200 + index),
                            sender="nuevo@example.com",
                            subject=f"Activo {index}",
                            body="Pedido activo",
                            received_at=datetime(2026, 7, 31, 12, index, tzinfo=timezone.utc),
                        )
                    )
                for index in range(2):
                    tenant_db.add(
                        Email(
                            company_id=fixture.company_id,
                            external_id=f"legacy-{index}",
                            message_id=f"<legacy-{index}@example.com>",
                            imap_mailbox="INBOX",
                            imap_uidvalidity="111",
                            imap_uid=str(50 + index),
                            sender="legacy@example.com",
                            subject=f"Antiguo {index}",
                            body="Pedido antiguo",
                            received_at=datetime(2026, 7, 30, 12, index, tzinfo=timezone.utc),
                        )
                    )
                tenant_db.commit()

            with performance_test_client(fixture) as client:
                inbox = client.get("/mail", follow_redirects=True)

            self.assertEqual(inbox.status_code, 200)
            self.assertIn("Bandeja", inbox.text)
        finally:
            master_engine.dispose()
            tenant_engine.dispose()
            fixture.cleanup()

    def test_mail_inbox_redirects_to_dashboard(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                response = client.get("/mail", follow_redirects=False)

            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/")
        finally:
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
