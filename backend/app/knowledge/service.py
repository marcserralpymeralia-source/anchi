from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import KnowledgeEntry, ManualCorrection, Product, ProductAlias, utcnow
from app.semantic_retrieval.embeddings import EmbeddingProvider, OpenAIEmbeddingProvider, embedding_model
from app.semantic_retrieval.products import cosine_similarity, normalize_embedding_text, parse_embedding_json


logger = logging.getLogger(__name__)
KNOWLEDGE_EMBEDDING_VERSION = "knowledge-v1"
DEFAULT_KNOWLEDGE_MIN_SIMILARITY = 0.68
VALID_SCOPES = {"global", "customer", "product"}


@dataclass(frozen=True)
class KnowledgeEvidence:
    id: int
    source_type: str
    source_id: str
    content: str
    relevance: float
    customer_id: int | None = None
    product_id: int | None = None
    scope: str = "global"
    created_at: datetime | None = None
    metadata: dict | None = None


@dataclass(frozen=True)
class KnowledgeIndexStats:
    scanned: int = 0
    indexed: int = 0
    skipped: int = 0
    failed: int = 0


def build_knowledge_embedding_text(entry: KnowledgeEntry) -> str:
    fields = [
        ("Tipo", entry.source_type),
        ("Ambito", entry.scope),
        ("Cliente", entry.customer_id),
        ("Producto", entry.product_id),
        ("Texto", entry.content),
    ]
    return " | ".join(f"{label}: {clean}" for label, value in fields if (clean := normalize_embedding_text(value)))


def knowledge_content_hash(embedding_text: str, *, model: str, version: str = KNOWLEDGE_EMBEDDING_VERSION) -> str:
    digest = hashlib.sha256()
    digest.update(version.encode("utf-8"))
    digest.update(b"\0")
    digest.update(model.encode("utf-8"))
    digest.update(b"\0")
    digest.update(embedding_text.encode("utf-8"))
    return digest.hexdigest()


def create_knowledge_entry(
    db: Session,
    *,
    company_id: int,
    source_type: str,
    source_id: str,
    content: str,
    scope: str = "global",
    customer_id: int | None = None,
    product_id: int | None = None,
    metadata: dict | None = None,
) -> KnowledgeEntry:
    normalized_scope = scope if scope in VALID_SCOPES else "global"
    if customer_id is not None:
        normalized_scope = "customer"
    entry = db.scalar(
        select(KnowledgeEntry).where(
            KnowledgeEntry.company_id == company_id,
            KnowledgeEntry.source_type == source_type,
            KnowledgeEntry.source_id == source_id,
        )
    )
    now = utcnow()
    if entry is None:
        entry = KnowledgeEntry(company_id=company_id, source_type=source_type, source_id=source_id)
        db.add(entry)
    entry.scope = normalized_scope
    entry.content = normalize_embedding_text(content)
    entry.customer_id = customer_id
    entry.product_id = product_id
    entry.metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
    entry.updated_at = now
    db.flush()
    return entry


def index_knowledge_entries(
    db: Session,
    *,
    company_id: int,
    provider: EmbeddingProvider | None = None,
    model: str | None = None,
    version: str = KNOWLEDGE_EMBEDDING_VERSION,
    batch_size: int = 64,
) -> KnowledgeIndexStats:
    selected_model = model or embedding_model()
    selected_provider = provider or OpenAIEmbeddingProvider()
    entries = list(db.scalars(select(KnowledgeEntry).where(KnowledgeEntry.company_id == company_id).order_by(KnowledgeEntry.id.asc())))
    pending: list[tuple[KnowledgeEntry, str, str]] = []
    skipped = 0
    for entry in entries:
        embedding_text = build_knowledge_embedding_text(entry)
        content_hash = knowledge_content_hash(embedding_text, model=selected_model, version=version)
        if entry.embedding_model == selected_model and entry.embedding_version == version and entry.content_hash == content_hash:
            skipped += 1
            continue
        pending.append((entry, embedding_text, content_hash))

    indexed = 0
    failed = 0
    for start in range(0, len(pending), max(batch_size, 1)):
        batch = pending[start : start + max(batch_size, 1)]
        try:
            vectors = selected_provider.generate_embeddings([item[1] for item in batch], model=selected_model)
        except Exception as exc:  # noqa: BLE001
            failed += len(batch)
            logger.warning("knowledge_embedding_batch_failed", extra={"company_id": company_id, "count": len(batch), "error_type": type(exc).__name__})
            continue
        if len(vectors) != len(batch):
            failed += len(batch)
            logger.warning("knowledge_embedding_batch_size_mismatch", extra={"company_id": company_id, "expected": len(batch), "received": len(vectors)})
            continue
        for (entry, embedding_text, content_hash), vector in zip(batch, vectors, strict=True):
            clean_vector = [float(value) for value in vector]
            now = utcnow()
            entry.embedding_json = json.dumps(clean_vector)
            entry.embedding_text = embedding_text
            entry.embedding_model = selected_model
            entry.embedding_version = version
            entry.content_hash = content_hash
            entry.dimensions = len(clean_vector)
            entry.embedded_at = now
            entry.updated_at = now
            indexed += 1
    db.commit()
    stats = KnowledgeIndexStats(scanned=len(entries), indexed=indexed, skipped=skipped, failed=failed)
    logger.info("knowledge_entries_indexed", extra={"company_id": company_id, "stats": stats.__dict__})
    return stats


def retrieve_knowledge(
    db: Session,
    *,
    company_id: int,
    query: str,
    customer_id: int | None = None,
    product_ids: Sequence[int] | None = None,
    source_types: Sequence[str] | None = None,
    limit: int = 10,
    minimum_similarity: float | None = None,
    provider: EmbeddingProvider | None = None,
    model: str | None = None,
    version: str = KNOWLEDGE_EMBEDDING_VERSION,
) -> list[KnowledgeEvidence]:
    normalized_query = normalize_embedding_text(query)
    if not normalized_query:
        return []
    selected_model = model or embedding_model()
    product_id_set = {int(product_id) for product_id in product_ids or [] if product_id}
    conditions = [
        KnowledgeEntry.company_id == company_id,
        KnowledgeEntry.embedding_json.is_not(None),
        KnowledgeEntry.embedding_model == selected_model,
        KnowledgeEntry.embedding_version == version,
    ]
    if source_types:
        conditions.append(KnowledgeEntry.source_type.in_(list(source_types)))
    if customer_id is not None:
        conditions.append(or_(KnowledgeEntry.customer_id.is_(None), KnowledgeEntry.customer_id == customer_id))
    else:
        conditions.append(KnowledgeEntry.customer_id.is_(None))
    if product_id_set:
        conditions.append(or_(KnowledgeEntry.product_id.is_(None), KnowledgeEntry.product_id.in_(product_id_set)))
    entries = list(db.scalars(select(KnowledgeEntry).where(*conditions)))
    if not entries:
        return []

    threshold = _minimum_similarity(minimum_similarity)
    selected_provider = provider or OpenAIEmbeddingProvider()
    try:
        query_vector = selected_provider.generate_embedding(normalized_query, model=selected_model)
    except Exception as exc:  # noqa: BLE001
        logger.info("knowledge_retrieval_unavailable", extra={"company_id": company_id, "error_type": type(exc).__name__})
        return []

    evidences: list[KnowledgeEvidence] = []
    for entry in entries:
        similarity = cosine_similarity(query_vector, parse_embedding_json(entry.embedding_json or "[]"))
        if similarity < threshold:
            continue
        relevance = min(1.0, similarity + _context_boost(entry, customer_id=customer_id, product_ids=product_id_set))
        evidences.append(
            KnowledgeEvidence(
                id=entry.id,
                source_type=entry.source_type,
                source_id=entry.source_id,
                content=entry.content,
                relevance=round(relevance, 4),
                customer_id=entry.customer_id,
                product_id=entry.product_id,
                scope=entry.scope,
                created_at=entry.created_at,
                metadata=_metadata(entry),
            )
        )
    evidences.sort(key=lambda item: (item.relevance, item.customer_id == customer_id, item.product_id in product_id_set), reverse=True)
    limited = evidences[: max(limit, 0)]
    logger.info(
        "knowledge_retrieved",
        extra={
            "company_id": company_id,
            "query_length": len(normalized_query),
            "has_customer_filter": customer_id is not None,
            "product_filter_count": len(product_id_set),
            "source_type_count": len(source_types or []),
            "result_count": len(limited),
            "result_ids": [item.id for item in limited],
            "relevance_scores": [item.relevance for item in limited],
        },
    )
    return limited


def retrieve_product_knowledge(
    db: Session,
    *,
    company_id: int,
    raw_description: str,
    customer_id: int | None = None,
    candidate_product_ids: Sequence[int] | None = None,
    limit: int = 10,
    minimum_similarity: float | None = None,
    provider: EmbeddingProvider | None = None,
    model: str | None = None,
) -> list[KnowledgeEvidence]:
    return retrieve_knowledge(
        db,
        company_id=company_id,
        query=raw_description,
        customer_id=customer_id,
        product_ids=candidate_product_ids,
        limit=limit,
        minimum_similarity=minimum_similarity,
        provider=provider,
        model=model,
    )


def record_human_correction(
    db: Session,
    *,
    company_id: int,
    raw_description: str,
    chosen_product_id: int | None,
    rejected_product_ids: Sequence[int] | None = None,
    customer_id: int | None = None,
    source_order_id: int | None = None,
    order_line_id: int | None = None,
    inbound_message_id: int | None = None,
    created_by_user_id: int | None = None,
    reason: str | None = None,
) -> KnowledgeEntry:
    correction = ManualCorrection(
        company_id=company_id,
        inbound_message_id=inbound_message_id,
        order_id=source_order_id,
        order_line_id=order_line_id,
        entity_type="product",
        field_name="validated_product_id",
        original_value=raw_description,
        corrected_value=str(chosen_product_id) if chosen_product_id is not None else None,
        corrected_entity_id=chosen_product_id,
        reason=reason,
        should_learn=True,
        created_by_user_id=created_by_user_id,
    )
    db.add(correction)
    db.flush()
    content = f'Correccion humana: "{normalize_embedding_text(raw_description)}" se corrigio al producto {chosen_product_id}.'
    metadata = {
        "rejected_product_ids": list(rejected_product_ids or []),
        "source_order_id": source_order_id,
        "order_line_id": order_line_id,
        "manual_correction_id": correction.id,
    }
    return create_knowledge_entry(
        db,
        company_id=company_id,
        source_type="human_correction",
        source_id=f"manual_correction:{correction.id}",
        content=content,
        scope="customer" if customer_id else "product",
        customer_id=customer_id,
        product_id=chosen_product_id,
        metadata=metadata,
    )


def add_product_alias(
    db: Session,
    *,
    company_id: int,
    product_id: int,
    alias: str,
    scope: str = "global",
    customer_id: int | None = None,
) -> KnowledgeEntry:
    normalized_alias = normalize_embedding_text(alias)
    product = db.get(Product, product_id)
    reference = product.reference if product else str(product_id)
    if scope == "global" and normalized_alias:
        existing_alias = db.scalar(
            select(ProductAlias).where(
                ProductAlias.company_id == company_id,
                ProductAlias.product_id == product_id,
                ProductAlias.alias == normalized_alias,
            )
        )
        if existing_alias is None:
            db.add(ProductAlias(company_id=company_id, product_id=product_id, alias=normalized_alias))
            db.flush()
    resolved_scope = "customer" if customer_id is not None else scope
    source_id = f"product_alias:{resolved_scope}:{customer_id or 'global'}:{product_id}:{hashlib.sha1(normalized_alias.encode('utf-8')).hexdigest()[:12]}"
    content = f'Alias de producto: "{normalized_alias}" se usa para la referencia {reference}.'
    return create_knowledge_entry(
        db,
        company_id=company_id,
        source_type="product_alias",
        source_id=source_id,
        content=content,
        scope=resolved_scope,
        customer_id=customer_id,
        product_id=product_id,
        metadata={"alias": normalized_alias, "scope": resolved_scope},
    )


def _context_boost(entry: KnowledgeEntry, *, customer_id: int | None, product_ids: set[int]) -> float:
    boost = 0.0
    if customer_id is not None and entry.customer_id == customer_id:
        boost += 0.08
    if product_ids and entry.product_id in product_ids:
        boost += 0.04
    if entry.source_type == "human_correction":
        boost += 0.03
    return boost


def _minimum_similarity(value: float | None) -> float:
    if value is not None:
        return value
    configured = os.getenv("KNOWLEDGE_MIN_SIMILARITY")
    if configured:
        try:
            return float(configured)
        except ValueError:
            logger.warning("invalid_knowledge_min_similarity", extra={"value": configured})
    return DEFAULT_KNOWLEDGE_MIN_SIMILARITY


def _metadata(entry: KnowledgeEntry) -> dict:
    if not entry.metadata_json:
        return {}
    try:
        parsed = json.loads(entry.metadata_json)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
