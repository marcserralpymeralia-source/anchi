from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.security import hash_password
from app.auth.lifecycle import accept_invitation, create_invitation, create_password_reset_token, reset_password
from app.master.database import MasterBase
from app.master.models import CompanyMembership, MasterUser
from app.master.service import authenticate_master_user
from app.superadmin.service import create_company
from app.tenancy.database import clear_tenant_engine_cache, clear_tenant_schema_cache


class AccountLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.master_engine = create_engine(f"sqlite:///{Path(self.temp_dir.name, 'master.sqlite').as_posix()}", connect_args={"check_same_thread": False})
        MasterBase.metadata.create_all(self.master_engine)
        self.Session = sessionmaker(bind=self.master_engine, autoflush=False, autocommit=False)
        with self.Session() as db:
            db.add(MasterUser(id=1, email="root@example.test", full_name="Root", password_hash=hash_password("RootPassword123!"), is_active=True, platform_role_key="superadmin"))
            db.commit()
        self.tenant_url = f"sqlite:///{Path(self.temp_dir.name, 'tenant.sqlite').as_posix()}"

    def tearDown(self):
        clear_tenant_engine_cache()
        clear_tenant_schema_cache()
        self.master_engine.dispose()
        self.temp_dir.cleanup()

    def test_invitation_acceptance_and_password_reset_revoke_sessions(self):
        with self.Session() as db:
            company = create_company(
                db,
                name="Lifecycle Co",
                slug="lifecycle-co",
                database_url=self.tenant_url,
                actor_user_id=1,
            )
            self.assertEqual(db.scalar(select(CompanyMembership).where(CompanyMembership.company_id == company.id)), None)
            invitation, raw_token = create_invitation(
                db,
                company_id=company.id,
                email="new@lifecycle.test",
                role_key="Operador",
                invited_by_user_id=1,
            )
            accepted = accept_invitation(db, raw_token=raw_token, full_name="New Operator", password="NewPassword123!")
            self.assertEqual(accepted.email, "new@lifecycle.test")
            self.assertIsNotNone(invitation.accepted_at)
            membership = db.scalar(select(CompanyMembership).where(CompanyMembership.user_id == accepted.id, CompanyMembership.company_id == company.id))
            self.assertEqual(membership.role_key, "Operador")

            platform_invitation, platform_token = create_invitation(
                db,
                company_id=company.id,
                email="root@example.test",
                role_key="Administrador",
                invited_by_user_id=1,
            )
            with self.assertRaisesRegex(ValueError, "identidad global"):
                accept_invitation(db, raw_token=platform_token, full_name="Root", password="RootPassword123!")
            self.assertIsNone(platform_invitation.accepted_at)

            old_version = accepted.session_version
            reset_token = create_password_reset_token(db, email=accepted.email)
            reset_password(db, raw_token=reset_token, password="ResetPassword123!")
            db.refresh(accepted)
            self.assertEqual(accepted.session_version, old_version + 1)
            self.assertIsNone(authenticate_master_user(db, accepted.email, "NewPassword123!"))
            self.assertIsNotNone(authenticate_master_user(db, accepted.email, "ResetPassword123!"))


if __name__ == "__main__":
    unittest.main()
