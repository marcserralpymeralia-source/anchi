from __future__ import annotations

import math
import os
import tempfile
import unittest
from collections.abc import Sequence
from unittest.mock import patch

os.environ.setdefault("APP_ENV", "test")

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.db.database import Base  # noqa: E402
from app.agent.platform import DecisionEngineService  # noqa: E402
from app.db.models import BackgroundJob, Company, Product, ProductAlias, ProductEmbedding  # noqa: E402
from app.semantic_retrieval.embeddings import generate_embeddings  # noqa: E402
from app.semantic_retrieval.products import (  # noqa: E402
    ProductIndexStats,
    build_product_embedding_text,
    find_product_candidates,
    index_products,
    normalize_embedding_text,
    product_embedding_content_hash,
)
from app.workers.jobs_worker import JOB_TYPES, _process_product_embeddings_job  # noqa: E402


class KeywordEmbeddingProvider:
    def generate_embedding(self, text: str, *, model: str) -> list[float]:
        return self.generate_embeddings([text], model=model)[0]

    def generate_embeddings(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        lowered = text.lower()
        if "vaso" in lowered or "cristal" in lowered:
            return [1.0, 0.0, 0.0]
        if "plato" in lowered or "ceramica" in lowered:
            return [0.0, 1.0, 0.0]
        if "servilleta" in lowered or "mantel" in lowered:
            return [0.0, 0.0, 1.0]
        if "mixto" in lowered:
            return [0.7, 0.7, 0.0]
        return [0.0, 0.0, 0.0]


class SemanticProductRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{self.tmpdir.name}/semantic.db", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        self.db: Session = self.SessionLocal()
        self.company = Company(name="Demo")
        self.db.add(self.company)
        self.db.flush()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()
        self.tmpdir.cleanup()

    def _product(self, reference: str, name: str, **kwargs) -> Product:
        product = Product(company_id=self.company.id, reference=reference, name=name, **kwargs)
        self.db.add(product)
        self.db.flush()
        return product

    def test_embeddings_use_dedicated_timeout_setting(self):
        fake_response = unittest.mock.MagicMock()
        fake_response.__enter__.return_value.read.return_value = b'{"data":[{"index":0,"embedding":[1.0]}]}'

        with (
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "test-key",
                    "OPENAI_EMBEDDING_TIMEOUT_SECONDS": "7",
                    "OPENAI_TIMEOUT_SECONDS": "60",
                },
            ),
            patch("urllib.request.urlopen", return_value=fake_response) as mocked_urlopen,
        ):
            vectors = generate_embeddings(["vaso"], model="fake")

        self.assertEqual(vectors, [[1.0]])
        self.assertEqual(mocked_urlopen.call_args.kwargs["timeout"], 7)

    def test_embedding_text_is_deterministic_and_uses_stable_fields(self):
        product = self._product(
            "VAS-001",
            "Vaso cristal azul",
            description="Caja de vasos pequenos",
            brand="Nord",
            family="Menaje",
            sale_price=12.5,
            warehouse_location_code="A-01",
        )
        self.db.add(ProductAlias(company_id=self.company.id, product_id=product.id, alias="vasito azul"))
        self.db.add(ProductAlias(company_id=self.company.id, product_id=product.id, alias="cristal azul"))
        self.db.flush()

        first = build_product_embedding_text(product)
        second = build_product_embedding_text(product)

        self.assertEqual(first, second)
        self.assertIn("Referencia: VAS-001", first)
        self.assertIn("Nombre: Vaso cristal azul", first)
        self.assertIn("Sinonimos: cristal azul, vasito azul", first)
        self.assertNotIn("12.5", first)
        self.assertNotIn("A-01", first)

    def test_same_product_unchanged_is_not_reindexed(self):
        self._product("VAS-001", "Vaso cristal azul", description="Caja de vasos")
        first = index_products(self.db, company_id=self.company.id, provider=KeywordEmbeddingProvider(), model="fake")
        second = index_products(self.db, company_id=self.company.id, provider=KeywordEmbeddingProvider(), model="fake")

        self.assertEqual(first.indexed, 1)
        self.assertEqual(second.indexed, 0)
        self.assertEqual(second.skipped, 1)

    def test_relevant_product_change_changes_content_hash(self):
        product = self._product("VAS-001", "Vaso cristal azul", description="Caja de vasos")
        first_text = build_product_embedding_text(product)
        first_hash = product_embedding_content_hash(first_text, model="fake")
        product.description = "Caja de vasos grandes"
        second_text = build_product_embedding_text(product)
        second_hash = product_embedding_content_hash(second_text, model="fake")

        self.assertNotEqual(first_hash, second_hash)

    def test_search_without_product_index_does_not_call_embedding_provider(self):
        class FailingProvider:
            def generate_embedding(self, text: str, *, model: str) -> list[float]:
                raise AssertionError("El proveedor no debe llamarse sin indice de productos.")

            def generate_embeddings(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
                raise AssertionError("El proveedor no debe llamarse sin indice de productos.")

        self._product("VAS-001", "Vaso cristal azul")

        candidates = find_product_candidates(
            self.db,
            company_id=self.company.id,
            query="necesito vasos",
            provider=FailingProvider(),
            model="fake",
        )

        self.assertEqual(candidates, [])

    def test_search_returns_candidates_ordered_by_similarity(self):
        self._seed_search_products()
        index_products(self.db, company_id=self.company.id, provider=KeywordEmbeddingProvider(), model="fake")

        candidates = find_product_candidates(
            self.db,
            company_id=self.company.id,
            query="necesito vasos de cristal",
            provider=KeywordEmbeddingProvider(),
            model="fake",
            minimum_similarity=0,
        )

        self.assertEqual(candidates[0].reference, "VAS-001")
        self.assertGreaterEqual(candidates[0].similarity, candidates[1].similarity)

    def test_search_respects_limit(self):
        self._seed_search_products()
        index_products(self.db, company_id=self.company.id, provider=KeywordEmbeddingProvider(), model="fake")

        candidates = find_product_candidates(
            self.db,
            company_id=self.company.id,
            query="mixto",
            limit=1,
            provider=KeywordEmbeddingProvider(),
            model="fake",
            minimum_similarity=0,
        )

        self.assertEqual(len(candidates), 1)

    def test_search_respects_minimum_similarity(self):
        self._seed_search_products()
        index_products(self.db, company_id=self.company.id, provider=KeywordEmbeddingProvider(), model="fake")

        candidates = find_product_candidates(
            self.db,
            company_id=self.company.id,
            query="necesito vasos",
            provider=KeywordEmbeddingProvider(),
            model="fake",
            minimum_similarity=0.99,
        )

        self.assertTrue(all(math.isclose(candidate.similarity, 1.0) for candidate in candidates))

    def test_search_returns_empty_when_no_reasonable_results(self):
        self._seed_search_products()
        index_products(self.db, company_id=self.company.id, provider=KeywordEmbeddingProvider(), model="fake")

        candidates = find_product_candidates(
            self.db,
            company_id=self.company.id,
            query="producto desconocido",
            provider=KeywordEmbeddingProvider(),
            model="fake",
            minimum_similarity=0.2,
        )

        self.assertEqual(candidates, [])

    def test_search_does_not_assign_first_candidate_automatically(self):
        product = self._product("VAS-001", "Vaso cristal azul")
        index_products(self.db, company_id=self.company.id, provider=KeywordEmbeddingProvider(), model="fake")

        candidates = find_product_candidates(
            self.db,
            company_id=self.company.id,
            query="vasos de cristal",
            provider=KeywordEmbeddingProvider(),
            model="fake",
            minimum_similarity=0,
        )
        refreshed_product = self.db.get(Product, product.id)

        self.assertEqual(candidates[0].product_id, product.id)
        self.assertFalse(hasattr(candidates[0], "selected"))
        self.assertEqual(refreshed_product.reference, "VAS-001")

    def test_decision_engine_keeps_semantic_candidates_out_of_selection(self):
        product = self._product("VAS-001", "Cristaleria azul", description="Vaso de cristal para agua")
        index_products(self.db, company_id=self.company.id, provider=KeywordEmbeddingProvider(), model="fake")
        engine = DecisionEngineService(semantic_provider=KeywordEmbeddingProvider())

        with patch.dict(os.environ, {"EMBEDDING_MODEL": "fake"}):
            decision = engine.product_decision(
                self.db,
                self.company.id,
                detected_name="vasos pequenos",
                text="necesito vasos pequenos",
            )

        self.assertIsNone(decision["selected"])
        self.assertEqual(decision["semantic_candidates"][0].source, "semantic_candidate")
        self.assertEqual(decision["semantic_candidates"][0].product_id, product.id)
        self.assertIn("semantic_candidate", {item["source"] for item in decision["evidence"]})

    def test_decision_engine_stops_semantic_calls_after_first_provider_failure(self):
        product = self._product("VAS-001", "Vaso cristal azul")
        self.db.add(
            ProductEmbedding(
                company_id=self.company.id,
                product_id=product.id,
                embedding_json="[1.0, 0.0, 0.0]",
                embedding_text="Vaso cristal azul",
                embedding_model="fake",
                embedding_version="product-v1",
                content_hash="test",
                dimensions=3,
            )
        )
        self.db.commit()

        class FailingProvider:
            def __init__(self):
                self.calls = 0

            def generate_embedding(self, text: str, *, model: str) -> list[float]:
                self.calls += 1
                raise RuntimeError("provider unavailable")

        provider = FailingProvider()
        engine = DecisionEngineService(semantic_provider=provider)

        with patch.dict(os.environ, {"EMBEDDING_MODEL": "fake"}):
            engine.product_decision(
                self.db,
                self.company.id,
                detected_name="vasos",
                text="vasos",
            )
            engine.product_decision(
                self.db,
                self.company.id,
                detected_name="cristal",
                text="cristal",
            )

        self.assertEqual(provider.calls, 1)

    def test_decision_engine_runtime_reset_allows_semantic_retry(self):
        product = self._product("VAS-001", "Vaso cristal azul")
        self.db.add(
            ProductEmbedding(
                company_id=self.company.id,
                product_id=product.id,
                embedding_json="[1.0, 0.0, 0.0]",
                embedding_text="Vaso cristal azul",
                embedding_model="fake",
                embedding_version="product-v1",
                content_hash="test",
                dimensions=3,
            )
        )
        self.db.commit()

        class FailingProvider:
            def __init__(self):
                self.calls = 0

            def generate_embedding(self, text: str, *, model: str) -> list[float]:
                self.calls += 1
                raise RuntimeError("provider unavailable")

        provider = FailingProvider()
        engine = DecisionEngineService(semantic_provider=provider)

        with patch.dict(os.environ, {"EMBEDDING_MODEL": "fake"}):
            engine.product_decision(
                self.db,
                self.company.id,
                detected_name="vasos",
                text="vasos",
            )
            engine.reset_runtime_state()
            engine.product_decision(
                self.db,
                self.company.id,
                detected_name="cristal",
                text="cristal",
            )

        self.assertEqual(provider.calls, 2)

    def test_worker_accepts_product_embedding_index_job(self):
        self.assertIn("index_product_embeddings", JOB_TYPES)
        job = BackgroundJob(company_id=self.company.id, job_type="index_product_embeddings", payload_json='{"batch_size": 10}', status="running")
        self.db.add(job)
        self.db.flush()

        with patch("app.workers.jobs_worker.index_products", return_value=ProductIndexStats(scanned=2, indexed=1, skipped=1, failed=0)):
            result = _process_product_embeddings_job(self.db, job, {"batch_size": 10})

        self.assertTrue(result["ok"])
        self.assertEqual(result["indexed"], 1)
        self.assertEqual(job.progress, 100)

    def test_products_without_description_are_indexed(self):
        self._product("VAS-001", "Vaso cristal azul", description=None)

        stats = index_products(self.db, company_id=self.company.id, provider=KeywordEmbeddingProvider(), model="fake")
        embedding = self.db.scalar(select(ProductEmbedding))

        self.assertEqual(stats.indexed, 1)
        self.assertIn("Nombre: Vaso cristal azul", embedding.embedding_text)

    def test_normalization_handles_incomplete_data(self):
        product = self._product("VAS-001", "  Vaso   cristal\nazul  ", description=None, brand=None)
        text = build_product_embedding_text(product)

        self.assertEqual(normalize_embedding_text("  A\t B\nC "), "A B C")
        self.assertNotIn("None", text)
        self.assertIn("Nombre: Vaso cristal azul", text)

    def test_product_embedding_table_has_idempotency_columns(self):
        columns = ProductEmbedding.__table__.columns

        self.assertIn("embedding_model", columns)
        self.assertIn("embedding_version", columns)
        self.assertIn("content_hash", columns)
        self.assertIn("embedded_at", columns)

    def _seed_search_products(self) -> None:
        self._product("VAS-001", "Vaso cristal azul", description="Vaso de cristal para agua")
        self._product("PLA-001", "Plato ceramica", description="Plato llano blanco")
        self._product("SER-001", "Servilleta papel", description="Servilleta para mesa")


if __name__ == "__main__":
    unittest.main()
