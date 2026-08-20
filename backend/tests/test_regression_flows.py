from __future__ import annotations

import json
import gc
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker, selectinload
from sqlalchemy.pool import NullPool

import os
import sys

os.environ.setdefault("APP_ENV", "development")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.services import AgentProcessingService, MockAgentService  # noqa: E402
from app.channels.service import get_or_create_channel  # noqa: E402
from app.core.encryption import encrypt_secret  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import AuditLog, BackgroundJob, Company, Conversation, Customer, CustomerContactPoint, Email, ExportFile, FTPSettings, InboundMessage, LLMSettings, Order, OrderLine, Product, ProductAlias, ScoringSettings  # noqa: E402
from app.jobs.service import enqueue_job, retry_job  # noqa: E402
from app.master.database import MasterBase  # noqa: E402
from app.master.models import MasterCompany, MasterTenantDatabase  # noqa: E402
from app.orders.routes import confirm_order  # noqa: E402
from app.messages.service import upsert_inbound_message  # noqa: E402
from app.tenancy.migrations import upgrade_tenant_schema  # noqa: E402
from app.workers.jobs_worker import run_worker_cycle  # noqa: E402


class RegressionFlowsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        self.master_path = base / "master.sqlite"
        self.tenant_path = base / "tenant.sqlite"
        self.master_engine = create_engine(
            f"sqlite:///{self.master_path.as_posix()}",
            connect_args={"check_same_thread": False},
            poolclass=NullPool,
        )
        self.tenant_engine = create_engine(
            f"sqlite:///{self.tenant_path.as_posix()}",
            connect_args={"check_same_thread": False},
            poolclass=NullPool,
        )
        MasterBase.metadata.create_all(self.master_engine)
        Base.metadata.create_all(self.tenant_engine)
        self.MasterSession = sessionmaker(bind=self.master_engine, autoflush=False, autocommit=False)
        self.TenantSession = sessionmaker(bind=self.tenant_engine, autoflush=False, autocommit=False)

    def tearDown(self):
        self.master_engine.dispose()
        self.tenant_engine.dispose()
        gc.collect()
        self.tempdir.cleanup()

    def _seed_master(self):
        db = self.MasterSession()
        db.add(MasterCompany(id=1, name="Demo", slug="demo", active=True))
        db.add(MasterTenantDatabase(company_id=1, database_key="demo", database_url=f"sqlite:///{self.tenant_path.as_posix()}", is_active=True, health_status="ok"))
        db.commit()
        db.close()

    def _seed_tenant_settings(self):
        db = self.TenantSession()
        db.add(LLMSettings(company_id=1, provider="openai", api_key_encrypted=encrypt_secret("test-token")))
        db.add(
            ScoringSettings(
                company_id=1,
                safe_threshold=80,
                review_threshold=60,
                doubtful_threshold=40,
                blocked_threshold=39,
                block_without_customer=True,
                block_without_reference=True,
                block_without_quantity=True,
                block_below_threshold=True,
            )
        )
        db.add(FTPSettings(company_id=1, host="ftp.example.com", port=22, username="demo", destination_path="/exports"))
        db.commit()
        db.close()

    def _seed_customer_and_product(self):
        db = self.TenantSession()
        customer = Customer(
            company_id=1,
            code="C001",
            fiscal_name="Cliente Demo SL",
            commercial_name="Cliente Demo",
            primary_email="cliente@example.com",
            phone="600000000",
            status="active",
        )
        product = Product(
            company_id=1,
            reference="P-100",
            alternative_code="ALT-100",
            name="Producto Demo",
            description="Producto de prueba",
            sale_unit="uds",
            sale_price=10.0,
            status="active",
        )
        db.add_all(
            [
                customer,
                product,
                CustomerContactPoint(company_id=1, customer_id=1, type="email", value="cliente@example.com", is_primary=True, active=True),
                ProductAlias(company_id=1, product_id=1, alias="Producto demo"),
            ]
        )
        db.commit()
        db.close()

    def _upgrade_tenant_schema(self):
        upgrade_tenant_schema(self.tenant_engine, company_id=1, application_version="1.2.3")

    def _seed_user(self):
        return SimpleNamespace(id=1, company_id=1, role=SimpleNamespace(name="Administrador"))

    def test_mock_order_uses_tenant_company_name(self):
        db = self.TenantSession()
        try:
            db.add(Company(id=1, name="Tenant Prueba SL"))
            db.commit()

            MockAgentService().create_mock_order(db, 1)

            email = db.scalar(
                select(Email).where(
                    Email.company_id == 1,
                    Email.external_id.like("mock-1-%"),
                )
            )

            self.assertIsNotNone(email)
            self.assertIn("Tenant Prueba SL", email.extracted_text or "")
            self.assertNotIn("Anchi Demo", email.extracted_text or "")
            self.assertNotEqual(email.sender, "compras@anchi-demo.local")
        finally:
            db.close()

    def test_email_to_review_confirm_export_and_audit_flow(self):
        self._seed_master()
        self._seed_tenant_settings()
        self._seed_customer_and_product()
        self._upgrade_tenant_schema()
        db = self.TenantSession()
        email = Email(company_id=1, external_id="mail-e2e-1", sender="cliente@example.com", subject="Pedido demo", body="Necesitamos 5 unidades de P-100.")
        db.add(email)
        db.commit()

        channel = get_or_create_channel(db, 1, "email")
        channel.is_active = True
        db.commit()

        classification = json.dumps({"tipo_correo": "pedido", "confianza": 0.96, "motivo": "Pedido claro"}, ensure_ascii=False)
        extraction = json.dumps(
            {
                "cliente": {"nombre_detectado": "Cliente Demo SL", "codigo_cliente_detectado": "C001"},
                "pedido": {
                    "fecha_pedido": "2026-07-16",
                    "observaciones": "Pedido de ejemplo",
                    "lineas": [
                        {
                            "texto_original": "5 unidades de P-100",
                            "referencia_detectada": "P-100",
                            "producto_detectado": "Producto Demo",
                            "cantidad": 5,
                            "unidad": "uds",
                            "confianza_extraccion": 0.95,
                        }
                    ],
                },
            },
            ensure_ascii=False,
        )

        with patch("app.agent.platform.classify_sample", return_value={"ok": True, "content": classification}), patch(
            "app.agent.platform.extract_sample", return_value={"ok": True, "content": extraction}
        ):
            result = AgentProcessingService().process_email(db, email)

        self.assertTrue(result["ok"])
        self.assertIn("review_id", result)
        inbound = db.scalar(select(InboundMessage).where(InboundMessage.company_id == 1, InboundMessage.source_external_id == "mail-e2e-1"))
        self.assertIsNotNone(inbound)
        self.assertIsNotNone(inbound.conversation_id)
        conversation = db.get(Conversation, inbound.conversation_id)
        self.assertIsNotNone(conversation)
        order = db.scalar(select(Order).where(Order.id == result["order_id"]).options(selectinload(Order.lines)))
        self.assertIsNotNone(order.validated_customer_id)
        self.assertEqual(len(order.lines or []), 1)
        self.assertEqual(order.lines[0].validated_product_id, 1)

        user = self._seed_user()
        confirm_order(order.id, db=db, user=user)
        db.refresh(order)
        self.assertEqual(order.status, "pedido_confirmado")

        enqueue_job(db, company_id=1, job_type="export_order", payload={"order_id": order.id}, created_by_user_id=user.id)
        job = db.scalar(select(BackgroundJob).where(BackgroundJob.company_id == 1, BackgroundJob.job_type == "export_order").order_by(BackgroundJob.id.desc()))
        self.assertIsNotNone(job)

        with patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession), patch(
            "app.workers.jobs_worker.FTPService.send", return_value=True
        ):
            summary = run_worker_cycle()

        self.assertEqual(summary["tenants"], 1)
        db.refresh(order)
        self.assertEqual(order.status, "pedido_exportado")
        self.assertEqual(db.scalar(select(func.count()).select_from(ExportFile)) or 0, 1)
        actions = {row.action for row in db.scalars(select(AuditLog).where(AuditLog.company_id == 1)).all()}
        self.assertIn("agent.order_created", actions)
        self.assertIn("order.confirm", actions)
        self.assertIn("job.export_order.success", actions)
        db.close()

    def test_duplicate_mail_retry_and_export_retry_path(self):
        self._seed_master()
        self._seed_tenant_settings()
        self._seed_customer_and_product()
        self._upgrade_tenant_schema()
        db = self.TenantSession()
        first_message, _ = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="email",
            provider="imap",
            external_id="mail-dup-1",
            sender="cliente@example.com",
            recipients=["pedidos@example.com"],
            subject="Pedido duplicado",
            text_content="Pedido duplicado",
            external_thread_id="thread-dup",
            received_at=datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc),
            metadata={"message_id": "mail-dup-1"},
            content_type="email",
        )
        duplicate_message, _ = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="email",
            provider="imap",
            external_id="mail-dup-1",
            sender="cliente@example.com",
            recipients=["pedidos@example.com"],
            subject="Pedido duplicado",
            text_content="Pedido duplicado otra vez",
            external_thread_id="thread-dup",
            received_at=datetime(2026, 7, 16, 10, 1, tzinfo=timezone.utc),
            metadata={"message_id": "mail-dup-1"},
            content_type="email",
        )
        db.commit()
        self.assertEqual(first_message.id, duplicate_message.id)
        self.assertEqual(db.scalar(select(func.count()).select_from(InboundMessage)) or 0, 1)
        self.assertEqual(db.scalar(select(func.count()).select_from(Conversation)) or 0, 1)

        email = Email(company_id=1, external_id="mail-dup-1", sender="cliente@example.com", subject="Pedido duplicado", body="Pedido de 5 unidades de P-100")
        db.add(email)
        db.commit()

        with patch("app.agent.platform.classify_sample", return_value={"ok": True, "content": "no-json"}), patch(
            "app.agent.platform.extract_sample", return_value={"ok": True, "content": "no-json"}
        ):
            failed = AgentProcessingService().process_email(db, email)
        self.assertFalse(failed["ok"])
        self.assertEqual(db.get(Email, email.id).status, "error_processing")

        classification = json.dumps({"tipo_correo": "pedido", "confianza": 0.95, "motivo": "Pedido claro"}, ensure_ascii=False)
        extraction = json.dumps(
            {
                "cliente": {"nombre_detectado": "Cliente Demo SL", "codigo_cliente_detectado": "C001"},
                "pedido": {
                    "fecha_pedido": "2026-07-16",
                    "lineas": [
                        {
                            "texto_original": "5 unidades de P-100",
                            "referencia_detectada": "P-100",
                            "producto_detectado": "Producto Demo",
                            "cantidad": 5,
                            "unidad": "uds",
                            "confianza_extraccion": 0.93,
                        }
                    ],
                },
            },
            ensure_ascii=False,
        )

        with patch("app.agent.platform.classify_sample", return_value={"ok": True, "content": classification}), patch(
            "app.agent.platform.extract_sample", return_value={"ok": True, "content": extraction}
        ):
            retried = AgentProcessingService().process_email(db, email)

        self.assertTrue(retried["ok"])
        order = db.scalar(select(Order).where(Order.id == retried["order_id"]).options(selectinload(Order.lines)))
        user = self._seed_user()
        confirm_order(order.id, db=db, user=user)
        enqueue_job(db, company_id=1, job_type="export_order_ftp", payload={"order_id": order.id}, created_by_user_id=user.id)
        job = db.scalar(select(BackgroundJob).where(BackgroundJob.company_id == 1, BackgroundJob.job_type == "export_order_ftp").order_by(BackgroundJob.id.desc()))
        self.assertIsNotNone(job)

        with patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession), patch(
            "app.workers.jobs_worker.FTPService.send", side_effect=[TimeoutError("connection refused"), True]
        ):
            first_cycle = run_worker_cycle()

        self.assertEqual(first_cycle["tenants"], 1)
        db.refresh(job)
        self.assertEqual(job.status, "retrying")
        retried_job = retry_job(db, 1, job.id)
        self.assertIsNotNone(retried_job)

        with patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession), patch(
            "app.workers.jobs_worker.FTPService.send", return_value=True
        ):
            second_cycle = run_worker_cycle()

        self.assertEqual(second_cycle["tenants"], 1)
        db.refresh(order)
        self.assertEqual(order.status, "pedido_exportado")
        self.assertEqual(db.scalar(select(func.count()).select_from(ExportFile)) or 0, 1)
        db.close()


if __name__ == "__main__":
    unittest.main()
