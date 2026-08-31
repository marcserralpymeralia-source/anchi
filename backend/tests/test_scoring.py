import unittest
from types import SimpleNamespace

from app.orders.scoring import calculate_order_score


class SharedOrderScoringTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            customer_weight=25,
            products_weight=40,
            quantities_weight=20,
            coherence_weight=10,
            llm_weight=5,
        )

    def test_validated_order_can_reach_one_hundred(self):
        order = SimpleNamespace(
            validated_customer_id=7,
            customer_id=7,
            lines=[
                SimpleNamespace(
                    product_id=11,
                    validated_product_id=11,
                    quantity="10,5",
                    extraction_confidence=1,
                )
            ],
        )

        result = calculate_order_score(order, self.settings)

        self.assertEqual(result.total, 100)
        self.assertEqual(result.products, 40)
        self.assertEqual(result.quantities, 20)

    def test_unvalidated_proposal_does_not_receive_product_or_customer_points(self):
        order = SimpleNamespace(
            validated_customer_id=None,
            customer_id=7,
            lines=[
                SimpleNamespace(
                    product_id=11,
                    validated_product_id=None,
                    quantity=2,
                    extraction_confidence=0.8,
                )
            ],
        )

        result = calculate_order_score(order, self.settings)

        self.assertEqual(result.customer, 0)
        self.assertEqual(result.products, 0)
        self.assertEqual(result.quantities, 20)
        self.assertEqual(result.confidence, 4)

    def test_preview_can_score_proposals_explicitly(self):
        order = SimpleNamespace(
            validated_customer_id=None,
            customer_id=7,
            lines=[SimpleNamespace(product_id=11, quantity=1, extraction_confidence=0.9)],
        )

        result = calculate_order_score(order, self.settings, use_proposals=True)

        self.assertEqual(result.customer, 25)
        self.assertEqual(result.products, 40)


if __name__ == "__main__":
    unittest.main()
