from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.agent.extraction.diagnostics import extraction_diagnostics_from_payload
from app.agent.extraction.schema import (
    OrderExtraction,
    OrderExtractionInput,
    OrderExtractionResult,
)
from app.agent.platform import UnifiedOrderPipelineService
from app.db.database import Base
from app.db.models import (
    Company,
    Customer,
    InboundMessage,
    InputChannel,
    Product,
    ProductAlias,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

CORPUS_PATH = Path(__file__).parent / "fixtures" / "agent_release_corpus.json"


class AgentReleaseCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.tempdir.name, 'release.sqlite').as_posix()}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        self.db = self.Session()
        self.db.add_all([
            Company(id=1, name="Release Demo"),
            InputChannel(id=1, company_id=1, key="email", name="Email", channel_type="email", is_active=True),
            Customer(id=1, company_id=1, code="C001", fiscal_name="Cliente Demo SL", primary_email="compras@demo.test"),
            Customer(id=2, company_id=1, code="C002", fiscal_name="Hostal Playa Norte SL"),
            Customer(id=3, company_id=1, code="C003", fiscal_name="Hostal Playa Norte SA"),
            Product(id=1, company_id=1, reference="001842", name="Tomate rama caja 5 kg", description="Tomate rama caja 5 kg; color rojo"),
            Product(id=2, company_id=1, reference="999", name="Cebolla saco 10 kg", description="Cebolla saco 10 kg"),
            Product(id=3, company_id=1, reference="001843", name="Tomate rama caja 10 kg", description="Tomate rama caja 10 kg"),
            Product(id=4, company_id=1, reference="P500", alternative_code="ALT-500", name="Envase PET 500 ml transparente", description="Capacidad 500 ml; color transparente; PET"),
            Product(id=5, company_id=1, reference="B3040", name="Bolsa transparente 30x40", description="Bolsa de plastico transparente 30 x 40 cm"),
        ])
        self.db.add(ProductAlias(company_id=1, product_id=5, alias="bolsa 30x40 transparente"))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.tempdir.cleanup()

    @staticmethod
    def _payload(case):
        return {
            "isOrder": bool(case["lines"]),
            "customer": {"rawName": case.get("customer_name"), "rawNameSource": "expressed" if case.get("customer_name") else "unknown"},
            "lines": [
                {
                    "rawText": line["raw_text"],
                    "rawDescription": line["description"],
                    "rawDescriptionSource": "expressed" if line["description"] else "unknown",
                    "reference": line["reference"],
                    "referenceSource": "expressed" if line["reference"] else "unknown",
                    "quantity": line["quantity"],
                    "quantitySource": "expressed" if line["quantity"] is not None else "unknown",
                    "unit": line["unit"],
                    "unitSource": "expressed" if line["unit"] else "unknown",
                    "notes": [],
                    "uncertainties": [{"field": "line", "reason": "Datos incompletos o ambiguos."}] if line["requires_review"] else [],
                    "requiresReview": line["requires_review"],
                }
                for line in case["lines"]
            ],
            "notes": [],
            "uncertainties": [{"field": "customer.rawName", "reason": "Cliente no indicado."}] if not case.get("customer_name") else [],
            "requiresReview": case["review_expected"],
        }

    def _run_case(self, case, pipeline):
        payload = self._payload(case)
        extracted = OrderExtraction.model_validate(payload)
        extraction_input = OrderExtractionInput(
            text=case["text"],
            sourceType=case["source_type"],
            sourceId=f"release-{case['id']}",
            attachmentText=case.get("attachment_text"),
        )
        result = OrderExtractionResult(rawInput=extraction_input, extractedData=extracted, model="release-test")
        legacy_payload = pipeline._legacy_payload_from_structured(result)
        inbound = InboundMessage(
            company_id=1,
            channel_id=1,
            provider="email",
            source_external_id=f"release-{case['id']}",
            sender=case["sender"],
            subject=f"Release case {case['id']}",
            original_content=case["text"],
            content_type=case["source_type"],
            has_pdf=case["source_type"] == "pdf",
        )
        self.db.add(inbound)
        self.db.flush()
        order = pipeline._create_order(self.db, inbound, None, legacy_payload, extraction_input.combined_text(), fast_path=True)
        self.db.flush()
        return extracted, result, legacy_payload, order

    def test_release_corpus_has_zero_critical_false_auto_confirmations(self):
        pipeline = UnifiedOrderPipelineService()
        metrics = {
            "extraction_correct": 0,
            "customer_correct": 0,
            "product_correct": 0,
            "quantity_correct": 0,
            "ambiguous_to_review": 0,
            "false_auto_confirmations": 0,
        }
        customer_cases = product_cases = quantity_cases = 0

        for case in self.corpus:
            with self.subTest(case=case["id"]):
                extracted, result, legacy_payload, order = self._run_case(case, pipeline)
                if extracted.is_order == bool(case["lines"]) and len(extracted.lines) == len(case["lines"]) and extracted.requires_review == case["review_expected"]:
                    metrics["extraction_correct"] += 1
                self.assertEqual(result.raw_input.source_type, case["source_type"])
                diagnostics = extraction_diagnostics_from_payload(json.dumps(legacy_payload))
                self.assertEqual(diagnostics["source"], "structured_order_extraction")
                self.assertEqual(diagnostics["schema_version"], "1.0")

                if case.get("customer_expected") is not None:
                    customer_cases += 1
                    customer = self.db.get(Customer, order.customer_id) if order.customer_id else None
                    if (
                        case["customer_expected"] == "demo"
                        and customer
                        and customer.code == "C001"
                    ) or (
                        case["customer_expected"] == "ambiguous"
                        and order.validated_customer_id is None
                    ):
                        metrics["customer_correct"] += 1

                expected_products = case["product_expected"] if isinstance(case["product_expected"], list) else [case["product_expected"]]
                order_lines = list(order.lines)
                product_cases += len(expected_products)
                for line, expected_key in zip(order_lines, expected_products):
                    expected_id = {"tomato5": 1, "onion": 2, "env500": 4, "bag3040": 5, None: None}[expected_key]
                    if (line.product_id or None) == expected_id:
                        metrics["product_correct"] += 1
                    if not case["product_auto"] and line.validated_product_id is not None:
                        metrics["false_auto_confirmations"] += 1

                if case.get("quantities_expected") is not None:
                    quantity_cases += len(case["quantities_expected"])
                    if [line.quantity for line in order_lines] == case["quantities_expected"]:
                        metrics["quantity_correct"] += len(case["quantities_expected"])

                actual_review = bool(order.review_reasons)
                if case["review_expected"] and actual_review:
                    metrics["ambiguous_to_review"] += 1
                if not case["customer_auto"] and order.validated_customer_id is not None:
                    metrics["false_auto_confirmations"] += 1

        self.assertEqual(metrics["false_auto_confirmations"], 0)
        self.assertEqual(metrics["extraction_correct"], len(self.corpus))
        self.assertEqual(metrics["customer_correct"], customer_cases)
        self.assertEqual(metrics["product_correct"], product_cases)
        self.assertEqual(metrics["quantity_correct"], quantity_cases)
        self.assertEqual(metrics["ambiguous_to_review"], sum(case["review_expected"] for case in self.corpus))
        print(f"RELEASE_CORPUS_CASES={len(self.corpus)}")
        print(f"EXTRACTION_CORRECT={metrics['extraction_correct']}/{len(self.corpus)}")
        print(f"CUSTOMER_MATCH_CORRECT={metrics['customer_correct']}/{customer_cases}")
        print(f"PRODUCT_MATCH_CORRECT={metrics['product_correct']}/{product_cases}")
        print(f"QUANTITY_CORRECT={metrics['quantity_correct']}/{quantity_cases}")
        print(f"AMBIGUOUS_TO_REVIEW={metrics['ambiguous_to_review']}")
        print(f"FALSE_AUTO_CONFIRMATIONS={metrics['false_auto_confirmations']}")


if __name__ == "__main__":
    unittest.main()
