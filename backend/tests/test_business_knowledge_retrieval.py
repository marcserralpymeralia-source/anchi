from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import Sequence
from unittest.mock import patch

os.environ.setdefault("APP_ENV", "test")

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.agent.platform import DecisionEngineService  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import Company, Customer, KnowledgeEntry, ManualCorrection, Order, Product, ProductAlias  # noqa: E402
from app.knowledge.service import (  # noqa: E402
    add_product_alias,
    build_knowledge_embedding_text,
    create_knowledge_entry,
    index_knowledge_entries,
    knowledge_content_hash,
    record_human_correction,
    retrieve_product_knowledge,
)


class KeywordKnowledgeProvider:
    def generate_embedding(self, text: str, *, model: str) -> list[float]:
        return self.generate_embeddings([text], model=model)[0]

    def generate_embeddings(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        lowered = text.lower()
        if "azul" in lowered or "vaso" in lowered:
            return [1.0, 0.0, 0.0]
        if "sustituto" in lowered or "4821" in lowered:
            return [0.0, 1.0, 0.0]
        if "semana pasada" in lowered or "habitual" in lowered:
            return [0.0, 0.0, 1.0]
        return [0.0, 0.0, 0.0]


class BusinessKnowledgeRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{self.tmpdir.name}/knowledge.db", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        self.db: Session = self.SessionLocal()
        self.company = Company(name="Demo")
        self.db.add(self.company)
        self.db.flush()
        self.customer_a = Customer(company_id=self.company.id, code="C001", fiscal_name="Cliente Azul SL")
        self.customer_b = Customer(company_id=self.company.id, code="C002", fiscal_name="Cliente Rojo SL")
        self.product_a = Product(company_id=self.company.id, reference="P8291", name="Vaso azul pequeno")
        self.product_b = Product(company_id=self.company.id, reference="P4821", name="Vaso transparente")
        self.db.add_all([self.customer_a, self.customer_b, self.product_a, self.product_b])
        self.db.flush()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()
        self.tmpdir.cleanup()

    def test_creates_knowledge_entry(self):
        entry = create_knowledge_entry(
            self.db,
            company_id=self.company.id,
            source_type="commercial_note",
            source_id="note:1",
            content="Cliente usa los azules para el producto P8291",
            customer_id=self.customer_a.id,
            product_id=self.product_a.id,
        )

        self.assertEqual(entry.scope, "customer")
        self.assertEqual(entry.source_type, "commercial_note")

    def test_embedding_text_is_deterministic(self):
        entry = create_knowledge_entry(
            self.db,
            company_id=self.company.id,
            source_type="human_correction",
            source_id="correction:1",
            content='El cliente dice "los azules"',
            customer_id=self.customer_a.id,
            product_id=self.product_a.id,
        )

        self.assertEqual(build_knowledge_embedding_text(entry), build_knowledge_embedding_text(entry))
        self.assertIn("Tipo: human_correction", build_knowledge_embedding_text(entry))
        self.assertIn("Cliente:", build_knowledge_embedding_text(entry))
        self.assertIn("Producto:", build_knowledge_embedding_text(entry))

    def test_same_content_does_not_reindex(self):
        create_knowledge_entry(self.db, company_id=self.company.id, source_type="product_note", source_id="product:1", content="Vaso azul pequeno", product_id=self.product_a.id)
        first = index_knowledge_entries(self.db, company_id=self.company.id, provider=KeywordKnowledgeProvider(), model="fake")
        second = index_knowledge_entries(self.db, company_id=self.company.id, provider=KeywordKnowledgeProvider(), model="fake")

        self.assertEqual(first.indexed, 1)
        self.assertEqual(second.skipped, 1)

    def test_relevant_change_changes_content_hash(self):
        entry = create_knowledge_entry(self.db, company_id=self.company.id, source_type="product_note", source_id="product:1", content="Vaso azul", product_id=self.product_a.id)
        first_hash = knowledge_content_hash(build_knowledge_embedding_text(entry), model="fake")
        entry.content = "Vaso azul pequeno"
        second_hash = knowledge_content_hash(build_knowledge_embedding_text(entry), model="fake")

        self.assertNotEqual(first_hash, second_hash)

    def test_retrieval_returns_ordered_evidence(self):
        create_knowledge_entry(self.db, company_id=self.company.id, source_type="commercial_note", source_id="note:global", content="Vaso azul global", product_id=self.product_a.id)
        create_knowledge_entry(self.db, company_id=self.company.id, source_type="human_correction", source_id="correction:customer", content="Cliente usa los azules", customer_id=self.customer_a.id, product_id=self.product_a.id)
        index_knowledge_entries(self.db, company_id=self.company.id, provider=KeywordKnowledgeProvider(), model="fake")

        results = retrieve_product_knowledge(
            self.db,
            company_id=self.company.id,
            raw_description="los azules",
            customer_id=self.customer_a.id,
            candidate_product_ids=[self.product_a.id],
            provider=KeywordKnowledgeProvider(),
            model="fake",
            minimum_similarity=0,
        )

        self.assertEqual(results[0].source_type, "human_correction")
        self.assertGreaterEqual(results[0].relevance, results[1].relevance)

    def test_customer_filter_excludes_other_customer_specific_knowledge(self):
        create_knowledge_entry(self.db, company_id=self.company.id, source_type="human_correction", source_id="correction:a", content="Cliente A usa los azules", customer_id=self.customer_a.id, product_id=self.product_a.id)
        create_knowledge_entry(self.db, company_id=self.company.id, source_type="human_correction", source_id="correction:b", content="Cliente B usa los azules", customer_id=self.customer_b.id, product_id=self.product_b.id)
        index_knowledge_entries(self.db, company_id=self.company.id, provider=KeywordKnowledgeProvider(), model="fake")

        results = retrieve_product_knowledge(
            self.db,
            company_id=self.company.id,
            raw_description="los azules",
            customer_id=self.customer_a.id,
            provider=KeywordKnowledgeProvider(),
            model="fake",
            minimum_similarity=0,
        )

        self.assertEqual({item.customer_id for item in results}, {self.customer_a.id})

    def test_product_filter_works(self):
        create_knowledge_entry(self.db, company_id=self.company.id, source_type="product_note", source_id="product:a", content="Vaso azul", product_id=self.product_a.id)
        create_knowledge_entry(self.db, company_id=self.company.id, source_type="known_substitution", source_id="product:b", content="Sustituto del 4821", product_id=self.product_b.id)
        index_knowledge_entries(self.db, company_id=self.company.id, provider=KeywordKnowledgeProvider(), model="fake")

        results = retrieve_product_knowledge(
            self.db,
            company_id=self.company.id,
            raw_description="sustituto 4821",
            candidate_product_ids=[self.product_b.id],
            provider=KeywordKnowledgeProvider(),
            model="fake",
            minimum_similarity=0.5,
        )

        self.assertEqual([item.product_id for item in results], [self.product_b.id])

    def test_global_search_works(self):
        create_knowledge_entry(self.db, company_id=self.company.id, source_type="business_rule", source_id="rule:1", content="Vaso azul suele ser familia menaje")
        index_knowledge_entries(self.db, company_id=self.company.id, provider=KeywordKnowledgeProvider(), model="fake")

        results = retrieve_product_knowledge(self.db, company_id=self.company.id, raw_description="vaso azul", provider=KeywordKnowledgeProvider(), model="fake", minimum_similarity=0)

        self.assertEqual(results[0].scope, "global")

    def test_source_type_and_source_id_are_preserved(self):
        create_knowledge_entry(self.db, company_id=self.company.id, source_type="business_rule", source_id="rule:1", content="Vaso azul suele ser familia menaje")
        index_knowledge_entries(self.db, company_id=self.company.id, provider=KeywordKnowledgeProvider(), model="fake")

        result = retrieve_product_knowledge(self.db, company_id=self.company.id, raw_description="vaso azul", provider=KeywordKnowledgeProvider(), model="fake", minimum_similarity=0)[0]

        self.assertEqual(result.source_type, "business_rule")
        self.assertEqual(result.source_id, "rule:1")

    def test_absence_of_results_returns_empty_list(self):
        create_knowledge_entry(self.db, company_id=self.company.id, source_type="business_rule", source_id="rule:1", content="Vaso azul")
        index_knowledge_entries(self.db, company_id=self.company.id, provider=KeywordKnowledgeProvider(), model="fake")

        results = retrieve_product_knowledge(self.db, company_id=self.company.id, raw_description="nada relacionado", provider=KeywordKnowledgeProvider(), model="fake", minimum_similarity=0.2)

        self.assertEqual(results, [])

    def test_evidence_does_not_return_product_decision(self):
        create_knowledge_entry(self.db, company_id=self.company.id, source_type="product_note", source_id="product:a", content="Vaso azul", product_id=self.product_a.id)
        index_knowledge_entries(self.db, company_id=self.company.id, provider=KeywordKnowledgeProvider(), model="fake")

        result = retrieve_product_knowledge(self.db, company_id=self.company.id, raw_description="vaso azul", provider=KeywordKnowledgeProvider(), model="fake", minimum_similarity=0)[0]

        self.assertFalse(hasattr(result, "selected"))
        self.assertFalse(hasattr(result, "decision"))
        self.assertEqual(result.product_id, self.product_a.id)

    def test_global_alias_is_retrieved_and_persisted_as_alias(self):
        entry = add_product_alias(self.db, company_id=self.company.id, product_id=self.product_a.id, alias="vaso azul pequeno", scope="global")
        index_knowledge_entries(self.db, company_id=self.company.id, provider=KeywordKnowledgeProvider(), model="fake")

        result = retrieve_product_knowledge(self.db, company_id=self.company.id, raw_description="vaso azul", provider=KeywordKnowledgeProvider(), model="fake", minimum_similarity=0)[0]
        alias = self.db.scalar(select(ProductAlias).where(ProductAlias.company_id == self.company.id, ProductAlias.alias == "vaso azul pequeno"))

        self.assertEqual(entry.scope, "global")
        self.assertIsNotNone(alias)
        self.assertEqual(result.source_type, "product_alias")

    def test_customer_specific_alias_preserves_scope(self):
        entry = add_product_alias(self.db, company_id=self.company.id, product_id=self.product_a.id, alias="los azules", scope="customer", customer_id=self.customer_a.id)

        self.assertEqual(entry.scope, "customer")
        self.assertEqual(entry.customer_id, self.customer_a.id)

    def test_human_correction_links_source_order(self):
        order = Order(company_id=self.company.id, customer_id=self.customer_a.id)
        self.db.add(order)
        self.db.flush()

        entry = record_human_correction(
            self.db,
            company_id=self.company.id,
            raw_description="los azules",
            chosen_product_id=self.product_a.id,
            rejected_product_ids=[self.product_b.id],
            customer_id=self.customer_a.id,
            source_order_id=order.id,
        )
        correction = self.db.scalar(select(ManualCorrection).where(ManualCorrection.company_id == self.company.id))

        self.assertEqual(correction.order_id, order.id)
        self.assertEqual(entry.source_type, "human_correction")
        self.assertEqual(entry.metadata_json is not None, True)

    def test_decision_engine_exposes_knowledge_as_evidence_without_selection(self):
        create_knowledge_entry(self.db, company_id=self.company.id, source_type="human_correction", source_id="correction:a", content="Cliente usa los azules", customer_id=self.customer_a.id, product_id=self.product_a.id)
        index_knowledge_entries(self.db, company_id=self.company.id, provider=KeywordKnowledgeProvider(), model="fake")
        engine = DecisionEngineService(semantic_provider=KeywordKnowledgeProvider())

        with patch.dict(os.environ, {"EMBEDDING_MODEL": "fake"}):
            decision = engine.product_decision(self.db, self.company.id, customer_id=self.customer_a.id, detected_name="azules", text="los azules")

        self.assertIsNone(decision["selected"])
        self.assertEqual(decision["knowledge_evidence"][0].source, "business_knowledge")
        self.assertIn("business_knowledge", {item["source"] for item in decision["evidence"]})


if __name__ == "__main__":
    unittest.main()
