from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from sqlalchemy import select

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")

from app.db.models import FTPConnection, ProxyConnection
from tests.test_setup_onboarding import SetupFixture


class FTPSettingsTests(unittest.TestCase):
    def _login(self, client):
        return client.post(
            "/login",
            data={"email": "admin@setup.local", "password": "setup-password"},
            follow_redirects=False,
        )

    def test_ftp_supports_multiple_profiles_and_proxy_selection(self):
        fixture = SetupFixture()
        client, cleanup = fixture.client()
        try:
            self._login(client)
            with fixture.TenantSession() as db:
                proxy = ProxyConnection(
                    company_id=1,
                    name="Gateway FTP",
                    proxy_host="proxy.example.com",
                    proxy_port=443,
                    proxy_protocol="https",
                    tls_mode="verify",
                )
                db.add(proxy)
                db.commit()
                db.refresh(proxy)
                proxy_id = proxy.id

            first = client.post(
                "/settings/ftp",
                data={
                    "name": "Exportación principal",
                    "connection_type": "ftps_explicit",
                    "host": "ftp.example.com",
                    "port": "21",
                    "username": "exporter",
                    "password": "first-secret",
                    "destination_path": "/orders",
                    "retries": "3",
                    "timeout_seconds": "45",
                    "passive_mode": "on",
                    "proxy_connection_id": str(proxy_id),
                },
                follow_redirects=False,
            )
            self.assertEqual(first.status_code, 303)

            second = client.post(
                "/settings/ftp",
                data={
                    "name": "Backup FTP",
                    "connection_type": "ftp",
                    "host": "backup.example.com",
                    "port": "21",
                    "username": "backup",
                    "password": "second-secret",
                    "destination_path": "/backup",
                    "proxy_connection_id": str(proxy_id),
                },
                follow_redirects=False,
            )
            self.assertEqual(second.status_code, 303)

            with fixture.TenantSession() as db:
                connections = db.scalars(select(FTPConnection).order_by(FTPConnection.name)).all()
                self.assertEqual([connection.name for connection in connections], ["Backup FTP", "Exportación principal"])
                self.assertTrue(all(connection.proxy_connection_id == proxy_id for connection in connections))
                self.assertEqual(connections[1].destination_path, "/orders")
                self.assertNotEqual(connections[1].password_encrypted, "first-secret")
                first_id = connections[1].id
                second_id = connections[0].id

            view = client.get("/settings/module/ftp")
            self.assertEqual(view.status_code, 200)
            self.assertIn("Conexiones configuradas (2)", view.text)
            self.assertIn("Exportación principal", view.text)
            self.assertIn("Backup FTP", view.text)
            self.assertIn("Gateway FTP · proxy.example.com:443", view.text)
            self.assertIn(f"/settings/ftp/{first_id}/test", view.text)

            edited = client.post(
                "/settings/ftp",
                data={
                    "id": str(first_id),
                    "name": "Exportación principal editada",
                    "connection_type": "ftps_explicit",
                    "host": "ftp2.example.com",
                    "port": "21",
                    "username": "exporter",
                    "destination_path": "/orders-v2",
                    "retries": "2",
                    "timeout_seconds": "30",
                    "passive_mode": "on",
                    "proxy_connection_id": str(proxy_id),
                },
                follow_redirects=False,
            )
            self.assertEqual(edited.status_code, 303)

            with fixture.TenantSession() as db:
                connection = db.get(FTPConnection, first_id)
                self.assertEqual(connection.name, "Exportación principal editada")
                self.assertEqual(connection.host, "ftp2.example.com")
                self.assertEqual(connection.destination_path, "/orders-v2")

            with patch("app.exports.service.FTPService.test_connection", return_value=True):
                tested = client.post(
                    f"/settings/ftp/{first_id}/test",
                    headers={"Accept": "application/json"},
                )
            self.assertEqual(tested.status_code, 200)
            self.assertTrue(tested.json()["ok"])

            deleted = client.post(f"/settings/ftp/{second_id}/delete", follow_redirects=False)
            self.assertEqual(deleted.status_code, 303)
            with fixture.TenantSession() as db:
                self.assertIsNone(db.get(FTPConnection, second_id))
        finally:
            cleanup()
            fixture.cleanup()

    def test_ftp_rejects_proxy_from_another_company(self):
        fixture = SetupFixture()
        client, cleanup = fixture.client()
        try:
            self._login(client)
            with fixture.TenantSession() as db:
                proxy = ProxyConnection(
                    company_id=2,
                    name="Proxy ajeno",
                    proxy_host="other.example.com",
                    proxy_port=443,
                    proxy_protocol="https",
                    tls_mode="verify",
                )
                db.add(proxy)
                db.commit()
                db.refresh(proxy)

            response = client.post(
                "/settings/ftp",
                data={
                    "name": "FTP inválido",
                    "connection_type": "ftp",
                    "host": "ftp.example.com",
                    "port": "21",
                    "username": "exporter",
                    "password": "secret",
                    "proxy_connection_id": str(proxy.id),
                },
                headers={"Accept": "application/json"},
            )
            self.assertEqual(response.status_code, 422)
            self.assertIn("no pertenece", response.json()["message"])
        finally:
            cleanup()
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
