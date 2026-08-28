from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.agent.prompt_runtime import run_prompt_execution
from app.db.models import LLMSettings
from app.settings.integrations import call_openai
from app.settings.service import get_or_create_settings


@dataclass(slots=True)
class WhatsAppConversationSemanticResult:
    intent: str
    state: str
    missing_or_uncertain: list[str]
    reply_needed: bool
    suggested_reply: str
    confidence: float
    prompt_execution_id: int | None = None


def evaluate_whatsapp_conversation_semantics(
    db: Session,
    *,
    company_id: int,
    transcript: str,
    input_reference: str | None = None,
) -> WhatsAppConversationSemanticResult:
    transcript = str(transcript or "").strip()
    if not transcript:
        raise ValueError("La conversación no contiene texto evaluable.")

    settings = get_or_create_settings(db, LLMSettings, company_id)

    result = run_prompt_execution(
        db,
        company_id,
        "whatsapp_conversation",
        settings,
        transcript,
        provider_call=call_openai,
        input_reference=input_reference,
    )

    if not result.get("ok"):
        raise RuntimeError(
            result.get("message")
            or "Error evaluando semánticamente la conversación de WhatsApp."
        )

    if not result.get("validation_ok"):
        errors = result.get("validation_errors") or []
        detail = "; ".join(str(item) for item in errors if item)
        raise RuntimeError(
            detail
            or "La evaluación semántica de WhatsApp no pasó la validación."
        )

    data = result.get("validated_content") or {}

    return WhatsAppConversationSemanticResult(
        intent=str(data["intent"]).strip().lower(),
        state=str(data["state"]).strip().lower(),
        missing_or_uncertain=[
            str(item).strip()
            for item in data["missing_or_uncertain"]
            if str(item).strip()
        ],
        reply_needed=bool(data["reply_needed"]),
        suggested_reply=str(data["suggested_reply"] or "").strip(),
        confidence=float(data["confidence"]),
        prompt_execution_id=result.get("prompt_execution_id"),
    )
