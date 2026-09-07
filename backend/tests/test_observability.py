from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import os
import sys

os.environ.setdefault("APP_ENV", "development")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.admin.diagnostics import company_diagnostics  # noqa: E402
from app.core.metrics import snapshot_metrics  # noqa: E402
from app.core.middleware import branding_middleware  # noqa: E402
from app.core.observability import decode_structured_message, observability_scope  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import AuditLog, BackgroundJob, Company, PromptExecution  # noqa: E402
from app.health.routes import health_live, health_ready  # noqa: E402
from app.jobs.service import enqueue_job, job_payload, job_trace  # noqa: E402
from app.logs.service import audit_log_text, log_action, log_flow_event  # noqa: E402
from app.logs.routes import _company_timezone_name, _serialize_audit_log, delete_logs, logs_download  # noqa: E402
from app.agent.prompt_runtime import run_prompt_execution  # noqa: E402
from app.master.database import MasterBase  # noqa: E402
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser  # noqa: E402
from app.tenancy.database import get_tenant_engine  # noqa: E402


class FakeRequest:
    def __init__(self, session: dict | None = None, headers: dict | None = None, host: str = "localhost"):
        self.scope = {"session": session or {}}
        self.headers = {"host": host, **(headers or {})}
        self.state = SimpleNamespace()
        self.url = SimpleNamespace(path="/demo")
        self.method = "GET"


class ObservabilityTests(unittest.TestCase):
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
        get_tenant_engine.cache_clear()
        self.tempdir.cleanup()

    def _seed_master(self):
        db = self.MasterSession()
        company = MasterCompany(id=1, name="Demo", slug="demo", active=True)
        user = MasterUser(id=1, email="admin@anchi.local", full_name="Admin Demo", password_hash=hash_password("admin123"), is_active=True)
        membership = CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=True)
        tenant_db = MasterTenantDatabase(company_id=1, database_key="demo", database_url=f"sqlite:///{self.tenant_path.as_posix()}", is_active=True, health_status="ok")
        db.add_all([company, user, membership, tenant_db])
        db.commit()
        db.close()

    def test_log_action_persists_structured_trace_context(self):
        db = self.TenantSession()
        with observability_scope(request_id="req-1", correlation_id="corr-1", tenant_id=1, user_id=7, membership_id=9):
            log_action(
                db,
                company_id=1,
                user=SimpleNamespace(id=7),
                action="email.saved",
                message="Correo guardado",
                entity_type="email",
                entity_id=11,
                metadata={"api_key": "secret", "channel": "email"},
            )
        log = db.scalar(select(AuditLog).where(AuditLog.company_id == 1))
        self.assertIsNotNone(log)
        parsed = decode_structured_message(log.message)
        self.assertEqual(parsed["message"], "Correo guardado")
        self.assertEqual(parsed["context"]["request_id"], "req-1")
        self.assertEqual(parsed["context"]["correlation_id"], "corr-1")
        self.assertEqual(parsed["metadata"]["api_key"], "[redacted]")
        self.assertEqual(parsed["metadata"]["channel"], "email")
        db.close()

    def test_log_action_ignores_missing_user_row_in_target_database(self):
        db = self.TenantSession()
        log_action(
            db,
            company_id=1,
            user=SimpleNamespace(id=99),
            action="email.imap.test",
            message="No se ha podido conectar con imap.demo.local:993.",
            entity_type="settings",
            entity_id=11,
        )
        log = db.scalar(select(AuditLog).where(AuditLog.company_id == 1))
        self.assertIsNotNone(log)
        self.assertIsNone(log.user_id)
        self.assertIn("imap.demo.local", decode_structured_message(log.message)["message"])
        db.close()

    def test_flow_event_is_correlated_and_exportable_without_credentials(self):
        db = self.TenantSession()
        with observability_scope(flow_id="flow-email-1", correlation_id="corr-flow-1"):
            log_flow_event(
                db,
                company_id=1,
                event="pipeline.scored",
                stage="scoring",
                message="Scoring calculado.",
                entity_type="order",
                entity_id=12,
                status="success",
                metadata={"score": 94, "access_token": "must-not-be-stored"},
            )

        log = db.scalar(select(AuditLog).where(AuditLog.company_id == 1))
        self.assertIsNotNone(log)
        parsed = decode_structured_message(log.message)
        self.assertEqual(log.action, "flow.pipeline.scored")
        self.assertEqual(parsed["context"]["flow_id"], "flow-email-1")
        self.assertEqual(parsed["metadata"]["event"], "pipeline.scored")
        self.assertEqual(parsed["metadata"]["access_token"], "[redacted]")

        exported = audit_log_text([log])
        record = json.loads(exported.strip())
        self.assertEqual(record["metadata"]["flow_id"], "flow-email-1")
        self.assertEqual(record["metadata"]["access_token"], "[redacted]")
        db.close()

    def test_logs_use_tenant_timezone_for_display_and_export(self):
        db = self.TenantSession()
        db.add(Company(id=1, timezone="Europe/Madrid"))
        db.commit()
        self.assertEqual(_company_timezone_name(db, 1), "Europe/Madrid")

        created_at = datetime(2026, 9, 4, 9, 50, 5, tzinfo=timezone.utc)
        log = AuditLog(
            company_id=1,
            action="flow.email.persisted",
            message="Correo guardado",
            created_at=created_at,
        )

        serialized = _serialize_audit_log(log, timezone_name="Europe/Madrid")
        self.assertEqual(serialized["created_label"], "04/09/2026 11:50:05")

        record = json.loads(audit_log_text([log], timezone_name="Europe/Madrid").strip())
        self.assertEqual(record["timestamp"], created_at.isoformat())
        self.assertEqual(record["timestamp_local"], "04/09/2026 11:50:05")
        self.assertEqual(record["timezone"], "Europe/Madrid")
        db.close()

    def test_log_download_and_delete_are_scoped_to_company(self):
        db = self.TenantSession()
        log_action(db, company_id=1, user=None, action="flow.email.persisted", message="Empresa 1")
        db.add(AuditLog(company_id=2, action="flow.email.persisted", message="Empresa 2"))
        db.commit()
        user = SimpleNamespace(
            id=7,
            company_id=1,
            role=SimpleNamespace(name="Administrador"),
        )

        response = logs_download(db=db, user=user)
        body = response.body.decode("utf-8")
        self.assertIn("Empresa 1", body)
        self.assertNotIn("Empresa 2", body)
        self.assertIn("attachment; filename=\"anchi-flow-logs.log\"", response.headers["content-disposition"])

        redirect = delete_logs(db=db, user=user)
        self.assertEqual(redirect.status_code, 303)
        self.assertIsNone(db.scalar(select(AuditLog).where(AuditLog.company_id == 1)))
        self.assertIsNotNone(db.scalar(select(AuditLog).where(AuditLog.company_id == 2)))
        db.close()

    def test_ai_provider_failure_is_persisted_as_flow_event(self):
        db = self.TenantSession()
        settings = SimpleNamespace(classification_model="gpt-4.1-mini")

        def failing_provider(_settings, _messages, _model):
            raise TimeoutError("provider timeout")

        with observability_scope(flow_id="flow-ai-1"):
            with self.assertRaises(TimeoutError):
                run_prompt_execution(
                    db,
                    company_id=1,
                    purpose="classification",
                    settings=settings,
                    text="Contenido de prueba suficiente.",
                    provider_call=failing_provider,
                )

        logs = db.scalars(select(AuditLog).order_by(AuditLog.id.asc())).all()
        events = [decode_structured_message(log.message)["metadata"].get("event") for log in logs]
        self.assertIn("ai.started", events)
        self.assertIn("ai.failed", events)
        failed = next(log for log in logs if decode_structured_message(log.message)["metadata"].get("event") == "ai.failed")
        failed_metadata = decode_structured_message(failed.message)["metadata"]
        self.assertEqual(failed_metadata["error_type"], "TimeoutError")
        self.assertEqual(failed_metadata["prompt_execution_id"], 1)
        execution = db.scalar(select(PromptExecution).where(PromptExecution.id == failed_metadata["prompt_execution_id"]))
        self.assertIsNotNone(execution)
        self.assertEqual(execution.output_status, "provider_error")
        db.close()

    def test_enqueue_job_embeds_trace_without_polluting_payload(self):
        db = self.TenantSession()
        with observability_scope(request_id="req-2", correlation_id="corr-2", tenant_id=1, user_id=8, membership_id=10, route="/orders", method="POST"):
            job = enqueue_job(db, company_id=1, job_type="process_email", payload={"email_id": 42}, created_by_user_id=8)
        self.assertEqual(job_payload(job), {"email_id": 42})
        trace = job_trace(job)
        self.assertEqual(trace["request_id"], "req-2")
        self.assertEqual(trace["correlation_id"], "corr-2")
        self.assertEqual(trace["tenant_id"], 1)
        self.assertEqual(trace["user_id"], 8)
        self.assertFalse(db.scalar(select(BackgroundJob).where(BackgroundJob.id == job.id)) is None)
        db.close()

    def test_middleware_sets_request_and_correlation_headers(self):
        request = FakeRequest(headers={"x-correlation-id": "corr-fixed"})

        async def call_next(_request):
            return JSONResponse({"ok": True})

        fake_session = SimpleNamespace(close=lambda: None)
        before = snapshot_metrics()["requests_total"]
        with patch("app.core.middleware.MasterSessionLocal", return_value=fake_session), patch("app.core.middleware.load_tenant_context", return_value=None):
            response = asyncio.run(branding_middleware(request, call_next))

        after = snapshot_metrics()
        self.assertTrue(response.headers.get("X-Request-ID"))
        self.assertEqual(response.headers.get("X-Correlation-ID"), "corr-fixed")
        self.assertEqual(after["requests_total"], before + 1)
        self.assertGreaterEqual(after["requests_by_status"].get("200", 0), 1)

    def test_health_and_diagnostics_expose_observability(self):
        self._seed_master()
        db = self.MasterSession()
        tenant_request = FakeRequest(session={"membership_id": 1, "user_id": 1, "company_id": 1, "company_slug": "demo"})
        tenant_request.state.tenant = SimpleNamespace(company=SimpleNamespace(id=1, slug="demo", database_url=f"sqlite:///{self.tenant_path.as_posix()}"))
        tenant_request.state.request_id = "req-3"
        tenant_request.state.correlation_id = "corr-3"

        ready = health_ready(tenant_request, db)
        live = health_live(tenant_request)
        diagnostics = company_diagnostics(db, 1)

        self.assertTrue(ready["ok"])
        self.assertTrue(ready["tenant_ping"])
        self.assertEqual(live["correlation_id"], "corr-3")
        self.assertIn("metrics", live)
        self.assertIn("observability", diagnostics)
        self.assertIn("requests_total", diagnostics["observability"])
        db.close()


if __name__ == "__main__":
    unittest.main()
