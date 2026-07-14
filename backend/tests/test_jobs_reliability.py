from __future__ import annotations

import asyncio
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import UploadFile
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "development")

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.services import AgentProcessingService  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import BackgroundJob, Customer, Email, ImportJob, JobAttempt, LLMSettings, Order, OrderLine  # noqa: E402
from app.imports.service import create_preview  # noqa: E402
from app.jobs.service import claim_next_job, enqueue_job, fail_job, finish_job, recover_stale_jobs, retry_job  # noqa: E402
from app.master.database import MasterBase  # noqa: E402
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser  # noqa: E402
from app.workers.jobs_worker import _process_import_job, run_worker_cycle  # noqa: E402
from app.core.security import hash_password  # noqa: E402


class JobsReliabilityTests(unittest.TestCase):
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

    def _seed_master(self):
        db = self.MasterSession()
        company = MasterCompany(id=1, name="Demo", slug="demo", active=True)
        user = MasterUser(id=1, email="admin@anchi.local", full_name="Admin Demo", password_hash=hash_password("admin123"), is_active=True)
        membership = CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=True)
        tenant_db = MasterTenantDatabase(company_id=1, database_key="demo", database_url=f"sqlite:///{self.tenant_path.as_posix()}", is_active=True, health_status="ok")
        db.add_all([company, user, membership, tenant_db])
        db.commit()
        db.close()

    def _seed_llm(self):
        db = self.TenantSession()
        db.add(LLMSettings(company_id=1, api_key_encrypted="encrypted-token"))
        db.commit()
        db.close()

    def test_enqueue_job_is_idempotent_and_rejects_secrets(self):
        db = self.TenantSession()

        first = enqueue_job(db, company_id=1, job_type="process_email", payload={"email_id": 10}, created_by_user_id=1)
        second = enqueue_job(db, company_id=1, job_type="process_email", payload={"email_id": 10}, created_by_user_id=1)
        self.assertEqual(first.id, second.id)
        self.assertEqual(db.scalar(select(func.count()).select_from(BackgroundJob)) or 0, 1)

        db.get(BackgroundJob, first.id).status = "success"
        db.commit()
        third = enqueue_job(db, company_id=1, job_type="process_email", payload={"email_id": 10}, created_by_user_id=1)
        self.assertEqual(third.id, first.id)

        with self.assertRaises(ValueError):
            enqueue_job(db, company_id=1, job_type="process_email", payload={"password": "secret"}, created_by_user_id=1)
        db.close()

    def test_claim_finish_and_attempt_history(self):
        db = self.TenantSession()
        job = enqueue_job(db, company_id=1, job_type="process_email", payload={"email_id": 11}, created_by_user_id=1)

        claimed = claim_next_job(db, owner="worker-a", job_types={"process_email"})
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, job.id)
        self.assertEqual(claimed.attempt_count, 1)

        attempts = db.scalars(select(JobAttempt).where(JobAttempt.job_id == job.id)).all()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].status, "running")
        self.assertEqual(attempts[0].worker_id, "worker-a")

        finish_job(db, claimed, {"ok": True, "message": "done"})
        db.refresh(claimed)
        attempts = db.scalars(select(JobAttempt).where(JobAttempt.job_id == job.id)).all()
        self.assertEqual(attempts[0].status, "succeeded")
        self.assertIsNotNone(attempts[0].finished_at)
        self.assertEqual(claimed.status, "success")
        db.close()

    def test_retry_manual_keeps_attempt_history(self):
        db = self.TenantSession()
        job = enqueue_job(db, company_id=1, job_type="process_email", payload={"email_id": 12}, created_by_user_id=1)
        claimed = claim_next_job(db, owner="worker-a", job_types={"process_email"})
        self.assertIsNotNone(claimed)
        fail_job(db, claimed, "timeout", retry=False, error_type="TimeoutError")

        retried = retry_job(db, 1, job.id)
        self.assertIsNotNone(retried)
        self.assertEqual(retried.status, "queued")
        self.assertEqual(retried.retry_count, 1)
        attempts = db.scalars(select(JobAttempt).where(JobAttempt.job_id == job.id)).all()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].status, "failed_permanent")
        db.close()

    def test_stale_jobs_recover_or_fail_at_limit(self):
        db = self.TenantSession()
        stale_running = BackgroundJob(
            company_id=1,
            job_type="process_email",
            status="running",
            payload_json='{"email_id": 1}',
            lock_owner="old-worker",
            attempt_count=1,
            retry_count=0,
            max_retries=2,
        )
        db.add(stale_running)
        db.flush()
        stale_running.started_at = stale_running.created_at.replace(year=stale_running.created_at.year - 1)
        stale_running.lock_until = stale_running.started_at
        stale_running.last_heartbeat_at = stale_running.started_at
        db.add(JobAttempt(company_id=1, job_id=stale_running.id, attempt_number=1, worker_id="old-worker", status="running", started_at=stale_running.started_at))

        exhausted = BackgroundJob(
            company_id=1,
            job_type="process_email",
            status="running",
            payload_json='{"email_id": 2}',
            lock_owner="old-worker",
            attempt_count=1,
            retry_count=0,
            max_retries=0,
        )
        db.add(exhausted)
        db.flush()
        exhausted.started_at = exhausted.created_at.replace(year=exhausted.created_at.year - 1)
        exhausted.lock_until = exhausted.started_at
        exhausted.last_heartbeat_at = exhausted.started_at
        db.add(JobAttempt(company_id=1, job_id=exhausted.id, attempt_number=1, worker_id="old-worker", status="running", started_at=exhausted.started_at))
        db.commit()

        recovered = recover_stale_jobs(db, owner="worker-b", job_types={"process_email"})
        db.refresh(stale_running)
        db.refresh(exhausted)
        self.assertEqual(len(recovered), 2)
        self.assertEqual(stale_running.status, "retrying")
        self.assertEqual(stale_running.retry_count, 1)
        self.assertIsNotNone(stale_running.next_retry_at)
        self.assertEqual(exhausted.status, "failed")
        self.assertIsNotNone(exhausted.finished_at)
        abandoned_attempt = db.scalar(select(JobAttempt).where(JobAttempt.job_id == stale_running.id))
        self.assertEqual(abandoned_attempt.status, "abandoned")
        db.close()

    def test_process_email_retry_is_idempotent(self):
        self._seed_llm()
        db = self.TenantSession()
        email = Email(company_id=1, external_id="mail-1", sender="cliente@example.com", subject="Pedido", body="10 cajas")
        db.add(email)
        db.commit()
        calls = {"count": 0}

        def fake_process_inbound_message(_self, session, inbound_message, user=None, force_order=False, email=None):  # noqa: ANN001
            calls["count"] += 1
            if inbound_message.order_id:
                return {"ok": True, "status": "order_detected", "message": f"Pedido {inbound_message.order_id} ya habia sido creado.", "order_id": inbound_message.order_id, "score": 91}
            order = Order(company_id=inbound_message.company_id, email_id=email.id if email else None, customer_detected_name="Cliente demo", status="pedido_pendiente_revision", score=91)
            session.add(order)
            session.flush()
            session.add(OrderLine(company_id=inbound_message.company_id, order_id=order.id, original_text="10 cajas", quantity=10, unit="cajas", extraction_confidence=0.95, validation_status="validated"))
            inbound_message.order_id = order.id
            inbound_message.customer_id = None
            inbound_message.status = "order_detected"
            inbound_message.processing_step = "completed"
            inbound_message.score = 91
            inbound_message.last_processed_at = inbound_message.received_at
            session.commit()
            return {"ok": True, "status": "order_detected", "message": f"Pedido {order.id} creado.", "order_id": order.id, "score": 91}

        with patch("app.agent.platform.UnifiedOrderPipelineService.process_inbound_message", new=fake_process_inbound_message):
            result_first = AgentProcessingService().process_email(db, email)
            self.assertTrue(result_first["ok"])
            result_second = AgentProcessingService().process_email(db, email)
            self.assertTrue(result_second["ok"])
            self.assertEqual(result_first["order_id"], result_second["order_id"])
            self.assertEqual(calls["count"], 1)

        db.close()

    def test_import_confirm_retry_is_idempotent(self):
        db = self.TenantSession()
        csv_bytes = b"code,fiscal_name,email\nC001,Cliente Uno,uno@example.com\n"
        upload = UploadFile(filename="customers.csv", file=io.BytesIO(csv_bytes))
        preview = asyncio.run(create_preview(upload, "customers"))
        payload = {
            "token": preview["token"],
            "filename": preview["filename"],
            "entity_type": "customers",
            "encoding": "utf-8",
            "mapping": preview["guessed_mapping"],
            "mode": "create_update",
            "save_template": False,
            "template_name": "",
        }
        job = enqueue_job(db, company_id=1, job_type="import_confirm", payload=payload, created_by_user_id=1)
        result_first = _process_import_job(db, job, payload)
        self.assertTrue(result_first["ok"])
        customers_after_first = db.scalar(select(func.count()).select_from(Customer)) or 0
        imports_after_first = db.scalar(select(func.count()).select_from(ImportJob)) or 0

        result_second = _process_import_job(db, job, payload)
        self.assertTrue(result_second["ok"])
        self.assertEqual(customers_after_first, db.scalar(select(func.count()).select_from(Customer)) or 0)
        self.assertEqual(imports_after_first, db.scalar(select(func.count()).select_from(ImportJob)) or 0)
        db.close()

    def test_worker_cycle_processes_job_once(self):
        self._seed_master()
        self._seed_llm()
        db = self.TenantSession()
        email = Email(company_id=1, external_id="mail-2", sender="cliente@example.com", subject="Pedido worker", body="3 cajas")
        db.add(email)
        db.commit()
        job = enqueue_job(db, company_id=1, job_type="process_email", payload={"email_id": email.id}, created_by_user_id=1)
        db.close()

        def fake_process_inbound_message(_self, session, inbound_message, user=None, force_order=False, email=None):  # noqa: ANN001
            if inbound_message.order_id:
                return {"ok": True, "status": "order_detected", "message": f"Pedido {inbound_message.order_id} ya habia sido creado.", "order_id": inbound_message.order_id, "score": 88}
            order = Order(company_id=inbound_message.company_id, email_id=email.id if email else None, customer_detected_name="Cliente worker", status="pedido_pendiente_revision", score=88)
            session.add(order)
            session.flush()
            session.add(OrderLine(company_id=inbound_message.company_id, order_id=order.id, original_text="3 cajas", quantity=3, unit="cajas", extraction_confidence=0.9, validation_status="validated"))
            inbound_message.order_id = order.id
            inbound_message.status = "order_detected"
            inbound_message.processing_step = "completed"
            session.commit()
            return {"ok": True, "status": "order_detected", "message": f"Pedido {order.id} creado.", "order_id": order.id, "score": 88}

        with patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession), patch("app.agent.platform.UnifiedOrderPipelineService.process_inbound_message", new=fake_process_inbound_message):
            summary = run_worker_cycle()

        self.assertEqual(summary["tenants"], 1)
        self.assertGreaterEqual(summary["processed"], 1)

        db = self.TenantSession()
        processed_job = db.get(BackgroundJob, job.id)
        self.assertEqual(processed_job.status, "success")
        self.assertEqual(db.scalar(select(func.count()).select_from(Order)) or 0, 1)
        self.assertEqual(db.scalar(select(func.count()).select_from(JobAttempt).where(JobAttempt.job_id == job.id)) or 0, 1)
        db.close()


if __name__ == "__main__":
    unittest.main()
