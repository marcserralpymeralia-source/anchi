from __future__ import annotations

import json
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.extraction.schema import (  # noqa: E402
    ORDER_EXTRACTION_SCHEMA_VERSION,
    FORBIDDEN_EXTRACTION_KEYS,
    OrderExtraction,
    assert_no_erp_identifiers,
    order_extraction_json_schema,
)


FIXTURES_PATH = Path(__file__).parent / "fixtures" / "order_extraction_cases.json"


class OrderExtractionSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))

    def test_schema_version_is_stable(self):
        self.assertEqual(ORDER_EXTRACTION_SCHEMA_VERSION, "1.0")

    def test_ten_evaluation_cases_validate(self):
        self.assertEqual(len(self.cases), 10)
        for case in self.cases:
            with self.subTest(case=case["name"]):
                extraction = OrderExtraction.model_validate(case["payload"])
                self.assertEqual(extraction.model_dump(by_alias=True), case["payload"])
                assert_no_erp_identifiers(case["payload"])

    def test_not_order_cannot_include_lines(self):
        payload = dict(self.cases[2]["payload"])
        payload["lines"] = [self.cases[0]["payload"]["lines"][0]]
        with self.assertRaises(ValueError):
            OrderExtraction.model_validate(payload)

    def test_order_requires_at_least_one_line(self):
        payload = dict(self.cases[0]["payload"])
        payload["lines"] = []
        with self.assertRaises(ValueError):
            OrderExtraction.model_validate(payload)

    def test_erp_identifiers_are_rejected(self):
        payload = dict(self.cases[0]["payload"])
        payload["customerId"] = 123
        with self.assertRaises(ValueError):
            assert_no_erp_identifiers(payload)
        self.assertIn("customerId", FORBIDDEN_EXTRACTION_KEYS)
        self.assertIn("productId", FORBIDDEN_EXTRACTION_KEYS)

    def test_json_schema_uses_camel_case_and_forbids_extra_fields(self):
        schema = order_extraction_json_schema()
        properties = schema["properties"]
        self.assertIn("isOrder", properties)
        self.assertIn("requiresReview", properties)
        self.assertEqual(schema["additionalProperties"], False)
        self.assertEqual(set(schema["required"]), set(properties.keys()))


if __name__ == "__main__":
    unittest.main()
