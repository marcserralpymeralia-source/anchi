from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.master.database import MasterBase
from app.master.migrations import upgrade_master_schema
from app.master.models import MasterCompany, MasterTenantDatabase, TenantProvisioningRun
from app.tenancy.migrations import upgrade_tenant_schema
from app.core.schema_readiness import RuntimeSchemaError, validate_runtime_schema


class RuntimeSchemaReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.master_engine = create_engine(f"sqlite:///{(root / 'master.sqlite').as_posix()}")
        self.tenant_engine = create_engine(f"sqlite:///{(root / 'tenant.sqlite').as_posix()}")
        self.MasterSession = sessionmaker(bind=self.master_engine, autoflush=False, autocommit=False)
        self.TenantSession = sessionmaker(bind=self.tenant_engine, autoflush=False, autocommit=False)

    def tearDown(self):
        self.master_engine.dispose()
        self.tenant_engine.dispose()
        self.tempdir.cleanup()

    def _prepare_current_schemas(self):
        MasterBase.metadata.create_all(self.master_engine)
        upgrade_master_schema(self.master_engine, application_version="test")
        Base.metadata.create_all(self.tenant_engine)
        upgrade_tenant_schema(self.tenant_engine, company_id=1, application_version="test")
        with self.MasterSession() as db:
            db.add(MasterCompany(id=1, name="Synthetic", slug="synthetic", active=True))
            db.add(MasterTenantDatabase(
                company_id=1,
                database_key="synthetic",
                database_url=str(self.tenant_engine.url),
                database_type="sqlite",
                is_active=True,
            ))
            db.commit()

    def _validate(self):
        return validate_runtime_schema(
            master_session_factory=self.MasterSession,
            tenant_session_factory=lambda _url: self.TenantSession,
        )

    def test_current_master_and_tenant_schema_are_checked_without_ddl(self):
        self._prepare_current_schemas()
        statements: list[str] = []

        def collect(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.lstrip().lower())

        event.listen(self.master_engine, "before_cursor_execute", collect)
        event.listen(self.tenant_engine, "before_cursor_execute", collect)
        try:
            result = self._validate()
        finally:
            event.remove(self.master_engine, "before_cursor_execute", collect)
            event.remove(self.tenant_engine, "before_cursor_execute", collect)

        self.assertEqual(result["master"], "current")
        self.assertEqual(result["tenants"], {1: "current"})
        self.assertFalse(any(statement.startswith(("create ", "alter ", "drop ")) for statement in statements))

    def test_missing_master_schema_fails_closed_without_creating_tables(self):
        Path(self.master_engine.url.database).touch()
        with self.assertRaisesRegex(RuntimeSchemaError, "master.*missing"):
            self._validate()
        from sqlalchemy import inspect

        self.assertEqual(inspect(self.master_engine).get_table_names(), [])

    def test_missing_tenant_schema_fails_closed_without_creating_database(self):
        MasterBase.metadata.create_all(self.master_engine)
        upgrade_master_schema(self.master_engine, application_version="test")
        tenant_path = Path(self.tempdir.name) / "not-created.sqlite"
        with self.MasterSession() as db:
            db.add(MasterCompany(id=1, name="Synthetic", slug="synthetic", active=True))
            db.add(MasterTenantDatabase(
                company_id=1,
                database_key="synthetic",
                database_url=f"sqlite:///{tenant_path.as_posix()}",
                database_type="sqlite",
                is_active=True,
            ))
            db.commit()
        with self.assertRaisesRegex(RuntimeSchemaError, "tenant company_id=1 database file is missing"):
            self._validate()
        self.assertFalse(tenant_path.exists())

    def test_stale_tenant_schema_fails_closed(self):
        self._prepare_current_schemas()
        from app.db.models import TenantSchemaMigration

        with self.TenantSession() as db:
            migration = db.scalar(select(TenantSchemaMigration).where(TenantSchemaMigration.company_id == 1))
            migration.checksum = "stale-checksum"
            db.commit()

        with self.assertRaisesRegex(RuntimeSchemaError, "tenant.*1.*outdated"):
            self._validate()

    def test_pending_inactive_tenant_does_not_block_runtime_before_operator_migration(self):
        self._prepare_current_schemas()
        pending_path = Path(self.tempdir.name) / "pending-tenant.sqlite"
        with self.MasterSession() as db:
            db.add(MasterCompany(id=2, name="Pending", slug="pending", active=False, status="provisioning"))
            db.add(MasterTenantDatabase(
                company_id=2,
                database_key="pending",
                database_url=f"sqlite:///{pending_path.as_posix()}",
                database_type="sqlite",
                is_active=True,
            ))
            db.commit()

        result = self._validate()

        self.assertEqual(result["tenants"], {1: "current"})
        self.assertFalse(pending_path.exists())

    def test_provisioning_waits_for_operator_migration_without_creating_tenant_db(self):
        self._prepare_current_schemas()
        pending_path = Path(self.tempdir.name) / "pending-tenant.sqlite"
        with self.MasterSession() as db:
            company = MasterCompany(id=2, name="Pending", slug="pending", active=False, status="provisioning")
            db.add(company)
            db.add(MasterTenantDatabase(
                company_id=2,
                database_key="pending",
                database_url=f"sqlite:///{pending_path.as_posix()}",
                database_type="sqlite",
                is_active=True,
            ))
            run = TenantProvisioningRun(company_id=2, operation="create", status="pending", current_step="queued")
            db.add(run)
            db.commit()
            run_id = run.id

        from app.workers import provisioning_worker
        with patch.object(provisioning_worker, "MasterSessionLocal", side_effect=self.MasterSession):
            self.assertFalse(provisioning_worker._provision_run(run_id))

        with self.MasterSession() as db:
            run = db.get(TenantProvisioningRun, run_id)
            self.assertEqual(run.status, "retry")
            self.assertEqual(run.current_step, "schema")
            self.assertEqual(run.error_message, "tenant_schema_not_current")
        self.assertFalse(pending_path.exists())

    def test_catalog_worker_cycle_skips_company_provisioning(self):
        from app.workers import jobs_worker

        fake_master = type("Master", (), {"scalars": lambda _self, *_args, **_kwargs: type("Rows", (), {"all": lambda _self: []})(), "close": lambda _self: None})()
        with patch("app.workers.jobs_worker.pilot_profile_state", return_value="catalog"), patch(
            "app.workers.jobs_worker.MasterSessionLocal", return_value=fake_master
        ), patch("app.workers.jobs_worker.record_heartbeat"), patch(
            "app.workers.provisioning_worker.run_provisioning_cycle"
        ) as provision:
            jobs_worker.run_worker_cycle(include_provisioning=True)

        provision.assert_not_called()

    def test_standard_worker_cycle_keeps_data_only_company_provisioning(self):
        from app.workers import jobs_worker

        fake_master = type("Master", (), {"scalars": lambda _self, *_args, **_kwargs: type("Rows", (), {"all": lambda _self: []})(), "close": lambda _self: None})()
        with patch("app.workers.jobs_worker.pilot_profile_state", return_value="standard"), patch(
            "app.workers.jobs_worker.MasterSessionLocal", return_value=fake_master
        ), patch("app.workers.jobs_worker.record_heartbeat"), patch(
            "app.workers.provisioning_worker.run_provisioning_cycle", return_value={"claimed": 0, "completed": 0, "failed": 0}
        ) as provision:
            jobs_worker.run_worker_cycle(include_provisioning=True)

        provision.assert_called_once()

    def test_web_lifespan_validates_current_schema_without_ddl(self):
        import asyncio
        from fastapi import FastAPI
        from app.core import lifespan

        self._prepare_current_schemas()
        statements: list[str] = []

        def collect(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.lstrip().lower())

        event.listen(self.master_engine, "before_cursor_execute", collect)
        event.listen(self.tenant_engine, "before_cursor_execute", collect)
        settings = SimpleNamespace(environment="test", run_internal_email_worker=False, run_internal_job_worker=False)
        try:
            with patch.object(lifespan, "validate_runtime_schema", side_effect=self._validate) as gate, patch.object(
                lifespan, "get_settings", return_value=settings
            ):
                async def enter_lifespan():
                    async with lifespan.app_lifespan(FastAPI()):
                        return "started"

                self.assertEqual(asyncio.run(enter_lifespan()), "started")
                gate.assert_called_once_with()
        finally:
            event.remove(self.master_engine, "before_cursor_execute", collect)
            event.remove(self.tenant_engine, "before_cursor_execute", collect)

        self.assertFalse(any(statement.startswith(("create ", "alter ", "drop ")) for statement in statements))

    def test_web_and_worker_startup_fail_closed_when_schema_is_stale(self):
        import asyncio
        from fastapi import FastAPI
        from app.core import lifespan
        from app.workers import jobs_worker

        failure = RuntimeSchemaError("tenant schema is not current (company_id=1, outdated)")
        with patch.object(lifespan, "validate_runtime_schema", side_effect=failure), self.assertRaises(RuntimeSchemaError):
            async def enter_lifespan():
                async with lifespan.app_lifespan(FastAPI()):
                    return "should not start"

            asyncio.run(enter_lifespan())

        stop_event = __import__("threading").Event()
        with patch.object(jobs_worker, "validate_runtime_schema", side_effect=failure), patch.object(
            jobs_worker, "run_worker_cycle"
        ) as run_cycle, self.assertRaises(RuntimeSchemaError):
            jobs_worker._worker_loop(stop_event)
        run_cycle.assert_not_called()

    def test_pool_settings_are_bounded_and_sqlite_uses_dialect_defaults(self):
        from app.core.config import Settings
        from app.db.pool import runtime_pool_options

        settings = Settings(_env_file=None, DATABASE_POOL_SIZE=3, DATABASE_MAX_OVERFLOW=1, DATABASE_POOL_TIMEOUT_SECONDS=10)
        self.assertEqual(runtime_pool_options("postgresql://db.example/pilot", settings), {
            "pool_size": 3,
            "max_overflow": 1,
            "pool_timeout": 10,
            "pool_pre_ping": True,
        })
        self.assertEqual(runtime_pool_options("sqlite:///:memory:", settings), {"pool_pre_ping": True})
        self.assertLessEqual(2 * 2 * (settings.database_pool_size + settings.database_max_overflow), 112)

    def test_migration_cli_override_must_target_same_database_without_same_user(self):
        from scripts import schema_migrations
        from scripts.schema_migrations import _same_database_target, _migration_engine
        from unittest.mock import Mock

        runtime = "postgresql://runtime@db.example:5432/anchi_tenant"
        migrator = "postgresql+psycopg://migrator@db.example:5432/anchi_tenant"
        other_database = "postgresql://migrator@db.example:5432/production"
        self.assertTrue(_same_database_target(runtime, migrator))
        self.assertFalse(_same_database_target(runtime, other_database))

        fallback = Mock()
        with patch.dict("os.environ", {"APP_ENV": "staging"}, clear=True):
            with self.assertRaisesRegex(ValueError, "MASTER_MIGRATION_DATABASE_URL is required"):
                _migration_engine("MASTER_MIGRATION_DATABASE_URL", runtime, fallback)

        with patch.dict("os.environ", {"APP_ENV": "staging", "MASTER_MIGRATION_DATABASE_URL": "postgresql://runtime@db.example:5432/anchi_tenant"}, clear=True):
            with self.assertRaisesRegex(ValueError, "different database identity"):
                _migration_engine("MASTER_MIGRATION_DATABASE_URL", runtime, fallback)

        with patch.dict("os.environ", {"APP_ENV": "staging", "MASTER_MIGRATION_DATABASE_URL": migrator}, clear=True), patch.object(
            schema_migrations, "create_engine", return_value=object()
        ) as create_migration_engine:
            engine, dispose = _migration_engine("MASTER_MIGRATION_DATABASE_URL", runtime, fallback)
        self.assertTrue(dispose)
        create_migration_engine.assert_called_once()

        with patch.dict("os.environ", {"ENVIRONMENT": "production"}, clear=True):
            with self.assertRaisesRegex(ValueError, "upgrade-all-tenants is disabled"):
                schema_migrations.cmd_upgrade_all_tenants(SimpleNamespace())


if __name__ == "__main__":
    unittest.main()
