import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.tenancy.database as tenancy_database
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

    def test_same_url_reuses_cached_engine(self):
        with patch.object(tenancy_database, "create_engine", side_effect=lambda *args, **kwargs: Mock()) as create_engine:
            first = get_tenant_engine("sqlite:///tenant-a.sqlite")
            second = get_tenant_engine("sqlite:///tenant-a.sqlite")

        self.assertIs(first, second)
        create_engine.assert_called_once()

    def test_cache_is_bounded_and_evicts_least_recently_used_engine(self):
        created = {}

        def build_engine(database_url, **kwargs):
            engine = Mock(name=database_url)
            created[database_url] = engine
            return engine

        with patch.object(tenancy_database, "create_engine", side_effect=build_engine):
            urls = [f"sqlite:///tenant-{index}.sqlite" for index in range(tenancy_database.TENANT_ENGINE_CACHE_LIMIT + 1)]
            for url in urls[:-1]:
                get_tenant_engine(url)
            get_tenant_engine(urls[0])
            get_tenant_engine(urls[-1])

        self.assertLessEqual(len(tenancy_database._tenant_engine_cache), tenancy_database.TENANT_ENGINE_CACHE_LIMIT)
        self.assertNotIn(urls[1], tenancy_database._tenant_engine_cache)
        created[urls[1]].dispose.assert_called_once_with()
        self.assertIn(urls[0], tenancy_database._tenant_engine_cache)

    def test_clear_disposes_all_cached_engines(self):
        created = []

        def build_engine(*args, **kwargs):
            engine = Mock()
            created.append(engine)
            return engine

        with patch.object(tenancy_database, "create_engine", side_effect=build_engine):
            get_tenant_engine("sqlite:///tenant-a.sqlite")
            get_tenant_engine("sqlite:///tenant-b.sqlite")

        get_tenant_engine.cache_clear()

        self.assertFalse(tenancy_database._tenant_engine_cache)
        for engine in created:
            engine.dispose.assert_called_once_with()

    def test_same_url_is_created_once_under_concurrent_access(self):
        with patch.object(tenancy_database, "create_engine", side_effect=lambda *args, **kwargs: Mock()) as create_engine, ThreadPoolExecutor(max_workers=8) as pool:
            engines = list(pool.map(lambda _: get_tenant_engine("sqlite:///tenant-concurrent.sqlite"), range(16)))

        self.assertEqual(len({id(engine) for engine in engines}), 1)
        create_engine.assert_called_once()

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
