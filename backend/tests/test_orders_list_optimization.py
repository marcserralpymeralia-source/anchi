from __future__ import annotations

import os
import unittest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")
os.environ.setdefault("PERFORMANCE_PROFILING_ENABLED", "true")
os.environ.setdefault("ENABLE_PERFORMANCE_PROFILING", "true")

from scripts.performance_data import build_performance_fixture, performance_test_client  # noqa: E402


class OrdersListOptimizationTests(unittest.TestCase):
    def _assert_orders_list_is_light(self, scenario: str, *, max_queries: int, max_duplicates: int, max_response_size: int | None = None) -> None:
        fixture = build_performance_fixture(scenario)
        try:
            with performance_test_client(fixture) as client:
                response = client.get("/orders?date_range=90d")
                filtered_response = client.get("/orders?date_range=90d&has_pdf=yes")
                paged_response = client.get("/orders?date_range=90d&page=2&page_size=10")

            for current_response in (response, filtered_response, paged_response):
                self.assertEqual(current_response.status_code, 200)
                self.assertLessEqual(int(current_response.headers["X-Perf-SQL-Count"]), max_queries)
                self.assertLessEqual(int(current_response.headers["X-Perf-SQL-Duplicate-Count"]), max_duplicates)
                self.assertNotIn("internal server error", current_response.text.lower())
                self.assertNotIn("order-modal-", current_response.text)

            self.assertLessEqual(int(response.headers["X-Perf-Displayed-Items"]), 25)
            self.assertLessEqual(int(paged_response.headers["X-Perf-Displayed-Items"]), 10)
            if max_response_size is not None:
                self.assertLessEqual(int(response.headers["X-Perf-Response-Size-Bytes"]), max_response_size)
        finally:
            fixture.cleanup()

    def test_orders_list_query_budget_small(self):
        self._assert_orders_list_is_light("small", max_queries=20, max_duplicates=3)

    def test_orders_list_query_budget_medium(self):
        self._assert_orders_list_is_light("medium", max_queries=20, max_duplicates=3, max_response_size=500_000)

    def test_orders_list_query_budget_large(self):
        self._assert_orders_list_is_light("large", max_queries=20, max_duplicates=3)


if __name__ == "__main__":
    unittest.main()
