from __future__ import annotations

import os
import sqlite3
import unittest
from pathlib import Path

from sqlalchemy import select

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")

from app.db.models import Customer, ExternalDatabaseConnection, ExternalDatabaseMapping
from app.external_databases.service import (
    scan_schema,
    sync_mapping,
    test_connection,
    validate_mapping_payload,
)
from tests.test_setup_onboarding import SetupFixture


class ExternalDatabaseTests(unittest.TestCase):
    def _create_source(self, root: str | Path) -> Path:
        source_path = Path(root) / "external-source.sqlite3"
        source = sqlite3.connect(source_path)
        try:
            source.execute(
                "CREATE TABLE customers_source (customer_code TEXT, legal_name TEXT, email TEXT)"
            )
            source.executemany(
                "INSERT INTO customers_source(customer_code, legal_name, email) VALUES (?, ?, ?)",
                [
                    ("C-001", "Cliente Uno", "uno@example.test"),
                    ("", "Fila sin código", "invalid@example.test"),
                    ("C-001", "Cliente duplicado", "duplicate@example.test"),
                    ("C-002", "Cliente Dos", "dos@example.test"),
                ],
            )
            source.commit()
        finally:
            source.close()
        return source_path

    def test_sqlite_read_only_scan_mapping_and_partial_sync(self):
        fixture = SetupFixture()
        try:
            source_path = self._create_source(fixture.tempdir.name)
            connection = ExternalDatabaseConnection(
                company_id=1,
                name="ERP demo",
                database_type="sqlite",
                host=":memory:",
                port=0,
                database_name=str(source_path),
                schema_name="main",
                username="",
                enabled=True,
                read_only=True,
            )
            mapping = ExternalDatabaseMapping(
                company_id=1,
                connection=connection,
                entity_type="customers",
                table_schema="main",
                table_name="customers_source",
                field_map_json='{"code": "customer_code", "fiscal_name": "legal_name", "primary_email": "email"}',
                sync_enabled=True,
                sync_limit=10,
            )
            with fixture.TenantSession() as db:
                db.add(connection)
                db.commit()
                db.refresh(connection)

                ok, message = test_connection(connection)
                self.assertTrue(ok, message)
                schema = scan_schema(connection)
                self.assertEqual(schema["schema"], "main")
                self.assertEqual([table["name"] for table in schema["tables"]], ["customers_source"])
                normalized = validate_mapping_payload(
                    "customers",
                    {"code": "customer_code", "fiscal_name": "legal_name", "primary_email": "email"},
                    schema,
                )
                self.assertEqual(normalized["code"], "customer_code")

                db.add(mapping)
                db.commit()
                result = sync_mapping(db, connection, mapping, company_id=1, actor_id=1)
                self.assertEqual(result["rows_read"], 4)
                self.assertEqual(result["created"], 2)
                self.assertEqual(result["skipped"], 1)
                self.assertEqual(result["errors"], 1)
                customers = db.scalars(
                    select(Customer).where(Customer.company_id == 1).order_by(Customer.code)
                ).all()
                self.assertEqual([customer.code for customer in customers], ["C-001", "C-002"])
        finally:
            fixture.cleanup()

    def test_settings_flow_persists_connection_toggle_scan_mapping_preview_and_sync(self):
        fixture = SetupFixture()
        client, cleanup = fixture.client()
        try:
            source_path = self._create_source(fixture.tempdir.name)
            login = client.post(
                "/login",
                data={"email": "admin@setup.local", "password": "setup-password"},
                follow_redirects=False,
            )
            self.assertEqual(login.status_code, 303)
            saved = client.post(
                "/settings/data-sources",
                data={
                    "name": "ERP de pruebas",
                    "database_type": "sqlite",
                    "host": ":memory:",
                    "port": "0",
                    "database_name": str(source_path),
                    "schema_name": "main",
                    "username": "",
                    "ssl_mode": "disable",
                    "enabled": "on",
                },
                follow_redirects=False,
            )
            self.assertEqual(saved.status_code, 303)
            with fixture.TenantSession() as db:
                connection = db.scalar(
                    select(ExternalDatabaseConnection).where(ExternalDatabaseConnection.name == "ERP de pruebas")
                )
                self.assertIsNotNone(connection)
                connection_id = connection.id

            module = client.get("/settings/module/data-sources")
            self.assertEqual(module.status_code, 200)
            self.assertIn('id="settings-data-sources"', module.text)
            self.assertIn("ERP de pruebas", module.text)

            tested = client.post(
                f"/settings/data-sources/{connection_id}/test",
                headers={"Accept": "application/json"},
            )
            self.assertEqual(tested.status_code, 200)
            self.assertTrue(tested.json()["ok"])

            scanned = client.post(
                f"/settings/data-sources/{connection_id}/scan",
                headers={"Accept": "application/json"},
            )
            self.assertEqual(scanned.status_code, 200)
            self.assertEqual(scanned.json()["schema"]["tables"][0]["name"], "customers_source")

            mapping = client.post(
                f"/settings/data-sources/{connection_id}/mapping",
                json={
                    "entity_type": "customers",
                    "table_schema": "main",
                    "table_name": "customers_source",
                    "field_map": {
                        "code": "customer_code",
                        "fiscal_name": "legal_name",
                        "primary_email": "email",
                    },
                    "sync_enabled": True,
                    "sync_limit": 10,
                },
                headers={"Accept": "application/json"},
            )
            self.assertEqual(mapping.status_code, 200)
            self.assertTrue(mapping.json()["mapping"]["sync_enabled"])

            preview = client.post(
                f"/settings/data-sources/{connection_id}/mapping/customers/preview",
                headers={"Accept": "application/json"},
            )
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.json()["count"], 4)

            synced = client.post(
                f"/settings/data-sources/{connection_id}/mapping/customers/sync",
                headers={"Accept": "application/json"},
            )
            self.assertEqual(synced.status_code, 200)
            self.assertEqual(synced.json()["result"]["created"], 2)

            toggled = client.post(
                f"/settings/data-sources/{connection_id}/toggle",
                headers={"Accept": "application/json"},
            )
            self.assertEqual(toggled.status_code, 200)
            self.assertFalse(toggled.json()["enabled"])
        finally:
            cleanup()
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
