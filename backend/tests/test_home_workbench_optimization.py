from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")
os.environ.setdefault("PERFORMANCE_PROFILING_ENABLED", "true")
os.environ.setdefault("ENABLE_PERFORMANCE_PROFILING", "true")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.performance_data import build_performance_fixture, performance_test_client  # noqa: E402


class HomeWorkbenchOptimizationTests(unittest.TestCase):
    def _assert_home_and_workbench_are_light(self, scenario: str, *, max_queries: int, max_duplicates: int) -> None:
        fixture = build_performance_fixture(scenario)
        try:
            with performance_test_client(fixture) as client:
                home_response = client.get("/")
                workbench_response = client.get("/workbench")

            self.assertEqual(home_response.status_code, 200)
            self.assertEqual(workbench_response.status_code, 200)
            self.assertLessEqual(int(home_response.headers["X-Perf-SQL-Count"]), max_queries)
            self.assertLessEqual(int(workbench_response.headers["X-Perf-SQL-Count"]), max_queries)
            self.assertLessEqual(int(home_response.headers["X-Perf-SQL-Duplicate-Count"]), max_duplicates)
            self.assertLessEqual(int(workbench_response.headers["X-Perf-SQL-Duplicate-Count"]), max_duplicates)
            self.assertNotIn("internal server error", home_response.text.lower())
            self.assertNotIn("internal server error", workbench_response.text.lower())
        finally:
            fixture.cleanup()

    def test_home_and_workbench_query_budget_small(self):
        self._assert_home_and_workbench_are_light("small", max_queries=12, max_duplicates=1)

    def test_home_and_workbench_query_budget_medium(self):
        self._assert_home_and_workbench_are_light("medium", max_queries=12, max_duplicates=1)

    def test_home_and_workbench_query_budget_large(self):
        self._assert_home_and_workbench_are_light("large", max_queries=12, max_duplicates=1)


if __name__ == "__main__":
    unittest.main()
