from __future__ import annotations

import os
import unittest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")
os.environ.setdefault("PERFORMANCE_PROFILING_ENABLED", "true")
os.environ.setdefault("ENABLE_PERFORMANCE_PROFILING", "true")

from scripts.performance_data import build_performance_fixture, performance_test_client  # noqa: E402


class OrdersDetailOptimizationTests(unittest.TestCase):
    def _assert_order_detail_is_light(self, scenario: str, *, max_queries: int, max_duplicates: int, max_loaded_records: int, max_response_size: int) -> None:
        fixture = build_performance_fixture(scenario)
        try:
            with performance_test_client(fixture) as client:
                response = client.get("/orders/1")

            self.assertEqual(response.status_code, 200)
            self.assertLessEqual(int(response.headers["X-Perf-SQL-Count"]), max_queries)
            self.assertLessEqual(int(response.headers["X-Perf-SQL-Duplicate-Count"]), max_duplicates)
            self.assertLessEqual(int(response.headers["X-Perf-Loaded-Records"]), max_loaded_records)
            self.assertLessEqual(int(response.headers["X-Perf-Response-Size-Bytes"]), max_response_size)
            self.assertNotIn("internal server error", response.text.lower())
            self.assertIn("Revisión de propuesta", response.text)
        finally:
            fixture.cleanup()

    def test_order_detail_query_budget_small(self):
        self._assert_order_detail_is_light("small", max_queries=15, max_duplicates=2, max_loaded_records=108, max_response_size=140_000)

    def test_order_detail_query_budget_medium(self):
        self._assert_order_detail_is_light("medium", max_queries=15, max_duplicates=2, max_loaded_records=108, max_response_size=140_000)

    def test_order_detail_query_budget_large(self):
        self._assert_order_detail_is_light("large", max_queries=15, max_duplicates=2, max_loaded_records=108, max_response_size=140_000)


if __name__ == "__main__":
    unittest.main()
