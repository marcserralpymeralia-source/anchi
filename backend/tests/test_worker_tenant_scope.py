from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import BackgroundJob, Company
from app.jobs.service import claim_next_job, recover_stale_jobs


class WorkerTenantScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.temp_dir.name, 'shared.sqlite').as_posix()}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        with self.Session() as db:
            db.add_all([Company(id=1, name="Alpha", active=True), Company(id=2, name="Beta", active=True)])
            db.add_all(
                [
                    BackgroundJob(company_id=1, job_type="process_email", dedupe_key="alpha", status="queued", payload_json="{}"),
                    BackgroundJob(company_id=2, job_type="process_email", dedupe_key="beta", status="queued", payload_json="{}"),
                ]
            )
            db.commit()

    def tearDown(self):
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_claim_and_recovery_are_scoped_to_company(self):
        with self.Session() as db:
            claimed_alpha = claim_next_job(db, owner="worker-alpha", company_id=1, job_types={"process_email"})
            self.assertIsNotNone(claimed_alpha)
            self.assertEqual(claimed_alpha.company_id, 1)

            claimed_beta = claim_next_job(db, owner="worker-beta", company_id=2, job_types={"process_email"})
            self.assertIsNotNone(claimed_beta)
            self.assertEqual(claimed_beta.company_id, 2)

            claimed_alpha.lock_until = datetime.now(timezone.utc).replace(year=2020)
            claimed_alpha.last_heartbeat_at = datetime.now(timezone.utc).replace(year=2020)
            db.commit()
            recovered_beta = recover_stale_jobs(db, owner="worker-beta", company_id=2, job_types={"process_email"})
            self.assertEqual(recovered_beta, [])


if __name__ == "__main__":
    unittest.main()
