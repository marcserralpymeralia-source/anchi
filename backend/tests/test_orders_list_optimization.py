from __future__ import annotations

import os
import re
import unittest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")
os.environ.setdefault("PERFORMANCE_PROFILING_ENABLED", "true")
os.environ.setdefault("ENABLE_PERFORMANCE_PROFILING", "true")

from scripts.performance_data import build_performance_fixture, performance_test_client  # noqa: E402


class OrdersListOptimizationTests(unittest.TestCase):
    def test_orders_switches_between_card_and_list_views(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                cards_response = client.get("/orders?view=cards")
                list_response = client.get("/orders?view=list")

            self.assertEqual(cards_response.status_code, 200)
            self.assertIn('class="work-queue"', cards_response.text)
            self.assertIn('href="/orders?view=list"', cards_response.text)
            self.assertEqual(list_response.status_code, 200)
            self.assertIn("database-table-orders", list_response.text)
            self.assertIn('href="/orders?view=cards"', list_response.text)
        finally:
            fixture.cleanup()

    def test_card_and_list_views_share_the_same_order_dataset(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                cards_response = client.get("/orders?view=cards&page_size=25")
                list_response = client.get("/orders?view=list&page_size=25")
                filtered_cards_response = client.get("/orders?view=cards&search=Cliente&page_size=25")
                filtered_list_response = client.get("/orders?view=list&search=Cliente&page_size=25")

            card_ids = re.findall(r'data-kind="order"[^>]*data-selection-id="(\d+)"', cards_response.text)
            list_ids = re.findall(r'data-order-id="(\d+)"', list_response.text)
            filtered_card_ids = re.findall(r'data-kind="order"[^>]*data-selection-id="(\d+)"', filtered_cards_response.text)
            filtered_list_ids = re.findall(r'data-order-id="(\d+)"', filtered_list_response.text)

            self.assertEqual(card_ids, list_ids)
            self.assertEqual(filtered_card_ids, filtered_list_ids)
        finally:
            fixture.cleanup()

    def test_orders_can_be_archived_and_restored(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                response = client.get("/orders?view=list")
                archive_match = re.search(r'action="/orders/(\d+)/archive"', response.text)
                self.assertIsNotNone(archive_match)
                order_id = archive_match.group(1)

                archived_response = client.post(
                    f"/orders/{order_id}/archive",
                    headers={"referer": "/orders?view=list"},
                    follow_redirects=False,
                )
                self.assertEqual(archived_response.status_code, 303)
                self.assertEqual(archived_response.headers["location"], "/orders?view=list")

                active_response = client.get("/orders?view=list")
                archived_list_response = client.get("/orders?view=list&archived=true")
                self.assertNotIn(f'action="/orders/{order_id}/archive"', active_response.text)
                self.assertIn(f'action="/orders/{order_id}/unarchive"', archived_list_response.text)
                self.assertIn('name="archived" value="true"', archived_list_response.text)

                restored_response = client.post(
                    f"/orders/{order_id}/unarchive",
                    headers={"referer": "/orders?view=list&archived=true"},
                    follow_redirects=False,
                )
                self.assertEqual(restored_response.status_code, 303)
                self.assertEqual(restored_response.headers["location"], "/orders?view=list&archived=true")
                restored_list_response = client.get("/orders?view=list")
                self.assertIn(f'action="/orders/{order_id}/archive"', restored_list_response.text)
        finally:
            fixture.cleanup()

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
