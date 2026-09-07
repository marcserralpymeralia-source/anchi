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

    def test_single_active_proxy_invariant_on_create_and_edit(self):
        fixture = SetupFixture()
        client, cleanup = fixture.client()
        try:
            self._login(client)

            # 1. Create first proxy with enabled=True
            res1 = client.post(
                "/settings/proxies",
                data={
                    "name": "Proxy Primario",
                    "proxy_host": "proxy1.example.com",
                    "proxy_port": "8080",
                    "proxy_protocol": "https",
                    "tls_mode": "verify",
                    "enabled": "on",
                },
                follow_redirects=False,
            )
            self.assertEqual(res1.status_code, 303)

            with fixture.TenantSession() as db:
                p1 = db.scalar(select(ProxyConnection).where(ProxyConnection.name == "Proxy Primario"))
                self.assertIsNotNone(p1)
                self.assertTrue(p1.enabled)
                p1_id = p1.id

            # 2. Create second proxy with enabled=True
            res2 = client.post(
                "/settings/proxies",
                data={
                    "name": "Proxy Secundario",
                    "proxy_host": "proxy2.example.com",
                    "proxy_port": "8081",
                    "proxy_protocol": "http",
                    "tls_mode": "verify",
                    "enabled": "on",
                },
                follow_redirects=False,
            )
            self.assertEqual(res2.status_code, 303)

            with fixture.TenantSession() as db:
                p1 = db.get(ProxyConnection, p1_id)
                p2 = db.scalar(select(ProxyConnection).where(ProxyConnection.name == "Proxy Secundario"))
                self.assertIsNotNone(p2)
                self.assertTrue(p2.enabled)
                # p1 must be deactivated automatically!
                self.assertFalse(p1.enabled)
                p2_id = p2.id

            # 3. Edit p1 to re-enable it
            res3 = client.post(
                "/settings/proxies",
                data={
                    "id": str(p1_id),
                    "name": "Proxy Primario Reeditado",
                    "proxy_host": "proxy1.example.com",
                    "proxy_port": "8080",
                    "proxy_protocol": "https",
                    "tls_mode": "verify",
                    "enabled": "on",
                },
                follow_redirects=False,
            )
            self.assertEqual(res3.status_code, 303)

            with fixture.TenantSession() as db:
                p1 = db.get(ProxyConnection, p1_id)
                p2 = db.get(ProxyConnection, p2_id)
                self.assertTrue(p1.enabled)
                self.assertFalse(p2.enabled)

        finally:
            cleanup()
            fixture.cleanup()

    def test_proxy_toggle_endpoint_and_view_rendering(self):
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
                    enabled=True,
                )
                p2 = ProxyConnection(
                    company_id=1,
                    name="Proxy Beta",
                    proxy_host="beta.proxy.test",
                    proxy_port=3129,
                    proxy_protocol="http",
                    tls_mode="disabled",
                    enabled=False,
                )
                db.add_all([p1, p2])
                db.commit()
                db.refresh(p1)
                db.refresh(p2)
                p1_id, p2_id = p1.id, p2.id

            # Verify view shows Proxy Alpha as active
            view1 = client.get("/settings/module/proxies")
            self.assertEqual(view1.status_code, 200)
            self.assertIn("Proxy activo", view1.text)
            self.assertIn("Proxy Alpha", view1.text)
            self.assertIn("Activar (único)", view1.text)  # Button on Beta

            # Toggle Beta ON
            toggle_beta = client.post(f"/settings/proxies/{p2_id}/toggle", follow_redirects=False)
            self.assertEqual(toggle_beta.status_code, 303)

            with fixture.TenantSession() as db:
                p1 = db.get(ProxyConnection, p1_id)
                p2 = db.get(ProxyConnection, p2_id)
                self.assertFalse(p1.enabled)
                self.assertTrue(p2.enabled)

            # Toggle Beta OFF (now 0 active proxies)
            toggle_beta_off = client.post(
                f"/settings/proxies/{p2_id}/toggle",
                headers={"Accept": "application/json"},
            )
            self.assertEqual(toggle_beta_off.status_code, 200)
            self.assertFalse(toggle_beta_off.json()["enabled"])

            with fixture.TenantSession() as db:
                p1 = db.get(ProxyConnection, p1_id)
                p2 = db.get(ProxyConnection, p2_id)
                self.assertFalse(p1.enabled)
                self.assertFalse(p2.enabled)

            # Verify view shows Direct Connection (no active proxy)
            view2 = client.get("/settings/module/proxies")
            self.assertEqual(view2.status_code, 200)
            self.assertIn("Conexión directa", view2.text)
            self.assertIn("Sin proxy activo", view2.text)

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

            # Try to toggle -> should be 403 Forbidden
            toggle_res = client.post(f"/settings/proxies/{proxy_id}/toggle")
            self.assertEqual(toggle_res.status_code, 403)

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
