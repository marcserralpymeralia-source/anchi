from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from scripts.performance_data import build_performance_fixture, performance_test_client  # noqa: E402
from app.db.models import User  # noqa: E402
from app.master.models import CompanyMembership  # noqa: E402
from app.settings.branding import DEFAULT_THEME  # noqa: E402
from app.settings.service import resolve_updated_by_id  # noqa: E402


class BrandingCacheHttpTests(unittest.TestCase):
    def test_default_primary_color_matches_anchi_brand(self):
        self.assertEqual(DEFAULT_THEME["buttons"]["primary"], "#123A32")
        self.assertEqual(DEFAULT_THEME["buttons"]["primary_hover"], "#0B2924")

    def test_saved_primary_color_is_used_on_the_next_page_request(self):
        fixture = build_performance_fixture("small")
        master_engine = create_engine(fixture.master_database_url, connect_args={"check_same_thread": False})
        MasterSession = sessionmaker(bind=master_engine, autoflush=False, autocommit=False)
        try:
            with MasterSession() as db:
                membership = db.scalar(select(CompanyMembership).where(CompanyMembership.company_id == fixture.company_id))
                self.assertIsNotNone(membership)
                assert membership is not None
                membership.role_key = "Administrador"
                db.commit()

            with performance_test_client(fixture) as client:
                initial_page = client.get("/settings")
                self.assertEqual(initial_page.status_code, 200)

                saved = client.post(
                    "/settings/branding",
                    data={"theme.buttons.primary": "#ff0088"},
                    follow_redirects=False,
                )
                self.assertEqual(saved.status_code, 303)

                next_page = client.get("/")

            self.assertEqual(next_page.status_code, 200)
            self.assertIn("--accent:#ff0088;", next_page.text)
        finally:
            master_engine.dispose()
            fixture.cleanup()

    def test_branding_update_resolves_master_identity_to_tenant_user(self):
        fixture = build_performance_fixture("small")
        tenant_engine = create_engine(fixture.tenant_database_url, connect_args={"check_same_thread": False})
        TenantSession = sessionmaker(bind=tenant_engine, autoflush=False, autocommit=False)
        try:
            with TenantSession() as db:
                tenant_user = db.scalar(select(User).where(User.company_id == fixture.company_id))
                self.assertIsNotNone(tenant_user)
                assert tenant_user is not None

                master_identity = SimpleNamespace(
                    id=tenant_user.id + 1000,
                    email=tenant_user.email,
                    company_id=fixture.company_id,
                )

                self.assertEqual(resolve_updated_by_id(db, master_identity), tenant_user.id)
        finally:
            tenant_engine.dispose()
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
