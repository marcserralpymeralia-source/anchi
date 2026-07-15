from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "development")

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.security import hash_password  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import BackgroundJob, Customer, Email, JobAttempt, Order, OrderLine, TenantSchemaMigration  # noqa: E402
from app.jobs.service import enqueue_job  # noqa: E402
from app.master.database import MasterBase  # noqa: E402
from app.master.migrations import CURRENT_MASTER_SCHEMA_CHECKSUM, CURRENT_MASTER_SCHEMA_NAME, CURRENT_MASTER_SCHEMA_VERSION, master_migration_report, upgrade_master_schema  # noqa: E402
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser  # noqa: E402
from app.migrations.inspection import discover_sqlite_files, inspect_database_url, inventory_records, simulate_sqlite_reference  # noqa: E402
from app.migrations.helpers import table_exists  # noqa: E402
from app.migrations.registry import CURRENT_TENANT_SCHEMA_CHECKSUM, CURRENT_TENANT_SCHEMA_NAME, CURRENT_TENANT_SCHEMA_VERSION  # noqa: E402
from app.tenancy.migrations import tenant_migration_report, upgrade_tenant_schema  # noqa: E402
from app.workers.jobs_worker import run_worker_cycle  # noqa: E402


class SchemaMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        self.master_path = base / "master.sqlite"
        self.tenant_path = base / "tenant.sqlite"
        self.simulation_root = base / "simulations"
        self.master_engine = create_engine(f"sqlite:///{self.master_path.as_posix()}", connect_args={"check_same_thread": False})
        self.tenant_engine = create_engine(f"sqlite:///{self.tenant_path.as_posix()}", connect_args={"check_same_thread": False})
        self.MasterSession = sessionmaker(bind=self.master_engine, autoflush=False, autocommit=False)
        self.TenantSession = sessionmaker(bind=self.tenant_engine, autoflush=False, autocommit=False)

    def tearDown(self):
        self.master_engine.dispose()
        self.tenant_engine.dispose()
        self.tempdir.cleanup()

    def _create_tables_without_ledger(self, engine, base_metadata):  # noqa: ANN001
        tables = [table for name, table in base_metadata.tables.items() if name != "schema_migrations"]
        base_metadata.create_all(engine, tables=tables)

    def _seed_master_catalog(self, tenant_url: str) -> None:
        self._create_tables_without_ledger(self.master_engine, MasterBase.metadata)
        db = self.MasterSession()
        db.add_all(
            [
                MasterCompany(id=1, name="Demo", slug="demo", active=True),
                MasterUser(id=1, email="admin@anchi.local", full_name="Admin Demo", password_hash=hash_password("admin123"), is_active=True),
                CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=True),
                MasterTenantDatabase(company_id=1, database_key="demo", database_url=tenant_url, is_active=True, health_status="ok"),
            ]
        )
        db.commit()
        db.close()

    def _seed_current_tenant_without_ledger(self, *, with_jobs: bool = False) -> None:
        self._create_tables_without_ledger(self.tenant_engine, Base.metadata)
        db = self.TenantSession()
        db.add(Customer(company_id=1, code="C001", fiscal_name="Cliente Uno", primary_email="uno@example.com"))
        if with_jobs:
            job = BackgroundJob(company_id=1, job_type="process_email", dedupe_key="dedupe-1", status="queued", payload_json='{"email_id": 1}', progress=0)
            db.add(job)
            db.flush()
            db.add(JobAttempt(company_id=1, job_id=job.id, attempt_number=1, worker_id="worker-a", status="running"))
        db.commit()
        db.close()

    def _seed_legacy_tenant_schema(self) -> None:
        with self.tenant_engine.begin() as conn:
            conn.execute(text("CREATE TABLE customers (id INTEGER PRIMARY KEY, company_id INTEGER, code VARCHAR(50), fiscal_name VARCHAR(255))"))
            conn.execute(text("CREATE TABLE background_jobs (id INTEGER PRIMARY KEY, company_id INTEGER, job_type VARCHAR(80), dedupe_key VARCHAR(255), status VARCHAR(40), payload_json TEXT)"))
            conn.execute(text("INSERT INTO customers (id, company_id, code, fiscal_name) VALUES (1, 1, 'C001', 'Legacy Client')"))
            conn.execute(text("INSERT INTO background_jobs (id, company_id, job_type, dedupe_key, status, payload_json) VALUES (1, 1, 'process_email', 'dup-key', 'queued', '{}')"))

    def _seed_unknown_schema(self) -> None:
        with self.tenant_engine.begin() as conn:
            conn.execute(text("CREATE TABLE misc (id INTEGER PRIMARY KEY, label TEXT)"))
            conn.execute(text("INSERT INTO misc (id, label) VALUES (1, 'orphan')"))

    def _seed_checksum_mismatch(self) -> None:
        self._create_tables_without_ledger(self.tenant_engine, Base.metadata)
        with self.tenant_engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE schema_migrations (
                        id INTEGER PRIMARY KEY,
                        company_id INTEGER UNIQUE,
                        version VARCHAR(80),
                        name VARCHAR(180),
                        checksum VARCHAR(120),
                        execution_ms INTEGER DEFAULT 0,
                        application_version VARCHAR(80),
                        status VARCHAR(30),
                        applied_at DATETIME,
                        last_checked_at DATETIME,
                        last_error TEXT,
                        notes TEXT,
                        created_at DATETIME,
                        updated_at DATETIME
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO schema_migrations
                        (id, company_id, version, name, checksum, execution_ms, application_version, status, applied_at, last_checked_at, last_error, notes, created_at, updated_at)
                    VALUES
                        (1, 1, :version, :name, :checksum, 0, '1.2.3', 'current', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """
                ),
                {
                    "version": CURRENT_TENANT_SCHEMA_VERSION,
                    "name": CURRENT_TENANT_SCHEMA_NAME,
                    "checksum": "wrong-checksum",
                },
            )

    def test_inventory_detects_master_and_tenant_sources(self):
        self._seed_current_tenant_without_ledger()
        self._seed_master_catalog(f"sqlite:///{self.tenant_path.as_posix()}")

        db = self.MasterSession()
        try:
            items = inventory_records(db, Path(__file__).resolve().parents[2])
        finally:
            db.close()

        logical_names = {item["logical_name"] for item in items}
        self.assertIn("master", logical_names)
        self.assertIn("tenant:demo", logical_names)
        self.assertTrue(any(item["source"] == "filesystem" for item in items))

    def test_inspection_classifies_current_legacy_unknown_and_checksum_mismatch(self):
        self._seed_current_tenant_without_ledger()
        current = inspect_database_url(f"sqlite:///{self.tenant_path.as_posix()}", logical_name="tenant-current")
        self.assertEqual(current["classification"], "current-without-ledger")
        self.assertTrue(current["baseline_safe"])

        self.tenant_engine.dispose()
        self.tenant_path.unlink()
        self.tenant_engine = create_engine(f"sqlite:///{self.tenant_path.as_posix()}", connect_args={"check_same_thread": False})
        self.TenantSession = sessionmaker(bind=self.tenant_engine, autoflush=False, autocommit=False)
        self._seed_legacy_tenant_schema()
        legacy = inspect_database_url(f"sqlite:///{self.tenant_path.as_posix()}", logical_name="tenant-legacy")
        self.assertEqual(legacy["classification"], "legacy-recognized")
        self.assertTrue(legacy["baseline_safe"])

        self.tenant_engine.dispose()
        self.tenant_path.unlink()
        self.tenant_engine = create_engine(f"sqlite:///{self.tenant_path.as_posix()}", connect_args={"check_same_thread": False})
        self.TenantSession = sessionmaker(bind=self.tenant_engine, autoflush=False, autocommit=False)
        self._seed_unknown_schema()
        unknown = inspect_database_url(f"sqlite:///{self.tenant_path.as_posix()}", logical_name="tenant-unknown")
        self.assertEqual(unknown["classification"], "unknown-schema")
        self.assertFalse(unknown["baseline_safe"])

        self.tenant_engine.dispose()
        self.tenant_path.unlink()
        self.tenant_engine = create_engine(f"sqlite:///{self.tenant_path.as_posix()}", connect_args={"check_same_thread": False})
        self.TenantSession = sessionmaker(bind=self.tenant_engine, autoflush=False, autocommit=False)
        self._seed_checksum_mismatch()
        mismatch = inspect_database_url(f"sqlite:///{self.tenant_path.as_posix()}", logical_name="tenant-mismatch")
        self.assertEqual(mismatch["classification"], "checksum-mismatch")
        self.assertFalse(mismatch["readiness"] == "ready")

    def test_dry_run_does_not_write_tenant_copy(self):
        self._seed_current_tenant_without_ledger(with_jobs=True)
        self.assertTrue(self.tenant_path.exists())
        before_tables = set(inspect_database_url(f"sqlite:///{self.tenant_path.as_posix()}")["tables"])
        before_jobs = self._count_table("background_jobs")
        before_attempts = self._count_table("job_attempts")

        result = upgrade_tenant_schema(self.tenant_engine, company_id=1, application_version="1.2.3", dry_run=True)

        self.assertTrue(result["dry_run"])
        after_tables = set(inspect_database_url(f"sqlite:///{self.tenant_path.as_posix()}")["tables"])
        self.assertEqual(before_tables, after_tables)
        self.assertEqual(before_jobs, self._count_table("background_jobs"))
        self.assertEqual(before_attempts, self._count_table("job_attempts"))
        self.assertFalse(table_exists(self.tenant_engine, "schema_migrations"))

    def test_upgrade_copy_preserves_counts_and_second_run_is_noop(self):
        self._seed_current_tenant_without_ledger(with_jobs=True)
        before_counts = self._snapshot_counts(self.tenant_engine, ["customers", "background_jobs", "job_attempts"])

        first = upgrade_tenant_schema(self.tenant_engine, company_id=1, application_version="1.2.3")
        self.assertEqual(first["current_version"], CURRENT_TENANT_SCHEMA_VERSION)
        self.assertTrue(first["is_current"])

        after_first = self._snapshot_counts(self.tenant_engine, ["customers", "background_jobs", "job_attempts"])
        self.assertEqual(before_counts, after_first)

        second = upgrade_tenant_schema(self.tenant_engine, company_id=1, application_version="1.2.3")
        self.assertTrue(second["is_current"])
        self.assertEqual(second["applied_versions"], [])
        self.assertEqual(after_first, self._snapshot_counts(self.tenant_engine, ["customers", "background_jobs", "job_attempts"]))

        db = self.TenantSession()
        try:
            report = tenant_migration_report(db, 1)
        finally:
            db.close()
        self.assertTrue(report["is_current"])
        self.assertEqual(report["version"], CURRENT_TENANT_SCHEMA_VERSION)

    def test_upgrade_master_copy_preserves_state(self):
        self._create_tables_without_ledger(self.master_engine, MasterBase.metadata)
        db = self.MasterSession()
        db.add_all(
            [
                MasterCompany(id=1, name="Demo", slug="demo", active=True),
                MasterUser(id=1, email="admin@anchi.local", full_name="Admin Demo", password_hash=hash_password("admin123"), is_active=True),
                CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=True),
                MasterTenantDatabase(company_id=1, database_key="demo", database_url=f"sqlite:///{self.tenant_path.as_posix()}", is_active=True, health_status="ok"),
            ]
        )
        db.commit()
        db.close()

        before_users = self._count_master("users")
        before_companies = self._count_master("companies")
        result = upgrade_master_schema(self.master_engine, application_version="1.2.3")
        self.assertTrue(result["is_current"])
        self.assertEqual(before_users, self._count_master("users"))
        self.assertEqual(before_companies, self._count_master("companies"))
        inspection = inspect_database_url(f"sqlite:///{self.master_path.as_posix()}", logical_name="master-copy")
        self.assertEqual(inspection["classification"], "versioned-current")
        self.assertTrue(inspection["baseline_safe"])

    def test_duplicate_jobs_are_blocked_by_inspection(self):
        with self.tenant_engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE background_jobs (
                        id INTEGER PRIMARY KEY,
                        company_id INTEGER,
                        job_type VARCHAR(80),
                        dedupe_key VARCHAR(255),
                        status VARCHAR(40),
                        payload_json TEXT,
                        progress INTEGER DEFAULT 0,
                        attempt_count INTEGER DEFAULT 0,
                        retry_count INTEGER DEFAULT 0,
                        max_retries INTEGER DEFAULT 3,
                        queued_at DATETIME,
                        created_at DATETIME,
                        updated_at DATETIME
                    )
                    """
                )
            )
            conn.execute(text("INSERT INTO background_jobs (id, company_id, job_type, dedupe_key, status, payload_json, progress, attempt_count, retry_count, max_retries, queued_at, created_at, updated_at) VALUES (1, 1, 'process_email', 'dup', 'queued', '{}', 0, 0, 0, 3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
            conn.execute(text("INSERT INTO background_jobs (id, company_id, job_type, dedupe_key, status, payload_json, progress, attempt_count, retry_count, max_retries, queued_at, created_at, updated_at) VALUES (2, 1, 'process_email', 'dup', 'queued', '{}', 0, 0, 0, 3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        report = inspect_database_url(f"sqlite:///{self.tenant_path.as_posix()}", logical_name="tenant-duplicates")
        self.assertEqual(report["classification"], "blocked-by-data")
        self.assertIn("background_jobs duplicated dedupe_key", report["blockers"])

    def test_worker_skips_incompatible_schema_without_processing_jobs(self):
        self._create_tables_without_ledger(self.master_engine, MasterBase.metadata)
        db = self.MasterSession()
        db.add_all(
            [
                MasterCompany(id=1, name="Demo", slug="demo", active=True),
                MasterUser(id=1, email="admin@anchi.local", full_name="Admin Demo", password_hash=hash_password("admin123"), is_active=True),
                CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=True),
                MasterTenantDatabase(company_id=1, database_key="demo", database_url=f"sqlite:///{self.tenant_path.as_posix()}", is_active=True, health_status="ok"),
            ]
        )
        db.commit()
        db.close()

        self._seed_current_tenant_without_ledger()
        tenant_db = self.TenantSession()
        tenant_db.add(BackgroundJob(company_id=1, job_type="process_email", dedupe_key="worker-skip", status="queued", payload_json='{"email_id": 1}', progress=0))
        tenant_db.commit()
        tenant_db.close()

        calls = {"count": 0}

        def fake_process(*args, **kwargs):  # noqa: ANN001
            calls["count"] += 1
            raise AssertionError("The worker should not process jobs when the schema is not current.")

        with patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession), patch("app.agent.platform.UnifiedOrderPipelineService.process_inbound_message", new=fake_process):
            summary = run_worker_cycle()

        self.assertEqual(summary["blocked"], 1)
        self.assertEqual(summary["processed"], 0)
        self.assertEqual(calls["count"], 0)
        tenant_db = self.TenantSession()
        try:
            queued = tenant_db.scalar(select(func.count()).select_from(BackgroundJob))
        finally:
            tenant_db.close()
        self.assertEqual(queued, 1)

    def test_simulation_runs_on_copy_without_touching_original(self):
        self._seed_current_tenant_without_ledger(with_jobs=True)
        source_before = inspect_database_url(f"sqlite:///{self.tenant_path.as_posix()}", logical_name="source")
        self.assertEqual(source_before["classification"], "current-without-ledger")

        result = simulate_sqlite_reference(self.tenant_path, self.simulation_root, label="tenant", kind_hint="tenant", company_id=1, application_version="1.2.3")
        self.assertTrue(Path(result["copy_path"]).exists())
        self.assertTrue(result["dry_run"]["dry_run"])
        self.assertIsNotNone(result["baseline"])
        self.assertIsNotNone(result["upgrade"])
        self.assertIsNotNone(result["second_run"])

        source_after = inspect_database_url(f"sqlite:///{self.tenant_path.as_posix()}", logical_name="source")
        self.assertEqual(source_after["classification"], "current-without-ledger")
        self.assertFalse(table_exists(self.tenant_engine, "schema_migrations"))

    def _snapshot_counts(self, engine, tables: list[str]) -> dict[str, int]:  # noqa: ANN001
        counts: dict[str, int] = {}
        with engine.connect() as conn:
            for table in tables:
                if table in Base.metadata.tables or table in MasterBase.metadata.tables:
                    try:
                        counts[table] = int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
                    except Exception:  # noqa: BLE001
                        counts[table] = -1
                else:
                    counts[table] = -1
        return counts

    def _count_table(self, table_name: str) -> int:
        with self.tenant_engine.connect() as conn:
            try:
                return int(conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one())
            except Exception:
                return -1

    def _count_master(self, table_name: str) -> int:
        with self.master_engine.connect() as conn:
            try:
                return int(conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one())
            except Exception:
                return -1


if __name__ == "__main__":
    unittest.main()
