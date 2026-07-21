from __future__ import annotations

import os
import unittest
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")
os.environ.setdefault("PERFORMANCE_PROFILING_ENABLED", "true")
os.environ.setdefault("ENABLE_PERFORMANCE_PROFILING", "true")

from app.db.models import AgentLog, Alert, BackgroundJob, Company, Email  # noqa: E402
from scripts.performance_data import build_performance_fixture, performance_test_client  # noqa: E402


def _tenant_session(tenant_path: Path):
    engine = create_engine(f"sqlite:///{tenant_path.as_posix()}", connect_args={"check_same_thread": False})
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


class ChannelsOptimizationTests(unittest.TestCase):
    def _assert_channels_budget(self, scenario: str, *, max_queries: int, max_duplicates: int, max_loaded_records: int, max_response_size: int) -> None:
        fixture = build_performance_fixture(scenario)
        try:
            with performance_test_client(fixture) as client:
                response = client.get("/channels?tab=processed&date_range=30d")
            self.assertEqual(response.status_code, 200)
            self.assertLessEqual(int(response.headers["X-Perf-SQL-Count"]), max_queries)
            self.assertLessEqual(int(response.headers["X-Perf-SQL-Duplicate-Count"]), max_duplicates)
            self.assertLessEqual(int(response.headers["X-Perf-Loaded-Records"]), max_loaded_records)
            self.assertLessEqual(int(response.headers["X-Perf-Response-Size-Bytes"]), max_response_size)
            self.assertNotIn("Internal Server Error", response.text)
            self.assertIn("Entradas recibidas", response.text)
        finally:
            fixture.cleanup()

    def test_channels_query_budget_small(self):
        self._assert_channels_budget("small", max_queries=20, max_duplicates=3, max_loaded_records=108, max_response_size=175_512)

    def test_channels_query_budget_medium(self):
        self._assert_channels_budget("medium", max_queries=20, max_duplicates=3, max_loaded_records=108, max_response_size=177_000)

    def test_channels_query_budget_large(self):
        self._assert_channels_budget("large", max_queries=20, max_duplicates=3, max_loaded_records=108, max_response_size=205_000)

    def test_channels_tabs_filters_and_pagination(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                responses = {
                    "pending": client.get("/channels?tab=pending&date_range=30d"),
                    "processed": client.get("/channels?tab=processed&date_range=30d"),
                    "error": client.get("/channels?tab=error&date_range=30d"),
                    "7d": client.get("/channels?tab=processed&date_range=7d"),
                    "30d": client.get("/channels?tab=processed&date_range=30d"),
                    "90d": client.get("/channels?tab=processed&date_range=90d"),
                    "page1": client.get("/channels?tab=processed&date_range=30d&page=1&page_size=10"),
                    "page2": client.get("/channels?tab=processed&date_range=30d&page=2&page_size=10"),
                    "empty": client.get("/channels?tab=processed&date_range=30d&search=__no_results__"),
                }

            for key, response in responses.items():
                self.assertEqual(response.status_code, 200, key)
                self.assertNotIn("Internal Server Error", response.text)

            self.assertIn("No hay entradas para mostrar", responses["empty"].text)
            self.assertLessEqual(int(responses["page1"].headers["X-Perf-Displayed-Items"]), 10)
            self.assertLessEqual(int(responses["page2"].headers["X-Perf-Displayed-Items"]), 10)
            self.assertGreaterEqual(int(responses["processed"].headers["X-Perf-Displayed-Items"]), 1)
            self.assertGreaterEqual(int(responses["pending"].headers["X-Perf-Displayed-Items"]), 0)
            self.assertGreaterEqual(int(responses["error"].headers["X-Perf-Displayed-Items"]), 0)
            self.assertNotEqual(responses["page1"].text, responses["page2"].text)
            self.assertIn("Revisión", responses["processed"].text)
        finally:
            fixture.cleanup()

    def test_channels_ignores_other_tenant_records(self):
        fixture = build_performance_fixture("small")
        SessionLocal = _tenant_session(fixture.tenant_path)
        try:
            with SessionLocal() as db:
                db.add(Company(id=999, name="Tenant sombra", legal_name="Tenant sombra", active=True, default_language="es", language="es", timezone="Europe/Madrid"))
                db.add(
                    Email(
                        company_id=999,
                        sender="shadow@example.com",
                        subject="NO_DEBE_APARECER_EN_CHANNELS",
                        body="Texto del tenant sombra",
                    )
                )
                db.commit()

            with performance_test_client(fixture) as client:
                response = client.get("/channels?tab=all&date_range=all")

            self.assertEqual(response.status_code, 200)
            self.assertNotIn("NO_DEBE_APARECER_EN_CHANNELS", response.text)
            self.assertIn("Entradas recibidas", response.text)
        finally:
            fixture.cleanup()

    def test_channels_get_does_not_write(self):
        fixture = build_performance_fixture("small")
        SessionLocal = _tenant_session(fixture.tenant_path)
        try:
            with SessionLocal() as db:
                before = {
                    "alerts": db.scalar(select(func.count()).select_from(Alert)),
                    "jobs": db.scalar(select(func.count()).select_from(BackgroundJob)),
                    "logs": db.scalar(select(func.count()).select_from(AgentLog)),
                }

            with performance_test_client(fixture) as client:
                response = client.get("/channels?tab=processed&date_range=30d")

            with SessionLocal() as db:
                after = {
                    "alerts": db.scalar(select(func.count()).select_from(Alert)),
                    "jobs": db.scalar(select(func.count()).select_from(BackgroundJob)),
                    "logs": db.scalar(select(func.count()).select_from(AgentLog)),
                }

            self.assertEqual(response.status_code, 200)
            self.assertEqual(before, after)
        finally:
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
