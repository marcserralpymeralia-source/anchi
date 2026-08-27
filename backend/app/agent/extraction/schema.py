from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ORDER_EXTRACTION_SCHEMA_VERSION = "1.0"
SourceType = Literal["email", "pdf", "whatsapp", "voice", "social", "manual", "unknown"]
FieldSource = Literal["expressed", "inferred", "unknown"]

FORBIDDEN_EXTRACTION_KEYS = {
    "customer_id",
    "customerId",
    "product_id",
    "productId",
    "sage_customer_id",
    "sageCustomerId",
    "sage_product_id",
    "sageProductId",
    "sage_reference",
    "sageReference",
    "codigo_cliente",
    "codigoCliente",
    "referencia_sage",
    "referenciaSage",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ExtractionUncertainty(StrictModel):
    field: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)


class ExtractedCustomer(StrictModel):
    raw_name: str | None = Field(..., alias="rawName")
    raw_name_source: FieldSource = Field(..., alias="rawNameSource")


class ExtractedOrderLine(StrictModel):
    raw_text: str = Field(..., alias="rawText", min_length=1)
    raw_description: str | None = Field(..., alias="rawDescription")
    raw_description_source: FieldSource = Field(..., alias="rawDescriptionSource")
    reference: str | None = Field(
        default=None,
        description="Código o referencia del producto si aparece literalmente en el documento.",
    )
    reference_source: FieldSource = Field(
        default="unknown",
        alias="referenceSource",
    )
    quantity: float | None = Field(...)
    quantity_source: FieldSource = Field(..., alias="quantitySource")
    unit: str | None = Field(...)
    unit_source: FieldSource = Field(..., alias="unitSource")
    notes: list[str] = Field(default_factory=list)
    uncertainties: list[ExtractionUncertainty] = Field(default_factory=list)
    requires_review: bool = Field(..., alias="requiresReview")


class OrderExtraction(StrictModel):
    is_order: bool = Field(..., alias="isOrder")
    customer: ExtractedCustomer
    lines: list[ExtractedOrderLine]
    notes: list[str] = Field(default_factory=list)
    uncertainties: list[ExtractionUncertainty] = Field(default_factory=list)
    requires_review: bool = Field(..., alias="requiresReview")

    @model_validator(mode="after")
    def require_lines_only_for_orders(self) -> "OrderExtraction":
        if not self.is_order and self.lines:
            raise ValueError("Una entrada no pedido no debe incluir lineas de pedido.")
        if self.is_order and not self.lines:
            raise ValueError("Un pedido debe incluir al menos una linea extraida.")
        return self


class OrderExtractionInput(StrictModel):
    text: str = Field(..., min_length=1)
    source_type: SourceType = Field(default="unknown", alias="sourceType")
    source_id: str | None = Field(default=None, alias="sourceId")
    attachment_text: str | None = Field(default=None, alias="attachmentText")
    conversation_context: str | None = Field(default=None, alias="conversationContext")

    def combined_text(self) -> str:
        parts = [self.text.strip()]
        if self.attachment_text:
            parts.append(f"Texto de adjuntos:\n{self.attachment_text.strip()}")
        if self.conversation_context:
            parts.append(f"Contexto de conversacion:\n{self.conversation_context.strip()}")
        return "\n\n".join(part for part in parts if part)


class OrderExtractionResult(StrictModel):
    raw_input: OrderExtractionInput = Field(..., alias="rawInput")
    extracted_data: OrderExtraction = Field(..., alias="extractedData")
    model: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    schema_version: str = Field(default=ORDER_EXTRACTION_SCHEMA_VERSION, alias="schemaVersion")


def order_extraction_json_schema() -> dict[str, Any]:
    schema = OrderExtraction.model_json_schema(by_alias=True)
    _make_openai_strict_schema(schema)
    return schema


def assert_no_erp_identifiers(value: Any) -> None:
    if isinstance(value, dict):
        forbidden = FORBIDDEN_EXTRACTION_KEYS.intersection(value.keys())
        if forbidden:
            raise ValueError(f"La extraccion no puede incluir identificadores ERP: {', '.join(sorted(forbidden))}")
        for child in value.values():
            assert_no_erp_identifiers(child)
    elif isinstance(value, list):
        for child in value:
            assert_no_erp_identifiers(child)


def _make_openai_strict_schema(value: Any) -> None:
    if isinstance(value, dict):
        if value.get("type") == "object":
            value.setdefault("additionalProperties", False)
            properties = value.get("properties")
            if isinstance(properties, dict):
                value["required"] = list(properties.keys())
        for child in value.values():
            _make_openai_strict_schema(child)
    elif isinstance(value, list):
        for child in value:
            _make_openai_strict_schema(child)
