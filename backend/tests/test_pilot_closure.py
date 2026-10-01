from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")

from app.core.backup import backup_database, restore_sqlite, verify_sqlite_backup  # noqa: E402
from app.core.operational import operational_status, record_heartbeat, utcnow  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import AuditLog, BackgroundJob, Company, ExternalIdentity, InputChannel, IntegrationConnection, Order, OrderDelivery, OrderRoute  # noqa: E402
from app.master.models import CompanyMembership, MasterCompany, MasterBase, MasterTenantDatabase, MasterUser, OperationalHeartbeat  # noqa: E402
from app.orders.delivery import retry_order_delivery  # noqa: E402
from app.superadmin.service import offboard_company  # noqa: E402


class PilotClosureTests(unittest.TestCase):
    def test_sqlite_backup_restore_preserves_pilot_invariants(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            master_source = root / "master-source.sqlite"
            tenant_source = root / "tenant-source.sqlite"
            master_restore = root / "master-restore.sqlite"
            tenant_restore = root / "tenant-restore.sqlite"
            master_engine = create_engine(f"sqlite:///{master_source}")
            tenant_engine = create_engine(f"sqlite:///{tenant_source}")
            MasterBase.metadata.create_all(master_engine)
            Base.metadata.create_all(tenant_engine)
            MasterSession = sessionmaker(bind=master_engine)
            TenantSession = sessionmaker(bind=tenant_engine)

            with MasterSession() as db:
                db.add_all(
                    [
                        MasterCompany(id=1, name="Pilot", slug="pilot", active=True, status="active"),
                        MasterUser(id=1, email="owner@example.test", full_name="Owner", password_hash="hash", is_active=True),
                        CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=True),
                        MasterTenantDatabase(company_id=1, database_key="pilot", database_url=f"sqlite:///{tenant_source}", is_active=True),
                    ]
                )
                db.commit()
            with TenantSession() as db:
                db.add_all(
                    [
                        Company(id=1, name="Pilot", active=True),
                        InputChannel(id=1, company_id=1, key="catalog", name="Catalog", is_active=True),
                        IntegrationConnection(id=1, company_id=1, connector_key="fake", name="Fake", state="ACTIVE", secret_provider_ref="secret://pilot/fake"),
                        OrderRoute(id=1, company_id=1, destination_connector_key="fake", destination_connection_id=1, is_active=True),
                        ExternalIdentity(id=1, company_id=1, entity_type="order", entity_id=1, external_system="fake", external_id="EXT-1"),
                        Order(id=1, company_id=1, source_external_event_id="evt-1", status="pedido_validado", export_status="ready_to_export"),
                        OrderDelivery(id=1, company_id=1, order_id=1, destination_connector_key="fake", destination_connection_id=1, idempotency_key="1/order/1/fake/1", status="failed", attempt_count=3, max_attempts=3),
                        OrderDelivery(id=2, company_id=1, order_id=1, destination_connector_key="fake", destination_connection_id=1, idempotency_key="1/order/1/fake/1/exported", status="exported", destination_external_order_id="EXT-ORDER-1"),
                        BackgroundJob(id=1, company_id=1, job_type="deliver_order", dedupe_key="order-delivery:1/order/1/fake/1", status="failed"),
                        AuditLog(id=1, company_id=1, action="order.export", entity_type="order_delivery", entity_id=2, message="exported"),
                    ]
                )
                db.commit()

            master_meta = backup_database(f"sqlite:///{master_source}", root / "master.backup.sqlite", environment="test", database_identity="master:test")
            tenant_meta = backup_database(f"sqlite:///{tenant_source}", root / "tenant.backup.sqlite", environment="test", database_identity="tenant:1")
            self.assertEqual(master_meta["engine"], "sqlite")
            self.assertEqual(tenant_meta["schema_status"], None)
            self.assertTrue(verify_sqlite_backup(root / "tenant.backup.sqlite")["ok"])
            restore_sqlite(root / "master.backup.sqlite", f"sqlite:///{master_restore}")
            restore_sqlite(root / "tenant.backup.sqlite", f"sqlite:///{tenant_restore}")

            restored_master_engine = create_engine(f"sqlite:///{master_restore}")
            restored_tenant_engine = create_engine(f"sqlite:///{tenant_restore}")
            restored_master = sessionmaker(bind=restored_master_engine)
            restored_tenant = sessionmaker(bind=restored_tenant_engine)
            with restored_master() as db:
                company = db.get(MasterCompany, 1)
                self.assertEqual(company.slug, "pilot")
                self.assertTrue(db.get(CompanyMembership, 1).is_active)
            with restored_tenant() as db:
                failed = db.get(OrderDelivery, 1)
                exported = db.get(OrderDelivery, 2)
                self.assertEqual(failed.idempotency_key, "1/order/1/fake/1")
                self.assertEqual(db.get(ExternalIdentity, 1).external_id, "EXT-1")
                retried = retry_order_delivery(db, company_id=1, delivery_id=1)
                self.assertEqual(retried.status, "queued")
                self.assertEqual(retried.idempotency_key, failed.idempotency_key)
                self.assertIsNone(retry_order_delivery(db, company_id=1, delivery_id=2))
                self.assertEqual(exported.destination_external_order_id, "EXT-ORDER-1")
                self.assertEqual(db.scalar(select(BackgroundJob.status).where(BackgroundJob.dedupe_key == "order-delivery:1/order/1/fake/1")), "queued")
                self.assertEqual(db.scalar(select(AuditLog.action).where(AuditLog.id == 1)), "order.export")
            master_engine.dispose()
            tenant_engine.dispose()
            restored_master_engine.dispose()
            restored_tenant_engine.dispose()

    def test_persistent_storage_survives_process_restart(self):
        with tempfile.TemporaryDirectory() as tempdir:
            env = os.environ.copy()
            env.update({"APP_ENV": "test", "PYTHONPATH": str(Path(__file__).resolve().parents[1]), "TEMP_STORAGE_DIR": tempdir, "VERCEL": "", "VERCEL_ENV": "", "BLOB_READ_WRITE_TOKEN": ""})
            save = subprocess.run(
                [sys.executable, "-c", "from app.core.attachment_storage import save_attachment; print(save_attachment(filename='persistent.txt', payload=b'persisted', company_id=1))"],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            storage_ref = save.stdout.strip()
            read = subprocess.run(
                [sys.executable, "-c", "import sys; from app.core.attachment_storage import read_attachment; assert read_attachment(sys.argv[1]) == b'persisted'; print('restored')", storage_ref],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(read.stdout.strip(), "restored")

    def test_storage_failure_is_visible_and_recovery_is_healthy(self):
        from unittest.mock import patch

        from app.core.storage import check_storage_readiness

        with tempfile.TemporaryDirectory() as tempdir:
            blocked = Path(tempdir) / "not-a-directory"
            blocked.write_text("block", encoding="utf-8")
            with patch.dict(os.environ, {"TEMP_STORAGE_DIR": str(blocked), "VERCEL": "", "VERCEL_ENV": ""}, clear=False):
                failed = check_storage_readiness()
            self.assertFalse(failed["ok"])
            with patch.dict(os.environ, {"TEMP_STORAGE_DIR": tempdir, "VERCEL": "", "VERCEL_ENV": ""}, clear=False):
                recovered = check_storage_readiness()
            self.assertTrue(recovered["ok"])

    def test_scheduler_status_distinguishes_healthy_and_stale(self):
        with tempfile.TemporaryDirectory() as tempdir:
            engine = create_engine(f"sqlite:///{Path(tempdir) / 'master.sqlite'}")
            MasterBase.metadata.create_all(engine)
            Session = sessionmaker(bind=engine)
            with Session() as db:
                record_heartbeat(db, component="worker", name="jobs", status="healthy", owner="test", success=True)
                record_heartbeat(db, component="scheduler", name="jobs", status="healthy", owner="test", success=True)
                status = operational_status(db)
                self.assertEqual(status["worker"]["status"], "healthy")
                self.assertEqual(status["scheduler"]["status"], "healthy")
                row = db.scalar(select(OperationalHeartbeat))
                self.assertIsNotNone(row)
                db.execute(
                    OperationalHeartbeat.__table__.update().values(
                        last_success_at=utcnow() - timedelta(minutes=10),
                        last_seen_at=utcnow() - timedelta(minutes=10),
                    )
                )
                db.commit()
                stale = operational_status(db)
                self.assertEqual(stale["worker"]["status"], "stale")
                self.assertEqual(stale["scheduler"]["status"], "stale")

    def test_offboarding_pauses_work_preserves_history_and_requires_external_revoke(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            master_engine = create_engine(f"sqlite:///{root / 'master.sqlite'}")
            tenant_engine = create_engine(f"sqlite:///{root / 'tenant.sqlite'}")
            MasterBase.metadata.create_all(master_engine)
            Base.metadata.create_all(tenant_engine)
            MasterSession = sessionmaker(bind=master_engine)
            TenantSession = sessionmaker(bind=tenant_engine)
            with MasterSession() as db:
                db.add_all([
                    MasterCompany(id=1, name="Offboard", slug="offboard", active=True, status="active", provisioning_status="ready"),
                    MasterUser(id=1, email="owner@example.test", full_name="Owner", password_hash="hash", is_active=True),
                    CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=True),
                    MasterTenantDatabase(company_id=1, database_key="offboard", database_url=f"sqlite:///{root / 'tenant.sqlite'}", is_active=True),
                ])
                db.commit()
                with TenantSession() as tenant_db:
                    tenant_db.add_all([
                        Company(id=1, name="Offboard", active=True),
                        InputChannel(id=1, company_id=1, key="catalog", name="Catalog", is_active=True),
                        IntegrationConnection(id=1, company_id=1, connector_key="fake", name="Fake", state="ACTIVE", secret_provider_ref="secret://offboard/fake"),
                        OrderRoute(id=1, company_id=1, destination_connector_key="fake", destination_connection_id=1, is_active=True),
                        Order(id=1, company_id=1, status="pending_review"),
                        OrderDelivery(id=1, company_id=1, order_id=1, destination_connector_key="fake", idempotency_key="offboard/1", status="queued"),
                        OrderDelivery(id=2, company_id=1, order_id=1, destination_connector_key="fake", idempotency_key="offboard/2", status="failed"),
                        BackgroundJob(id=1, company_id=1, job_type="deliver_order", dedupe_key="offboard/1", status="queued"),
                    ])
                    tenant_db.commit()
                result = offboard_company(db, 1, 1, reason="Fin de piloto sintético")
                self.assertEqual(result["status"], "offboarded")
                self.assertTrue(result["external_secret_revocation_required"])
                self.assertEqual(db.get(MasterCompany, 1).status, "offboarded")
                self.assertFalse(db.get(MasterTenantDatabase, 1).is_active)
                self.assertFalse(db.get(CompanyMembership, 1).is_active)
            with TenantSession() as db:
                self.assertFalse(db.get(Company, 1).active)
                self.assertFalse(db.get(InputChannel, 1).is_active)
                self.assertEqual(db.get(IntegrationConnection, 1).state, "DISABLED")
                self.assertFalse(db.get(OrderRoute, 1).is_active)
                self.assertEqual(db.get(BackgroundJob, 1).status, "paused")
                self.assertEqual(db.get(OrderDelivery, 1).status, "paused")
                self.assertEqual(db.get(OrderDelivery, 2).status, "failed")
                self.assertEqual(db.scalar(select(AuditLog.action).where(AuditLog.action == "tenant.offboard")), "tenant.offboard")


if __name__ == "__main__":
    unittest.main()
