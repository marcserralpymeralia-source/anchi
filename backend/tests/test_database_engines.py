from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.database import engine as tenant_runtime_engine
from app.master.database import engine as master_runtime_engine
from app.tenancy.database import get_tenant_engine


class DatabaseEngineRuntimeTests(unittest.TestCase):
    def tearDown(self):
        get_tenant_engine.cache_clear()

    def test_runtime_engines_use_pre_ping(self):
        self.assertTrue(master_runtime_engine.pool._pre_ping)
        self.assertTrue(tenant_runtime_engine.pool._pre_ping)

    def test_cached_tenant_engine_uses_pre_ping(self):
        engine = get_tenant_engine("postgresql://user:password@db.example.com:5432/tenant")
        self.assertTrue(engine.pool._pre_ping)

    def test_tenant_sqlite_engine_still_connects(self):
        with tempfile.TemporaryDirectory() as tempdir:
            database_url = f"sqlite:///{(Path(tempdir) / 'tenant.sqlite').as_posix()}"
            engine = get_tenant_engine(database_url)

            with engine.connect() as conn:
                self.assertEqual(conn.execute(text("SELECT 1")).scalar(), 1)

            self.assertTrue(engine.pool._pre_ping)
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
