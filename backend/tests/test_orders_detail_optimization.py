from __future__ import annotations

import os
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")
os.environ.setdefault("PERFORMANCE_PROFILING_ENABLED", "true")
os.environ.setdefault("ENABLE_PERFORMANCE_PROFILING", "true")

from scripts.performance_data import build_performance_fixture, performance_test_client  # noqa: E402
from app.db.models import Email, Order  # noqa: E402


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
            self.assertIn("Confianza del pedido", response.text)
            self.assertIn("Correo", response.text)
            self.assertIn("Referencia", response.text)
            self.assertIn("Descripción", response.text)
            self.assertIn("Unidades", response.text)
            self.assertIn("Precio", response.text)
            self.assertNotIn('data-order-source-tab="conversation"', response.text)
            self.assertNotIn('data-order-source-panel="conversation"', response.text)
            self.assertIn("Adjuntos (", response.text)
            self.assertNotIn("PDF (", response.text)
        finally:
            fixture.cleanup()

    def test_order_detail_query_budget_small(self):
        self._assert_order_detail_is_light("small", max_queries=15, max_duplicates=2, max_loaded_records=108, max_response_size=140_000)

    def test_order_detail_query_budget_medium(self):
        self._assert_order_detail_is_light("medium", max_queries=15, max_duplicates=2, max_loaded_records=108, max_response_size=140_000)

    def test_order_detail_query_budget_large(self):
        self._assert_order_detail_is_light("large", max_queries=15, max_duplicates=2, max_loaded_records=108, max_response_size=140_000)

    def test_product_search_returns_matching_products(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                response = client.get("/orders/product-search", params={"q": "KRAFT"})

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload)
            self.assertTrue(
                any(
                    "KRAFT" in item["reference"].upper()
                    or "KRAFT" in item["name"].upper()
                    for item in payload
                )
            )
            self.assertTrue(
                all(
                    {"id", "reference", "name", "sale_price"} <= item.keys()
                    for item in payload
                )
            )
        finally:
            fixture.cleanup()

    def test_order_date_defaults_to_email_date_and_saves_iso(self):
        fixture = build_performance_fixture("small")
        engine = create_engine(
            fixture.tenant_database_url,
            connect_args={"check_same_thread": False},
        )
        SessionLocal = sessionmaker(
            bind=engine,
            autoflush=False,
            autocommit=False,
        )

        try:
            with SessionLocal() as db:
                order = db.get(Order, fixture.order_ids[0])
                self.assertIsNotNone(order)

                email = db.get(Email, order.email_id)
                self.assertIsNotNone(email)
                self.assertIsNotNone(email.received_at)

                expected_display_date = email.received_at.strftime("%d-%m-%Y")
                order.order_date = ""
                db.commit()

            with performance_test_client(fixture) as client:
                response = client.get(f"/orders/{fixture.order_ids[0]}")
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    f'value="{expected_display_date}"',
                    response.text,
                )

                save_response = client.post(
                    f"/orders/{fixture.order_ids[0]}/save",
                    data={"order_date": "25-08-2026"},
                    follow_redirects=False,
                )

            self.assertEqual(save_response.status_code, 303)
            self.assertEqual(
                save_response.headers["location"],
                f"/orders/{fixture.order_ids[0]}",
            )

            with SessionLocal() as db:
                order = db.get(Order, fixture.order_ids[0])
                self.assertEqual(order.order_date, "2026-08-25")
        finally:
            engine.dispose()
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
