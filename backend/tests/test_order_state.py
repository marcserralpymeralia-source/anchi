from __future__ import annotations

import os
import unittest
from unittest.mock import patch
from pathlib import Path
import sys

os.environ.setdefault("APP_ENV", "test")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import Order, ScoringSettings  # noqa: E402
from app.orders.state import ORDER_STATE  # noqa: E402


class OrderStateTests(unittest.TestCase):
    def setUp(self):
        self.settings = ScoringSettings(
            company_id=1,
            safe_threshold=80,
            review_threshold=60,
            doubtful_threshold=40,
            block_without_customer=True,
            block_without_reference=True,
            block_without_quantity=True,
            block_below_threshold=True,
        )

    def test_scoring_category_and_operational_state_are_consistent(self):
        order = Order(id=1, score=85, status="pedido_pendiente_revision", validated_customer_id=1)
        self.assertEqual(ORDER_STATE.scoring_category(order.score, self.settings), "safe")
        self.assertEqual(ORDER_STATE.operational_state(order, self.settings), "ready")

        review_order = Order(id=2, score=65, status="pedido_pendiente_revision", validated_customer_id=1)
        self.assertEqual(ORDER_STATE.scoring_category(review_order.score, self.settings), "reviewable")
        self.assertEqual(ORDER_STATE.operational_state(review_order, self.settings), "review")

    def test_blockers_drive_blocked_state(self):
        order = Order(id=3, score=35, status="pedido_pendiente_revision", validated_customer_id=1)
        blockers = ORDER_STATE.validate_blockers(order, self.settings)
        self.assertIn("Confianza baja", blockers)
        self.assertEqual(ORDER_STATE.operational_state(order, self.settings), "blocked")

    def test_terminal_statuses_include_final_lifecycle_states(self):
        self.assertTrue(ORDER_STATE.is_terminal("pedido_confirmado"))
        self.assertTrue(ORDER_STATE.is_terminal("pedido_exportado"))
        self.assertTrue(ORDER_STATE.is_terminal("cerrado"))
        self.assertTrue(ORDER_STATE.is_terminal("deleted"))
        self.assertFalse(ORDER_STATE.is_terminal("pedido_pendiente_revision"))

    def test_status_for_score_uses_configured_thresholds(self):
        order = Order(id=4, score=0, status="pending_review")
        with patch("app.orders.state.get_or_create_settings", return_value=self.settings):
            self.assertEqual(ORDER_STATE.status_for_score(object(), 1, 85), "pedido_pendiente_revision")
            self.assertEqual(ORDER_STATE.status_for_score(object(), 1, 55), "dudoso")
            self.assertEqual(ORDER_STATE.status_for_score(object(), 1, 45), "dudoso")
            self.assertEqual(ORDER_STATE.status_for_score(object(), 1, 20), "no_importable")

        with patch("app.orders.state.get_or_create_settings", return_value=self.settings):
            ORDER_STATE.apply_score(object(), order, 1, 77)
            self.assertEqual(order.score, 77)
            self.assertEqual(order.status, "pedido_pendiente_revision")


if __name__ == "__main__":
    unittest.main()
