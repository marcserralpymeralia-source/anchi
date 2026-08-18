from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.platform import LearningService
from app.auth.dependencies import current_user
from app.core.templating import templates
from app.db.models import Customer, EmailSettings, LearnedAlias, LLMSettings, ManualCorrection, Order, Product, PromptTemplate, RagCase, RagDocument, ScoringSettings
from app.logs.service import log_action
from app.master.service import TenantUser
from app.settings.service import get_or_create_settings, update_with_form
from app.tenancy.database import get_tenant_db
from app.workbench.routes import _redirect_back

router = APIRouter()


def has_admin_access(user: TenantUser) -> bool:
    return user.role.name in {"Administrador", "Superadmin"}


def active_channels_for_company(db: Session, company_id: int) -> list[dict]:
    from app.db.models import InputChannel
    channels = db.scalars(select(InputChannel).where(InputChannel.company_id == company_id).order_by(InputChannel.is_default.desc(), InputChannel.name)).all()
    return [
        {
            "id": channel.id,
            "key": channel.key,
            "name": channel.name,
            "is_active": channel.is_active,
            "is_default": channel.is_default,
            "supports_text": channel.supports_text,
            "supports_attachments": channel.supports_attachments,
            "supports_audio": channel.supports_audio,
            "supports_documents": channel.supports_documents,
            "supports_images": getattr(channel, "supports_images", False),
        }
        for channel in channels
    ]


def _learning_person_label(user: TenantUser | None) -> str:
    return user.name if user else "Sistema"


def _learning_customer_label(customer: Customer | None) -> str:
    if not customer:
        return "Sin cliente"
    return f"{customer.code} · {customer.fiscal_name}"


def _learning_product_label(product: Product | None) -> str:
    if not product:
        return "Sin producto"
    return f"{product.reference} · {product.name}"


def _learning_order_label(order: Order | None) -> str:
    if not order:
        return "Sin pedido"
    return f"Pedido #{order.id}"


def _learning_content_excerpt(text: str | None, limit: int = 220) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _learning_status_label(value: str | None) -> str:
    mapping = {
        "pending": "Pendiente",
        "processing": "Procesando",
        "indexed": "Indexado",
        "ready": "Listo",
        "completed": "Procesado",
        "excluded": "Excluido",
        "error": "Error",
        "failed": "Fallido",
    }
    return mapping.get((value or "").lower(), value or "Sin estado")


def learning_overview(db: Session, company_id: int, limit: int = 12) -> dict:
    email_settings = get_or_create_settings(db, EmailSettings, company_id)
    llm_settings = get_or_create_settings(db, LLMSettings, company_id)
    scoring_settings = get_or_create_settings(db, ScoringSettings, company_id)
    pending_corrections = db.scalar(select(func.count()).select_from(ManualCorrection).where(ManualCorrection.company_id == company_id, ManualCorrection.should_learn == True)) or 0
    pending_aliases = db.scalar(select(func.count()).select_from(LearnedAlias).where(LearnedAlias.company_id == company_id, LearnedAlias.approved == False)) or 0
    approved_aliases = db.scalar(select(func.count()).select_from(LearnedAlias).where(LearnedAlias.company_id == company_id, LearnedAlias.approved == True)) or 0
    learned_corrections = db.scalar(select(func.count()).select_from(ManualCorrection).where(ManualCorrection.company_id == company_id, ManualCorrection.corrected_value.is_not(None), ManualCorrection.should_learn == False)) or 0
    documents_total = db.scalar(select(func.count()).select_from(RagDocument).where(RagDocument.company_id == company_id)) or 0
    documents_indexed = db.scalar(select(func.count()).select_from(RagDocument).where(RagDocument.company_id == company_id, RagDocument.embedding_status.in_(("indexed", "ready", "completed")))) or 0
    documents_pending = db.scalar(select(func.count()).select_from(RagDocument).where(RagDocument.company_id == company_id, RagDocument.embedding_status.in_(("pending", "processing")))) or 0
    documents_excluded = db.scalar(select(func.count()).select_from(RagDocument).where(RagDocument.company_id == company_id, RagDocument.embedding_status == "excluded")) or 0
    documents_errors = db.scalar(select(func.count()).select_from(RagDocument).where(RagDocument.company_id == company_id, RagDocument.embedding_status.in_(("error", "failed")))) or 0
    last_indexing_at = db.scalar(
        select(func.max(RagDocument.created_at))
        .where(RagDocument.company_id == company_id, RagDocument.embedding_status.in_(("indexed", "ready", "completed")))
    )
    import_jobs_total = 0
    import_jobs_errors = 0
    rag_cases_total = db.scalar(select(func.count()).select_from(RagCase).where(RagCase.company_id == company_id)) or 0
    document_rows = db.scalars(select(RagDocument).where(RagDocument.company_id == company_id).order_by(RagDocument.created_at.desc()).limit(limit)).all()
    case_rows = db.scalars(select(RagCase).where(RagCase.company_id == company_id).order_by(RagCase.created_at.desc()).limit(limit)).all()
    prompt_rows = db.scalars(select(PromptTemplate).where(PromptTemplate.company_id == company_id).order_by(PromptTemplate.purpose)).all()
    return {
        "summary": {
            "pending_suggestions": pending_corrections + pending_aliases,
            "pending_corrections": pending_corrections,
            "pending_aliases": pending_aliases,
            "approved_aliases": approved_aliases,
            "learned_corrections": learned_corrections,
            "documents_total": documents_total,
            "documents_indexed": documents_indexed,
            "documents_pending": documents_pending,
            "documents_excluded": documents_excluded,
            "last_indexing_at": last_indexing_at,
            "last_import_at": None,
            "import_jobs_total": import_jobs_total,
            "rag_cases_total": rag_cases_total,
            "learning_errors": documents_errors + import_jobs_errors,
            "human_review_required": email_settings.always_human_review,
            "agent_mode": llm_settings.agent_mode,
            "safety_level": llm_settings.safety_level,
            "safe_threshold": scoring_settings.safe_threshold,
            "review_threshold": scoring_settings.review_threshold,
            "doubtful_threshold": scoring_settings.doubtful_threshold,
        },
        "documents": [
            {
                "id": document.id,
                "title": document.title,
                "source_label": document.source_type.replace("_", " ").title(),
                "entity_label": document.source_entity.replace("_", " ").title(),
                "status": document.embedding_status,
                "status_label": _learning_status_label(document.embedding_status),
                "created_at": document.created_at,
                "excerpt": _learning_content_excerpt(document.content_text, 260),
            }
            for document in document_rows
        ],
        "cases": [
            {
                "id": case.id,
                "summary": case.summary,
                "resolved_action": case.resolved_action,
                "score": case.similarity_score,
                "created_at": case.created_at,
            }
            for case in case_rows
        ],
        "prompts": [{"id": row.id, "name": row.name, "purpose": row.purpose, "active": row.active_version_id is not None} for row in prompt_rows],
        "settings": {"llm": llm_settings, "scoring": scoring_settings, "email": email_settings},
    }


@router.get("/learning")
def learning_page(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not has_admin_access(user):
        return RedirectResponse("/", status_code=303)
    overview = learning_overview(db, user.company_id)
    active_tab = request.query_params.get("tab", "summary")
    if active_tab not in {"summary", "suggestions", "corrections", "documents", "histories", "config", "advanced"}:
        active_tab = "summary"
    technical_access = user.role.name == "Superadmin"
    if active_tab == "advanced" and not technical_access:
        active_tab = "summary"
    return templates.TemplateResponse(
        "learning/list.html",
        {
            "request": request,
            "user": user,
            "learning": overview,
            "active_channels": active_channels_for_company(db, user.company_id),
            "active_tab": active_tab,
            "technical_access": technical_access,
            "llm": overview["settings"]["llm"],
            "scoring": overview["settings"]["scoring"],
            "email": overview["settings"]["email"],
            "alert_center": request.state.alert_center,
        },
    )


@router.get("/knowledge")
def knowledge_redirect(user: TenantUser = Depends(current_user)):
    return RedirectResponse("/customers?view=knowledge", status_code=303)


@router.post("/learning/corrections/{correction_id}/accept")
def accept_learning_correction(correction_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not has_admin_access(user):
        return RedirectResponse("/learning", status_code=303)
    correction = db.get(ManualCorrection, correction_id)
    if correction and correction.company_id == user.company_id:
        if correction.corrected_value:
            LearningService().record_alias(db, company_id=user.company_id, alias_type=correction.entity_type, alias=correction.original_value or correction.field_name, canonical_value=correction.corrected_value, source=f"manual_correction:{correction.id}", confidence=0.9)
        correction.should_learn = False
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="learning.correction.accept", entity_type="manual_correction", entity_id=correction.id, message="Correccion aceptada como aprendizaje")
    return RedirectResponse(f"/learning?tab={request.query_params.get('tab', 'suggestions')}", status_code=303)


@router.post("/learning/corrections/{correction_id}/ignore")
def ignore_learning_correction(correction_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not has_admin_access(user):
        return RedirectResponse("/learning", status_code=303)
    correction = db.get(ManualCorrection, correction_id)
    if correction and correction.company_id == user.company_id:
        correction.should_learn = False
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="learning.correction.ignore", entity_type="manual_correction", entity_id=correction.id, message="Correccion ignorada")
    return RedirectResponse(f"/learning?tab={request.query_params.get('tab', 'suggestions')}", status_code=303)


@router.post("/learning/aliases/{alias_id}/approve")
def approve_learning_alias(alias_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not has_admin_access(user):
        return RedirectResponse("/learning", status_code=303)
    alias = db.scalar(select(LearnedAlias).where(LearnedAlias.id == alias_id, LearnedAlias.company_id == user.company_id))
    if alias:
        alias.approved = True
        alias.approved_by = user.id
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="learning.alias.approve", entity_type="learned_alias", entity_id=alias.id, message="Alias aprobado")
    return RedirectResponse(f"/learning?tab={request.query_params.get('tab', 'suggestions')}", status_code=303)


@router.post("/learning/aliases/{alias_id}/ignore")
def ignore_learning_alias(alias_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not has_admin_access(user):
        return RedirectResponse("/learning", status_code=303)
    alias = db.scalar(select(LearnedAlias).where(LearnedAlias.id == alias_id, LearnedAlias.company_id == user.company_id))
    if alias:
        alias.approved = False
        alias.approved_by = None
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="learning.alias.ignore", entity_type="learned_alias", entity_id=alias.id, message="Alias ignorado")
    return RedirectResponse(f"/learning?tab={request.query_params.get('tab', 'suggestions')}", status_code=303)


@router.post("/learning/documents")
def create_learning_document(title: str = Form(...), content_text: str = Form(""), source_type: str = Form("manual"), source_entity: str = Form("knowledge_note"), source_entity_id: int = Form(0), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not has_admin_access(user):
        return RedirectResponse("/learning", status_code=303)
    title_value = title.strip()
    if not title_value:
        return RedirectResponse("/learning?tab=documents", status_code=303)
    document = RagDocument(company_id=user.company_id, source_type=source_type.strip() or "manual", source_entity=source_entity.strip() or "knowledge_note", source_entity_id=source_entity_id or None, title=title_value, content_text=content_text.strip(), metadata_json=None, embedding_status="pending")
    db.add(document)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="learning.document.create", entity_type="rag_document", entity_id=document.id, message=f"Documento creado: {document.title}")
    return RedirectResponse("/learning?tab=documents", status_code=303)


@router.post("/learning/documents/{document_id}/index")
def index_learning_document(document_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not has_admin_access(user):
        return RedirectResponse("/learning", status_code=303)
    document = db.get(RagDocument, document_id)
    if document and document.company_id == user.company_id:
        document.embedding_status = "indexed"
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="learning.document.index", entity_type="rag_document", entity_id=document.id, message=f"Documento indexado: {document.title}")
    return RedirectResponse(f"/learning?tab={request.query_params.get('tab', 'documents')}", status_code=303)


@router.post("/learning/documents/{document_id}/exclude")
def exclude_learning_document(document_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not has_admin_access(user):
        return RedirectResponse("/learning", status_code=303)
    document = db.get(RagDocument, document_id)
    if document and document.company_id == user.company_id:
        document.embedding_status = "excluded"
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="learning.document.exclude", entity_type="rag_document", entity_id=document.id, message=f"Documento excluido: {document.title}")
    return RedirectResponse(f"/learning?tab={request.query_params.get('tab', 'documents')}", status_code=303)


@router.post("/learning/documents/{document_id}/delete")
def delete_learning_document(document_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not has_admin_access(user):
        return RedirectResponse("/learning", status_code=303)
    document = db.get(RagDocument, document_id)
    if document and document.company_id == user.company_id:
        title = document.title
        db.delete(document)
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="learning.document.delete", entity_type="rag_document", entity_id=document_id, message=f"Documento eliminado: {title}")
    return RedirectResponse(f"/learning?tab={request.query_params.get('tab', 'documents')}", status_code=303)


@router.post("/learning/config")
async def update_learning_config(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    if not has_admin_access(user):
        return RedirectResponse("/learning", status_code=303)
    form = dict(await request.form())
    llm = get_or_create_settings(db, LLMSettings, user.company_id)
    scoring = get_or_create_settings(db, ScoringSettings, user.company_id)
    email = get_or_create_settings(db, EmailSettings, user.company_id)
    for field in ["allow_auto_confirm", "allow_auto_export", "can_read_email", "can_extract_pdf", "can_classify_email", "can_extract_order", "can_suggest_customer", "can_suggest_products", "can_calculate_score", "can_create_pending_order", "can_mark_no_order", "use_same_model_for_all", "detailed_llm_logs", "store_llm_payloads", "anonymize_llm_logs", "debug_mode", "always_human_review"]:
        form.setdefault(field, "off")
    update_with_form(llm, form)
    update_with_form(scoring, form)
    update_with_form(email, form)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="learning.config.update", entity_type="settings", entity_id=llm.id, message="Configuracion de aprendizaje actualizada")
    return RedirectResponse("/learning?tab=config", status_code=303)
