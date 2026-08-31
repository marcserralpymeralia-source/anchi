"""Shared order-scoring calculation used by every ingestion path."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """The score components persisted and displayed by the application."""

    total: float
    customer: float
    products: float
    quantities: float
    confidence: float
    coherence: float
    line_count: int
    validated_products: int
    valid_quantities: int


def _weight(settings, name: str) -> float:  # noqa: ANN001
    try:
        return max(float(getattr(settings, name, 0) or 0), 0.0)
    except (TypeError, ValueError):
        return 0.0


def is_positive_quantity(value) -> bool:  # noqa: ANN001
    try:
        number = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return False
    return isfinite(number) and number > 0


def _confidence(value) -> float:  # noqa: ANN001
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not isfinite(number):
        return 0.0
    return min(max(number, 0.0), 1.0)


def calculate_order_score(order, settings, *, use_proposals: bool = False) -> ScoreBreakdown:  # noqa: ANN001
    """Calculate a score from configured weights and validated evidence.

    Real orders only receive product points for ``validated_product_id``. A
    preview may opt into proposals because it has no persisted validation
    state yet.
    """

    lines = list(getattr(order, "lines", None) or [])
    line_count = len(lines)
    has_customer = bool(
        getattr(order, "validated_customer_id", None)
        or (use_proposals and getattr(order, "customer_id", None))
    )

    validated_products = 0
    valid_quantities = 0
    confidence_total = 0.0
    for line in lines:
        if hasattr(line, "validated_product_id"):
            product_is_validated = bool(getattr(line, "validated_product_id", None))
        else:
            product_is_validated = bool(getattr(line, "product_id", None))
        if use_proposals:
            product_is_validated = product_is_validated or bool(getattr(line, "product_id", None))
        validated_products += int(product_is_validated)
        valid_quantities += int(is_positive_quantity(getattr(line, "quantity", None)))
        confidence_total += _confidence(getattr(line, "extraction_confidence", 0.0))

    product_ratio = validated_products / line_count if line_count else 0.0
    quantity_ratio = valid_quantities / line_count if line_count else 0.0
    confidence_ratio = confidence_total / line_count if line_count else 0.0

    customer_score = _weight(settings, "customer_weight") if has_customer else 0.0
    products_score = _weight(settings, "products_weight") * product_ratio
    quantities_score = _weight(settings, "quantities_weight") * quantity_ratio
    confidence_score = _weight(settings, "llm_weight") * confidence_ratio
    coherence_score = _weight(settings, "coherence_weight") if line_count else 0.0
    total = min(
        max(customer_score + products_score + quantities_score + confidence_score + coherence_score, 0.0),
        100.0,
    )

    return ScoreBreakdown(
        total=round(total, 2),
        customer=round(customer_score, 2),
        products=round(products_score, 2),
        quantities=round(quantities_score, 2),
        confidence=round(confidence_score, 2),
        coherence=round(coherence_score, 2),
        line_count=line_count,
        validated_products=validated_products,
        valid_quantities=valid_quantities,
    )
