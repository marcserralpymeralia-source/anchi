from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.pilot_configuration import build_tenant_configuration_manifest, compare_runtime_to_approved
from app.master.database import MasterBase
from app.master.models import MasterCompany, MasterTenantDatabase
from scripts import pilot_configuration_manifest as manifest_cli


class _Result:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class _TenantSession:
    def __init__(self, connections, routes):
        self.results = [_Result(connections), _Result(routes)]

    def scalars(self, _query):
        return self.results.pop(0)

    def close(self):
        pass


class _MasterSession:
    def close(self):
        pass


class PilotConfigurationManifestCliTests(unittest.TestCase):
    runtime_env = {
        "APP_ENV": "production",
        "PILOT_PROFILE": "CATALOG_ONLY_PILOT",
        "PILOT_SCHEDULED_JOB_PROFILE": "CATALOG_ONLY_PILOT",
        "PILOT_ENABLED_MODULES": "whatsapp_catalog,generic_rest",
        "PILOT_DISABLED_MODULES": "email_autodiscovery,audio,sage_real,odoo_real",
        "TENANT_DB_MODE": "external",
        "PILOT_COMMIT_SHA": "966ba518904825f3602ab168c7cec81008e5c466",
        "ATTACHMENT_STORAGE_BACKEND": "vercel_blob",
        "BLOB_STORE_HOST": "pilot.private.blob.vercel-storage.com",
        "BLOB_READ_WRITE_TOKEN": "blob-value-must-never-appear",
        "CRON_SECRET": "cron-value-must-never-appear",
        "PILOT_CRON_ENDPOINT_URL": "https://pilot.onrender.com/cron/jobs",
        "PILOT_CRON_ALLOWED_HOST": "pilot.onrender.com",
        "PILOT_SECRET_PROVIDER": "environment",
        "ANCHI_SECRET_TENANT_42_CONNECTION_7_API_KEY": "rest-value-must-never-appear",
    }

    def test_tenant_manifest_is_deterministic_sanitized_and_baseline_compatible(self):
        connection = SimpleNamespace(
            id=7,
            connector_key="generic_rest",
            state="ACTIVE",
            config_json=json.dumps({
                "base_url": "https://sandbox.example.test",
                "api_key": "api-key-value-must-never-appear",
                "password": "password-value-must-never-appear",
                "database_url": "postgresql://user:pass@db.example.test/pilot",
                "contact_email": "pii-value-must-never-appear@example.test",
            }),
        )
        route = SimpleNamespace(
            id=9,
            order_type="catalog",
            source_connection_id=3,
            destination_connection_id=7,
            destination_connector_key="generic_rest",
            is_active=True,
        )
        master_report = {"status": "current", "current_version": "master-v1"}
        tenant_report = {"status": "current", "current_version": "tenant-v1"}
        with patch.dict(os.environ, self.runtime_env, clear=True):
            first = build_tenant_configuration_manifest(
                company_id=42,
                master_schema_report=master_report,
                tenant_schema_report=tenant_report,
                connections=[connection],
                routes=[route],
            )
            second = build_tenant_configuration_manifest(
                company_id=42,
                master_schema_report=master_report,
                tenant_schema_report=tenant_report,
                connections=[connection],
                routes=[route],
            )

        encoded = json.dumps(first, sort_keys=True)
        self.assertEqual(first, second)
        self.assertEqual(first["hash"], second["hash"])
        self.assertEqual(first["tenant_id"], 42)
        self.assertEqual(first["schema_version"], "master:master-v1;tenant:tenant-v1")
        for forbidden in (
            "blob-value-must-never-appear",
            "cron-value-must-never-appear",
            "rest-value-must-never-appear",
            "api-key-value-must-never-appear",
            "password-value-must-never-appear",
            "postgresql://user:pass@db.example.test/pilot",
            "pii-value-must-never-appear@example.test",
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(compare_runtime_to_approved(first, json.dumps(first))["status"], "MATCH")
        changed = {**first, "storage_backend": "filesystem"}
        drift = compare_runtime_to_approved(changed, json.dumps(first))
        self.assertEqual(drift["status"], "DRIFT")
        self.assertIn("storage_backend", {item["field"] for item in drift["critical"]})

    def test_cli_selects_explicit_company_and_uses_readiness_builder(self):
        tenant_mapping = SimpleNamespace(database_url="postgresql://never-output.invalid/pilot")
        tenant_db = _TenantSession(
            [SimpleNamespace(id=1, connector_key="whatsapp_catalog", state="ACTIVE", config_json="{}")],
            [SimpleNamespace(
                id=2,
                order_type="catalog",
                source_connection_id=1,
                destination_connection_id=3,
                destination_connector_key="generic_rest",
                is_active=True,
            )],
        )
        master_db = _MasterSession()
        with patch.dict(os.environ, self.runtime_env, clear=True), \
             patch.object(manifest_cli, "_resolve_tenant", return_value=tenant_mapping) as resolve, \
             patch.object(manifest_cli, "master_migration_report", return_value={"status": "current", "current_version": "m1"}), \
             patch.object(manifest_cli, "tenant_migration_report", return_value={"status": "current", "current_version": "t1"}), \
             patch.object(manifest_cli, "tenant_db_session", return_value=lambda: tenant_db) as tenant_session:
            manifest = manifest_cli.generate_manifest(42, master_session_factory=lambda: master_db)

        resolve.assert_called_once_with(master_db, 42)
        tenant_session.assert_called_once_with(tenant_mapping.database_url)
        self.assertEqual(manifest["tenant_id"], 42)
        self.assertEqual(manifest["connection_fingerprints"][0]["connection_id"], 1)
        self.assertEqual(manifest["routes"][0]["route_id"], 2)

    def test_missing_tenant_fails_closed(self):
        with patch.dict(os.environ, self.runtime_env, clear=True), \
             patch.object(manifest_cli, "_resolve_tenant", side_effect=manifest_cli.ManifestGenerationError(
                 "No existe un tenant activo con base configurada para ese company_id."
             )), \
             patch.object(manifest_cli, "master_migration_report", return_value={"status": "current"}):
            with self.assertRaises(manifest_cli.ManifestGenerationError):
                manifest_cli.generate_manifest(404, master_session_factory=_MasterSession)

    def test_tenant_resolver_selects_company_id_and_rejects_missing_or_inactive(self):
        engine = create_engine("sqlite:///:memory:")
        MasterBase.metadata.create_all(engine)
        try:
            with Session(engine) as db:
                first = MasterCompany(name="Synthetic One", slug="synthetic-one", active=True, status="active")
                second = MasterCompany(name="Synthetic Two", slug="synthetic-two", active=True, status="active")
                inactive = MasterCompany(name="Synthetic Off", slug="synthetic-off", active=False, status="active")
                db.add_all([first, second, inactive])
                db.flush()
                db.add_all([
                    MasterTenantDatabase(company_id=first.id, database_key="synthetic-one", database_url="sqlite:///tenant-one.db", is_active=True),
                    MasterTenantDatabase(company_id=second.id, database_key="synthetic-two", database_url="sqlite:///tenant-two.db", is_active=True),
                    MasterTenantDatabase(company_id=inactive.id, database_key="synthetic-off", database_url="sqlite:///tenant-off.db", is_active=True),
                ])
                db.flush()

                selected = manifest_cli._resolve_tenant(db, second.id)
                self.assertEqual(selected.company_id, second.id)
                for company_id in (99999, inactive.id):
                    with self.subTest(company_id=company_id), self.assertRaises(manifest_cli.ManifestGenerationError):
                        manifest_cli._resolve_tenant(db, company_id)
        finally:
            engine.dispose()

    def test_invalid_profile_fails_closed(self):
        with patch.dict(os.environ, {**self.runtime_env, "PILOT_SCHEDULED_JOB_PROFILE": "wrong"}, clear=True):
            with self.assertRaisesRegex(manifest_cli.ManifestGenerationError, "Catalog-only"):
                manifest_cli.generate_manifest(42, master_session_factory=_MasterSession)

    def test_cli_stdout_is_only_manifest_json(self):
        from app.core.pilot_configuration import PilotConfigurationManifest

        manifest = PilotConfigurationManifest(tenant_id=42).build(commit_sha=self.runtime_env["PILOT_COMMIT_SHA"])
        stdout = io.StringIO()
        with patch.object(manifest_cli, "generate_manifest", return_value=manifest), contextlib.redirect_stdout(stdout):
            result = manifest_cli.main(["--company-id", "42"])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout.getvalue()), manifest)


if __name__ == "__main__":
    unittest.main()
