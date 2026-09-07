from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")

from app.core.security import hash_password
from app.db.models import Company, ProxyConnection, Role, User
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser
from tests.test_setup_onboarding import SetupFixture


class ProxySettingsTests(unittest.TestCase):
    def _login(self, client, email="admin@setup.local", password="setup-password"):
        return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)

    def test_proxy_profiles_are_not_selected_globally(self):
        fixture = SetupFixture()
        client, cleanup = fixture.client()
        try:
            self._login(client)

            res1 = client.post(
                "/settings/proxies",
                data={
                    "name": "Proxy Primario",
                    "proxy_host": "proxy1.example.com",
                    "proxy_port": "8080",
                    "proxy_protocol": "https",
                    "tls_mode": "verify",
                },
                follow_redirects=False,
            )
            self.assertEqual(res1.status_code, 303)

            with fixture.TenantSession() as db:
                p1 = db.scalar(select(ProxyConnection).where(ProxyConnection.name == "Proxy Primario"))
                self.assertIsNotNone(p1)
                p1_id = p1.id

            res2 = client.post(
                "/settings/proxies",
                data={
                    "name": "Proxy Secundario",
                    "proxy_host": "proxy2.example.com",
                    "proxy_port": "8081",
                    "proxy_protocol": "http",
                    "tls_mode": "verify",
                },
                follow_redirects=False,
            )
            self.assertEqual(res2.status_code, 303)

            with fixture.TenantSession() as db:
                p1 = db.get(ProxyConnection, p1_id)
                p2 = db.scalar(select(ProxyConnection).where(ProxyConnection.name == "Proxy Secundario"))
                self.assertIsNotNone(p2)

            res3 = client.post(
                "/settings/proxies",
                data={
                    "id": str(p1.id),
                    "name": "Proxy Primario Reeditado",
                    "proxy_host": "proxy1.example.com",
                    "proxy_port": "8080",
                    "proxy_protocol": "https",
                    "tls_mode": "verify",
                },
                follow_redirects=False,
            )
            self.assertEqual(res3.status_code, 303)

            view = client.get("/settings/module/proxies")
            self.assertEqual(view.status_code, 200)
            self.assertIn("Proxy Primario Reeditado", view.text)
            self.assertIn("Proxy Secundario", view.text)
            self.assertNotIn("Gateway de Anchi", view.text)
            self.assertNotIn("Perfil activo", view.text)
            self.assertNotIn("Activar", view.text)
            self.assertNotIn("Desactivar", view.text)
            self.assertNotIn("Perfil habilitado", view.text)

        finally:
            cleanup()
            fixture.cleanup()

    def test_proxy_profiles_view_keeps_profile_actions(self):
        fixture = SetupFixture()
        client, cleanup = fixture.client()
        try:
            self._login(client)

            with fixture.TenantSession() as db:
                p1 = ProxyConnection(
                    company_id=1,
                    name="Proxy Alpha",
                    proxy_host="alpha.proxy.test",
                    proxy_port=3128,
                    proxy_protocol="https",
                    tls_mode="verify",
                )
                p2 = ProxyConnection(
                    company_id=1,
                    name="Proxy Beta",
                    proxy_host="beta.proxy.test",
                    proxy_port=3129,
                    proxy_protocol="http",
                    tls_mode="disabled",
                )
                db.add_all([p1, p2])
                db.commit()
                db.refresh(p1)
                db.refresh(p2)
                p1_id = p1.id

            view1 = client.get("/settings/module/proxies")
            self.assertEqual(view1.status_code, 200)
            self.assertIn("Proxy Alpha", view1.text)
            self.assertIn("Proxy Beta", view1.text)
            self.assertIn("Probar conexión", view1.text)
            self.assertIn("Editar", view1.text)
            self.assertIn(f"/settings/proxies/{p1_id}/test", view1.text)

        finally:
            cleanup()
            fixture.cleanup()

    def test_proxy_delete_endpoint(self):
        fixture = SetupFixture()
        client, cleanup = fixture.client()
        try:
            self._login(client)

            with fixture.TenantSession() as db:
                p = ProxyConnection(
                    company_id=1,
                    name="Proxy Temporal",
                    proxy_host="temp.proxy.test",
                    proxy_port=8080,
                    proxy_protocol="https",
                    tls_mode="verify",
                    enabled=False,
                )
                db.add(p)
                db.commit()
                db.refresh(p)
                p_id = p.id

            # Delete via POST
            del_res = client.post(f"/settings/proxies/{p_id}/delete", follow_redirects=False)
            self.assertEqual(del_res.status_code, 303)

            with fixture.TenantSession() as db:
                deleted = db.get(ProxyConnection, p_id)
                self.assertIsNone(deleted)

            # Deleting non-existent returns 404
            del_404 = client.post(f"/settings/proxies/{p_id}/delete")
            self.assertEqual(del_404.status_code, 404)

        finally:
            cleanup()
            fixture.cleanup()

    def test_proxy_actions_unauthorized_and_tenant_isolation(self):
        fixture = SetupFixture()
        client, cleanup = fixture.client()
        try:
            # Create operator user without admin role
            with fixture.MasterSession() as db:
                op_master = MasterUser(
                    id=2,
                    email="operator@setup.local",
                    full_name="Operador Test",
                    password_hash=hash_password("operator-password"),
                    is_active=True,
                )
                op_membership = CompanyMembership(
                    id=2,
                    user_id=2,
                    company_id=1,
                    role_key="Operador",
                    is_active=True,
                    is_owner=False,
                )
                db.add_all([op_master, op_membership])
                db.commit()

            with fixture.TenantSession() as db:
                op_role = Role(id=2, company_id=1, name="Operador", permissions="")
                op_user = User(
                    id=2,
                    company_id=1,
                    role_id=2,
                    email="operator@setup.local",
                    name="Operador Test",
                    password_hash=hash_password("operator-password"),
                    is_active=True,
                )
                proxy = ProxyConnection(
                    company_id=1,
                    name="Proxy Seguro",
                    proxy_host="safe.proxy.test",
                    proxy_port=8080,
                    proxy_protocol="https",
                    tls_mode="verify",
                    enabled=True,
                )
                db.add_all([op_role, op_user, proxy])
                db.commit()
                db.refresh(proxy)
                proxy_id = proxy.id

            # Login as operator
            self._login(client, email="operator@setup.local", password="operator-password")

            # Try to delete -> should be 403 Forbidden
            delete_res = client.post(f"/settings/proxies/{proxy_id}/delete")
            self.assertEqual(delete_res.status_code, 403)

            # Try to save new proxy -> should be 403 Forbidden
            save_res = client.post(
                "/settings/proxies",
                data={
                    "name": "Hack Proxy",
                    "proxy_host": "hack.proxy.test",
                    "proxy_port": "8080",
                    "proxy_protocol": "https",
                    "tls_mode": "verify",
                },
            )
            self.assertEqual(save_res.status_code, 403)

        finally:
            cleanup()
            fixture.cleanup()
