from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Product, ProductEmbedding, utcnow
from app.semantic_retrieval.embeddings import EmbeddingProvider, OpenAIEmbeddingProvider, embedding_model


logger = logging.getLogger(__name__)
PRODUCT_EMBEDDING_VERSION = "product-v1"
DEFAULT_PRODUCT_CANDIDATE_MIN_SIMILARITY = 0.72


@dataclass(frozen=True)
class ProductCandidate:
    product_id: int
    reference: str
    name: str
    similarity: float
    embedding_model: str
    embedding_version: str


@dataclass(frozen=True)
class ProductIndexStats:
    scanned: int = 0
    indexed: int = 0
    skipped: int = 0
    failed: int = 0


def normalize_embedding_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def build_product_embedding_text(product: Product) -> str:
    aliases = sorted(
        {
            normalize_embedding_text(getattr(alias, "alias", ""))
            for alias in getattr(product, "aliases", []) or []
            if normalize_embedding_text(getattr(alias, "alias", ""))
        },
        key=str.casefold,
    )
    fields = [
        ("Referencia", product.reference),
        ("Codigo alternativo", product.alternative_code),
        ("Nombre", product.name),
        ("Descripcion", product.description),
        ("Descripcion ampliada", product.description_cont),
        ("Marca", product.brand),
        ("Familia", product.family),
        ("Subfamilia", product.subfamily),
        ("Formato", product.format),
        ("Unidad", product.sale_unit),
        ("Grupo talla", product.size_group),
        ("Colores", product.colors),
        ("Tipo articulo", product.article_type),
        ("EAN", product.ean),
    ]
    parts = [f"{label}: {clean}" for label, raw in fields if (clean := normalize_embedding_text(raw))]
    if aliases:
        parts.append(f"Sinonimos: {', '.join(aliases)}")
    return " | ".join(parts)


def product_embedding_content_hash(embedding_text: str, *, model: str, version: str = PRODUCT_EMBEDDING_VERSION) -> str:
    digest = hashlib.sha256()
    digest.update(version.encode("utf-8"))
    digest.update(b"\0")
    digest.update(model.encode("utf-8"))
    digest.update(b"\0")
    digest.update(embedding_text.encode("utf-8"))
    return digest.hexdigest()


def index_products(
    db: Session,
    *,
    company_id: int,
    provider: EmbeddingProvider | None = None,
    model: str | None = None,
    version: str = PRODUCT_EMBEDDING_VERSION,
    batch_size: int = 64,
) -> ProductIndexStats:
    selected_model = model or embedding_model()
    selected_provider = provider or OpenAIEmbeddingProvider()
    products = list(
        db.scalars(
            select(Product)
            .where(Product.company_id == company_id, Product.deleted_at.is_(None))
            .options(selectinload(Product.aliases))
            .order_by(Product.id.asc())
        )
    )
    pending: list[tuple[Product, str, str, ProductEmbedding | None]] = []
    skipped = 0
    for product in products:
        embedding_text = build_product_embedding_text(product)
        content_hash = product_embedding_content_hash(embedding_text, model=selected_model, version=version)
        existing = db.scalar(
            select(ProductEmbedding).where(
                ProductEmbedding.company_id == company_id,
                ProductEmbedding.product_id == product.id,
                ProductEmbedding.embedding_model == selected_model,
                ProductEmbedding.embedding_version == version,
            )
        )
        if existing and existing.content_hash == content_hash:
            skipped += 1
            continue
        pending.append((product, embedding_text, content_hash, existing))

    indexed = 0
    failed = 0
    for start in range(0, len(pending), max(batch_size, 1)):
        batch = pending[start : start + max(batch_size, 1)]
        try:
            vectors = selected_provider.generate_embeddings([item[1] for item in batch], model=selected_model)
        except Exception as exc:  # noqa: BLE001
            failed += len(batch)
            logger.warning("product_embedding_batch_failed", extra={"company_id": company_id, "count": len(batch), "error_type": type(exc).__name__})
            continue
        if len(vectors) != len(batch):
            failed += len(batch)
            logger.warning("product_embedding_batch_size_mismatch", extra={"company_id": company_id, "expected": len(batch), "received": len(vectors)})
            continue
        for (product, embedding_text, content_hash, existing), vector in zip(batch, vectors, strict=True):
            clean_vector = [float(value) for value in vector]
            now = utcnow()
            if existing is None:
                existing = ProductEmbedding(
                    company_id=company_id,
                    product_id=product.id,
                    embedding_json=json.dumps(clean_vector),
                    embedding_text=embedding_text,
                    embedding_model=selected_model,
                    embedding_version=version,
                    content_hash=content_hash,
                    dimensions=len(clean_vector),
                    embedded_at=now,
                )
                db.add(existing)
            else:
                existing.embedding_json = json.dumps(clean_vector)
                existing.embedding_text = embedding_text
                existing.content_hash = content_hash
                existing.dimensions = len(clean_vector)
                existing.embedded_at = now
                existing.updated_at = now
            indexed += 1
    db.commit()
    stats = ProductIndexStats(scanned=len(products), indexed=indexed, skipped=skipped, failed=failed)
    logger.info("product_embeddings_indexed", extra={"company_id": company_id, "stats": stats.__dict__})
    return stats


def find_product_candidates(
    db: Session,
    *,
    company_id: int,
    query: str,
    limit: int = 10,
    minimum_similarity: float | None = None,
    provider: EmbeddingProvider | None = None,
    model: str | None = None,
    version: str = PRODUCT_EMBEDDING_VERSION,
) -> list[ProductCandidate]:
    normalized_query = normalize_embedding_text(query)
    if not normalized_query:
        return []
    selected_model = model or embedding_model()
    threshold = _minimum_similarity(minimum_similarity)
    rows = db.execute(
        select(ProductEmbedding, Product)
        .join(Product, Product.id == ProductEmbedding.product_id)
        .where(
            ProductEmbedding.company_id == company_id,
            ProductEmbedding.embedding_model == selected_model,
            ProductEmbedding.embedding_version == version,
            Product.company_id == company_id,
            Product.deleted_at.is_(None),
        )
    ).all()
    if not rows:
        return []

    selected_provider = provider or OpenAIEmbeddingProvider()
    query_vector = selected_provider.generate_embedding(normalized_query, model=selected_model)
    candidates: list[ProductCandidate] = []
    for embedding, product in rows:
        similarity = cosine_similarity(query_vector, parse_embedding_json(embedding.embedding_json))
        if similarity < threshold:
            continue
        candidates.append(
            ProductCandidate(
                product_id=product.id,
                reference=product.reference,
                name=product.name,
                similarity=similarity,
                embedding_model=embedding.embedding_model,
                embedding_version=embedding.embedding_version,
            )
        )
    candidates.sort(key=lambda candidate: candidate.similarity, reverse=True)
    return candidates[: max(limit, 0)]


def parse_embedding_json(value: str) -> list[float]:
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        return []
    return [float(item) for item in parsed]


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def _minimum_similarity(value: float | None) -> float:
    if value is not None:
        return value
    configured = os.getenv("PRODUCT_CANDIDATE_MIN_SIMILARITY")
    if configured:
        try:
            return float(configured)
        except ValueError:
            logger.warning("invalid_product_candidate_min_similarity", extra={"value": configured})
    return DEFAULT_PRODUCT_CANDIDATE_MIN_SIMILARITY
