from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.platform import LearningService
from app.db.models import (
    Alert,
    Company,
    Conversation,
    Customer,
    EmailAttachment,
    ExportFile,
    ExportJob,
    InboundMessage,
    InputChannel,
    ManualCorrection,
    Order,
    OrderLine,
    OrderReview,
    Product,
    RagCase,
    ScoringResult,
    ScoringSettings,
    User,
)
from app.messages.service import upsert_inbound_message
from app.whatsapp.service import get_or_create_whatsapp_channel


BASE_DIR = Path(__file__).resolve().parents[1]
TENANT_DB = BASE_DIR / "tenants" / "0002-mulet-hidalgo.db"
COMPANY_NAME = "Mulet Hidalgo"
ADMIN_EMAIL = "admin@gemavi.local"


@dataclass(slots=True)
class OrderSeed:
    external_id: str
    subject: str
    customer_code: str
    sender: str
    detected_name: str
    score: float
    status: str
    created_days_ago: int
    customer_score: float
    review_reasons: str
    notes: str
    transcript: list[dict[str, str]]
    lines: list[dict[str, object]]
    export_filename: str | None = None
    create_alert: bool = False
    create_correction: bool = False
    create_rag_case: bool = False


def _connect_args(database_url: str) -> dict[str, object]:
    return {"check_same_thread": False} if database_url.startswith("sqlite") else {}


def _now(days: int = 0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{TENANT_DB}", connect_args=_connect_args(f"sqlite:///{TENANT_DB}"))
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _load_company(db: Session) -> Company:
    company = db.scalar(select(Company).where(Company.name == COMPANY_NAME))
    if not company:
        raise RuntimeError(f"No se encontró la compañía {COMPANY_NAME!r} en {TENANT_DB}")
    return company


def _load_admin(db: Session, company_id: int) -> User:
    user = db.scalar(select(User).where(User.company_id == company_id, User.email == ADMIN_EMAIL))
    if not user:
        user = db.scalar(select(User).where(User.company_id == company_id).order_by(User.id.asc()))
    if not user:
        raise RuntimeError("No se encontró un usuario administrador para la demo.")
    return user


def _customer(db: Session, company_id: int, code: str) -> Customer:
    customer = db.scalar(select(Customer).where(Customer.company_id == company_id, Customer.code == code, Customer.deleted_at.is_(None)))
    if not customer:
        raise RuntimeError(f"No se encontró el cliente {code}")
    return customer


def _product(db: Session, company_id: int, reference: str) -> Product:
    product = db.scalar(select(Product).where(Product.company_id == company_id, Product.reference == reference, Product.deleted_at.is_(None)))
    if not product:
        raise RuntimeError(f"No se encontró el producto {reference}")
    return product


def _existing_order(db: Session, company_id: int, external_id: str) -> Order | None:
    return db.scalar(
        select(Order)
        .join(Conversation, Conversation.id == Order.conversation_id)
        .join(InboundMessage, InboundMessage.conversation_id == Conversation.id)
        .where(Order.company_id == company_id, InboundMessage.source_external_id == external_id)
    )


def _build_normalized_text(transcript: list[dict[str, str]]) -> str:
    return "\n".join(f"{msg['sender']}: {msg['text']}" for msg in transcript)


def _seed_transcript_messages(
    db: Session,
    *,
    company: Company,
    customer: Customer,
    order: Order,
    spec: OrderSeed,
    order_at: datetime,
    conversation_id: int,
) -> None:
    thread_id = f"thread-{spec.external_id}"
    for index, transcript_message in enumerate(spec.transcript):
        message_time = order_at + timedelta(minutes=index * 2)
        direction = transcript_message.get("direction") or "inbound"
        external_id = spec.external_id if index == 0 else f"{spec.external_id}-msg-{index + 1}"
        payload = {
            "import_type": "manual_whatsapp",
            "parsed": {
                "kind": "whatsapp",
                "channel": "whatsapp",
                "sender": transcript_message.get("sender") or spec.sender,
                "subject": spec.subject,
                "messages": [transcript_message],
                "participants": {
                    "client": spec.sender,
                    "company": COMPANY_NAME,
                },
                "warnings": [],
                "normalized_text": transcript_message.get("text") or "",
                "thread_key": thread_id,
                "message_index": index + 1,
            },
        }
        message, _conversation = upsert_inbound_message(
            db,
            company_id=company.id,
            channel_key="whatsapp",
            provider="whatsapp",
            external_id=external_id,
            sender=transcript_message.get("sender") or spec.sender,
            recipients=[COMPANY_NAME] if direction == "inbound" else [spec.sender],
            subject=spec.subject,
            text_content=transcript_message.get("text") or "",
            external_thread_id=thread_id,
            direction=direction,
            received_at=message_time,
            metadata=payload,
            content_type="whatsapp_text",
            has_attachments=False,
        )
        message.conversation_id = conversation_id
        message.order_id = order.id
        message.customer_id = customer.id
        message.provider = "whatsapp"
        message.direction = direction
        message.source_thread_id = thread_id
        message.source_message_id = external_id
        message.original_content = transcript_message.get("text") or ""
        message.normalized_text = transcript_message.get("text") or ""
        message.raw_payload_json = json.dumps(payload, ensure_ascii=False)
        message.status = "received" if direction == "inbound" else "processed"
        message.processing_step = "received" if direction == "inbound" else "completed"
        message.detected_type = "pedido"
        message.score = spec.score
        message.has_attachments = False
        message.has_pdf = False
        message.last_processed_at = message_time


def _seed_order(db: Session, company: Company, admin: User, channel: InputChannel, spec: OrderSeed) -> tuple[Order, bool]:
    existing = _existing_order(db, company.id, spec.external_id)
    if existing:
        customer = _customer(db, company.id, spec.customer_code)
        conversation = db.get(Conversation, existing.conversation_id) if existing.conversation_id else None
        if conversation:
            existing_thread_id = f"thread-{spec.external_id}"
            message_count = db.scalar(
                select(func.count(InboundMessage.id)).where(
                    InboundMessage.conversation_id == conversation.id,
                    InboundMessage.source_thread_id == existing_thread_id,
                )
            ) or 0
            if int(message_count) < len(spec.transcript):
                _seed_transcript_messages(
                    db,
                    company=company,
                    customer=customer,
                    order=existing,
                    spec=spec,
                    order_at=existing.created_at or _now(spec.created_days_ago),
                    conversation_id=conversation.id,
                )
                db.commit()
        return existing, False

    customer = _customer(db, company.id, spec.customer_code)
    order_at = _now(spec.created_days_ago)
    normalized_text = _build_normalized_text(spec.transcript)
    dedupe_hash = f"{company.id}|{spec.external_id}"
    payload = {
        "import_type": "manual_whatsapp",
        "parsed": {
            "kind": "whatsapp",
            "channel": "whatsapp",
            "sender": spec.sender,
            "subject": spec.subject,
            "messages": spec.transcript,
            "participants": {
                "client": spec.sender,
                "company": COMPANY_NAME,
            },
            "warnings": [],
            "normalized_text": normalized_text,
            "dedupe_hash": dedupe_hash,
            "thread_key": f"thread-{spec.external_id}",
        },
    }
    message, conversation = upsert_inbound_message(
        db,
        company_id=company.id,
        channel_key="whatsapp",
        provider="whatsapp",
        external_id=spec.external_id,
        sender=spec.sender,
        recipients=[COMPANY_NAME],
        subject=spec.subject,
        text_content=normalized_text,
        external_thread_id=f"thread-{spec.external_id}",
        received_at=order_at,
        metadata=payload,
        content_type="whatsapp_text",
        has_attachments=False,
    )
    order = Order(
        company_id=company.id,
        conversation_id=conversation.id,
        customer_id=customer.id,
        validated_customer_id=customer.id,
        customer_detected_name=spec.detected_name,
        customer_identification_method="whatsapp",
        customer_score=spec.customer_score,
        order_date=order_at.date().isoformat(),
        requested_delivery_date=(order_at + timedelta(days=1)).date().isoformat(),
        notes=spec.notes,
        score=spec.score,
        status=spec.status,
        review_reasons=spec.review_reasons,
        created_at=order_at,
        confirmed_at=order_at if spec.status in {"pedido_confirmado", "pedido_exportado"} else None,
        exported_at=order_at if spec.status == "pedido_exportado" else None,
    )
    db.add(order)
    db.flush()

    _seed_transcript_messages(
        db,
        company=company,
        customer=customer,
        order=order,
        spec=spec,
        order_at=order_at,
        conversation_id=conversation.id,
    )

    message.conversation_id = conversation.id
    message.order_id = order.id
    message.customer_id = customer.id
    message.provider = "whatsapp"
    message.source_thread_id = f"thread-{spec.external_id}"
    message.source_message_id = spec.external_id
    message.original_content = spec.transcript[0]["text"] if spec.transcript else normalized_text
    message.normalized_text = normalized_text
    message.raw_payload_json = json.dumps(payload, ensure_ascii=False)
    message.status = "order_detected"
    message.processing_step = "completed"
    message.detected_type = "pedido"
    message.score = spec.score
    message.has_attachments = False
    message.has_pdf = False
    message.last_processed_at = order_at

    for index, line_data in enumerate(spec.lines, start=1):
        product = db.get(Product, line_data.get("product_id")) if line_data.get("product_id") else None
        line = OrderLine(
            company_id=company.id,
            order_id=order.id,
            product_id=product.id if product else None,
            validated_product_id=product.id if product and line_data.get("validated", True) else None,
            original_text=line_data.get("original_text") or "",
            detected_reference=line_data.get("reference"),
            detected_product=line_data.get("name"),
            quantity=line_data.get("quantity"),
            unit=line_data.get("unit"),
            extraction_confidence=line_data.get("confidence", 0.8),
            line_score=line_data.get("line_score", spec.score),
            validation_status=line_data.get("validation_status", "validated" if product and line_data.get("validated", True) else "pending"),
            doubt_reason=line_data.get("doubt_reason"),
        )
        db.add(line)
        db.flush()
        if product and spec.score >= 70:
            LearningService().update_customer_product_knowledge(
                db,
                company_id=company.id,
                customer=customer,
                product=product,
                quantity=line.quantity,
                unit=line.unit,
                order=order,
                order_at=order_at,
                source_context="pedido_confirmado",
                customer_alias_used=spec.sender,
                comments=spec.notes,
                is_manual=False,
                exported_at=order_at if spec.status == "pedido_exportado" else None,
            )

    db.add(
        OrderReview(
            company_id=company.id,
            order_id=order.id,
            reviewer_user_id=admin.id,
            status="approved" if spec.score >= 80 else "pending",
            comments=spec.review_reasons,
            reviewed_at=order_at if spec.score >= 80 else None,
        )
    )
    db.add(
        ScoringResult(
            company_id=company.id,
            inbound_message_id=message.id,
            order_id=order.id,
            total_score=spec.score,
            customer_score=min(100, spec.customer_score * 100),
            product_score=min(100, max(20, spec.score - 18)),
            confidence_score=min(100, max(10, spec.score - 12)),
            rule_score=min(100, max(5, spec.score - 25)),
            block_reason=None if spec.score >= 50 else "Score bajo para demo",
            details_json=json.dumps({"demo": True, "external_id": spec.external_id, "transcript_messages": len(spec.transcript)}, ensure_ascii=False),
        )
    )
    if spec.export_filename and spec.status == "pedido_exportado":
        db.add(ExportFile(company_id=company.id, order_id=order.id, filename=spec.export_filename, content="demo-export", status="generated"))
        db.add(
            ExportJob(
                company_id=company.id,
                order_id=order.id,
                file_path=spec.export_filename,
                export_format="csv",
                destination_type="sftp",
                status="completed",
                status_message="Exportación demo completada",
                payload_json=json.dumps({"demo": True}, ensure_ascii=False),
                exported_at=order_at,
            )
        )
    if spec.create_alert:
        db.add(
            Alert(
                company_id=company.id,
                inbound_message_id=message.id,
                order_id=order.id,
                alert_type="order_review_required",
                severity="medium" if spec.score >= 45 else "high",
                status="open",
                title=f"Revisar pedido demo: {spec.subject}",
                message=spec.review_reasons,
                payload_json=json.dumps({"demo": True, "score": spec.score}, ensure_ascii=False),
            )
        )
    if spec.create_correction:
        db.add(
            ManualCorrection(
                company_id=company.id,
                inbound_message_id=message.id,
                order_id=order.id,
                entity_type="customer",
                field_name="validated_customer_id",
                original_value=spec.sender,
                corrected_value=customer.fiscal_name,
                agent_value=spec.detected_name,
                corrected_entity_id=customer.id,
                reason="Corrección demo para entrenamiento",
                should_learn=True,
                created_by_user_id=admin.id,
            )
        )
    if spec.create_rag_case:
        db.add(
            RagCase(
                company_id=company.id,
                inbound_message_id=message.id,
                order_id=order.id,
                customer_id=customer.id,
                summary=spec.review_reasons,
                resolved_action="confirmar_pedido" if spec.score >= 70 else "revisar_manual",
                resolution_json=json.dumps({"demo": True, "status": spec.status}, ensure_ascii=False),
                similarity_score=min(0.99, spec.score / 100),
            )
        )

    return order, True


def seed_mulet_demo_whatsapp_orders() -> dict[str, int]:
    SessionLocal = _session_factory()
    with SessionLocal() as db:
        company = _load_company(db)
        admin = _load_admin(db, company.id)
        channel = get_or_create_whatsapp_channel(db, company.id)
        channel.is_active = True
        channel.is_default = False

        product_ids = {reference: _product(db, company.id, reference).id for reference in [
            "100.0",
            "100.11",
            "102.1",
            "102.8",
            "103.0",
            "103.11",
            "104.11",
            "105.0",
            "106.11",
            "107.0",
            "222.0",
            "3.0",
            "101.0",
            "MH-010",
            "MH-020",
        ]}

        specs = [
            OrderSeed(
                external_id="mulet-demo-wa-01",
                subject="Pedido semanal perfecto",
                customer_code="430000307",
                sender="CollVerd - compres",
                detected_name="CollVerd",
                score=96,
                status="pedido_exportado",
                created_days_ago=0,
                customer_score=0.98,
                review_reasons="Cliente y productos identificados con coincidencia alta. Pedido listo para exportar.",
                notes="WhatsApp muy claro con referencias exactas y cantidades repetidas.",
                transcript=[
                    {"sender": "CollVerd", "direction": "inbound", "text": "Bon dia. Avui fem 18 u. de 100.0 BISTEC TALLAT CROSTO i 12 u. de 103.0 ENTRECOT PART AMPLA TALLAT.", "timestamp_label": "09:12"},
                    {"sender": "Mulet Hidalgo", "direction": "outbound", "text": "Perfecte, ho deixo preparat i només reviso el transport.", "timestamp_label": "09:14"},
                    {"sender": "CollVerd", "direction": "inbound", "text": "Sí, i manteniu també 2 u. de 105.0 COSTELLA DE VEDELLA TALLADA.", "timestamp_label": "09:15"},
                ],
                lines=[
                    {"product_id": product_ids["100.0"], "reference": "100.0", "name": "BISTEC TALLAT CROSTO", "quantity": 18, "unit": "u", "confidence": 0.99, "original_text": "18 u. 100.0 BISTEC TALLAT CROSTO", "validated": True},
                    {"product_id": product_ids["103.0"], "reference": "103.0", "name": "ENTRECOT PART AMPLA TALLAT", "quantity": 12, "unit": "u", "confidence": 0.98, "original_text": "12 u. 103.0 ENTRECOT PART AMPLA TALLAT", "validated": True},
                    {"product_id": product_ids["105.0"], "reference": "105.0", "name": "COSTELLA DE VEDELLA TALLADA", "quantity": 2, "unit": "u", "confidence": 0.97, "original_text": "2 u. 105.0 COSTELLA DE VEDELLA TALLADA", "validated": True},
                ],
                export_filename="MULET_WA_01.csv",
                create_rag_case=True,
            ),
            OrderSeed(
                external_id="mulet-demo-wa-02",
                subject="Pedido con detalle medio",
                customer_code="430000018",
                sender="La Plaça - compras",
                detected_name="BAR LA PLAÇA -XERTA",
                score=88,
                status="pedido_confirmado",
                created_days_ago=1,
                customer_score=0.92,
                review_reasons="Pedido claro con una combinación de referencias estándar y una entrega simple.",
                notes="WhatsApp directo y muy interpretable.",
                transcript=[
                    {"sender": "BAR LA PLAÇA -XERTA", "direction": "inbound", "text": "Bon dia, per demà poseu 10 de 104.11 FILET DE VEDELLA TALLAT I AL BUIT i 6 de 106.11 CONILL DE VEDELLA AL BUIT.", "timestamp_label": "08:41"},
                    {"sender": "Mulet Hidalgo", "direction": "outbound", "text": "Confirmat, surt amb la ruta de primera hora.", "timestamp_label": "08:43"},
                ],
                lines=[
                    {"product_id": product_ids["104.11"], "reference": "104.11", "name": "FILET DE VEDELLA TALLAT I AL BUIT", "quantity": 10, "unit": "u", "confidence": 0.96, "original_text": "10 u. 104.11 FILET DE VEDELLA TALLAT I AL BUIT"},
                    {"product_id": product_ids["106.11"], "reference": "106.11", "name": "CONILL DE VEDELLA AL BUIT", "quantity": 6, "unit": "u", "confidence": 0.95, "original_text": "6 u. 106.11 CONILL DE VEDELLA AL BUIT"},
                ],
                create_rag_case=True,
            ),
            OrderSeed(
                external_id="mulet-demo-wa-03",
                subject="Pedido con una línea de servicio",
                customer_code="430000033",
                sender="Hotel Juanito Platja",
                detected_name="HOTEL JUANITO PLATJA",
                score=81,
                status="pedido_pendiente_revision",
                created_days_ago=1,
                customer_score=0.84,
                review_reasons="Pedido interpretable pero con un servicio logístico que conviene validar manualmente.",
                notes="Incluye servicio y cantidades mixtas.",
                transcript=[
                    {"sender": "Hotel Juanito Platja", "direction": "inbound", "text": "Ens deixes 14 de 107.0 ESTOFAT DE VEDELLA i 1 servei de transport, si us plau?", "timestamp_label": "10:05"},
                    {"sender": "Mulet Hidalgo", "direction": "outbound", "text": "Sí, t'ho preparo. El servei queda pendent de validar.", "timestamp_label": "10:07"},
                ],
                lines=[
                    {"product_id": product_ids["107.0"], "reference": "107.0", "name": "ESTOFAT DE VEDELLA", "quantity": 14, "unit": "u", "confidence": 0.91, "original_text": "14 u. 107.0 ESTOFAT DE VEDELLA"},
                    {"product_id": product_ids["3.0"], "reference": "3.0", "name": "PROVA", "quantity": 1, "unit": "u", "confidence": 0.87, "original_text": "1 u. 3.0 PROVA com a extra", "validated": False, "doubt_reason": "Producto demo de apoyo"},
                ],
                create_alert=True,
            ),
            OrderSeed(
                external_id="mulet-demo-wa-04",
                subject="Pedido con referencia parcial",
                customer_code="430000049",
                sender="Criscar Cafeteria",
                detected_name="CRISCAR CAFETERIA",
                score=72,
                status="dudoso",
                created_days_ago=2,
                customer_score=0.76,
                review_reasons="Hay buena intención de pedido, pero una de las líneas llega con redacción parcial.",
                notes="La conversación permite inferir parte del pedido, pero no todo.",
                transcript=[
                    {"sender": "CRISCAR CAFETERIA", "direction": "inbound", "text": "Hola, necessito 20 de 102.8 TOMAHAWK i també el de sempre del menú.", "timestamp_label": "09:30"},
                    {"sender": "Mulet Hidalgo", "direction": "outbound", "text": "¿Te refieres al 101.0 BISTEC MENÚ?", "timestamp_label": "09:31"},
                    {"sender": "CRISCAR CAFETERIA", "direction": "inbound", "text": "Sí, exacto, 30 unitats del menú.", "timestamp_label": "09:33"},
                ],
                lines=[
                    {"product_id": product_ids["102.8"], "reference": "102.8", "name": "TOMAHAWK", "quantity": 20, "unit": "u", "confidence": 0.88, "original_text": "20 u. 102.8 TOMAHAWK"},
                    {"product_id": product_ids["101.0"], "reference": "101.0", "name": "BISTEC MENÚ", "quantity": 30, "unit": "u", "confidence": 0.74, "original_text": "30 unitats del menú", "validated": False, "doubt_reason": "Referencia deducida por contexto"},
                ],
                create_alert=True,
                create_correction=True,
            ),
            OrderSeed(
                external_id="mulet-demo-wa-05",
                subject="Pedido corto y algo ambiguo",
                customer_code="430000020",
                sender="Dragon de Oro compras",
                detected_name="DRAGON DE ORO - CHINO AMPOSTA",
                score=63,
                status="dudoso",
                created_days_ago=2,
                customer_score=0.67,
                review_reasons="Una parte del pedido es clara, pero otra se basa en sustitución probable.",
                notes="Sirve para enseñar revisión manual y propuesta alternativa.",
                transcript=[
                    {"sender": "DRAGON DE ORO - CHINO AMPOSTA", "direction": "inbound", "text": "Ponme 8 del 222.0 CUIXA DE CABRIT y 12 del otro de siempre.", "timestamp_label": "11:11"},
                    {"sender": "Mulet Hidalgo", "direction": "outbound", "text": "¿El otro de siempre era 100.0 o 103.11?", "timestamp_label": "11:12"},
                    {"sender": "DRAGON DE ORO - CHINO AMPOSTA", "direction": "inbound", "text": "Creo que el 103.11, el tallat al buit.", "timestamp_label": "11:15"},
                ],
                lines=[
                    {"product_id": product_ids["222.0"], "reference": "222.0", "name": "CUIXA DE CABRIT", "quantity": 8, "unit": "u", "confidence": 0.83, "original_text": "8 del 222.0 CUIXA DE CABRIT"},
                    {"product_id": product_ids["103.11"], "reference": "103.11", "name": "ENTRECOT TALLAT AL BUIT", "quantity": 12, "unit": "u", "confidence": 0.59, "original_text": "12 del otro de siempre", "validated": False, "doubt_reason": "Ambigüedad resuelta por contexto"},
                ],
                create_alert=True,
            ),
            OrderSeed(
                external_id="mulet-demo-wa-06",
                subject="Pedido claro de horno",
                customer_code="430000058",
                sender="Forn Alfredo",
                detected_name="FORN DE PA ALFREDO CHERTA",
                score=91,
                status="pedido_confirmado",
                created_days_ago=3,
                customer_score=0.95,
                review_reasons="Muy buen encaje entre cliente, contexto y referencias históricas.",
                notes="Pedido rápido con muy poco ruido.",
                transcript=[
                    {"sender": "FORN DE PA ALFREDO CHERTA", "direction": "inbound", "text": "Hola, per a dijous: 30 de 100.11 BISTEC TALLAT AL BUIT i 20 de 102.1 MITJANA AMB OS I FILET +22 KG.", "timestamp_label": "07:52"},
                    {"sender": "Mulet Hidalgo", "direction": "outbound", "text": "Perfecto, queda en revisión rápida.", "timestamp_label": "07:54"},
                ],
                lines=[
                    {"product_id": product_ids["100.11"], "reference": "100.11", "name": "BISTEC TALLAT AL BUIT", "quantity": 30, "unit": "u", "confidence": 0.98, "original_text": "30 u. 100.11 BISTEC TALLAT AL BUIT"},
                    {"product_id": product_ids["102.1"], "reference": "102.1", "name": "MITJANA AMB OS I FILET +22 KG", "quantity": 20, "unit": "u", "confidence": 0.94, "original_text": "20 u. 102.1 MITJANA AMB OS I FILET +22 KG"},
                ],
                create_rag_case=True,
            ),
            OrderSeed(
                external_id="mulet-demo-wa-07",
                subject="Pedido con producto no directo",
                customer_code="430000050",
                sender="Restaurante Costa",
                detected_name="LA BRASERIA",
                score=54,
                status="dudoso",
                created_days_ago=4,
                customer_score=0.58,
                review_reasons="El cliente es reconocible, pero las referencias llegan mezcladas con una petición demasiado genérica.",
                notes="Útil para mostrar el umbral medio de confianza.",
                transcript=[
                    {"sender": "LA BRASERIA", "direction": "inbound", "text": "Bon dia, del mateix de la setmana passada, posa'm 16 i 8. I algun article demo per completar.", "timestamp_label": "13:19"},
                    {"sender": "Mulet Hidalgo", "direction": "outbound", "text": "¿Me concretas referencia o te preparo sugerencias?", "timestamp_label": "13:21"},
                    {"sender": "LA BRASERIA", "direction": "inbound", "text": "Sí, el tallat al buit i el tomahawk si hi ha estoc.", "timestamp_label": "13:24"},
                ],
                lines=[
                    {"product_id": product_ids["103.11"], "reference": "103.11", "name": "ENTRECOT TALLAT AL BUIT", "quantity": 16, "unit": "u", "confidence": 0.76, "original_text": "16 del tallat al buit"},
                    {"product_id": product_ids["102.8"], "reference": "102.8", "name": "TOMAHAWK", "quantity": 8, "unit": "u", "confidence": 0.69, "original_text": "8 del tomahawk si hi ha estoc", "validated": False, "doubt_reason": "Condicional de stock"},
                ],
                create_alert=True,
            ),
            OrderSeed(
                external_id="mulet-demo-wa-08",
                subject="Pedido para exportar",
                customer_code="430000039",
                sender="Termes Montbrió compras",
                detected_name="TERMES - MONTBRIÓ",
                score=86,
                status="pedido_exportado",
                created_days_ago=5,
                customer_score=0.9,
                review_reasons="Cliente conocido, referencias directas y cantidades coherentes con histórico.",
                notes="Buen ejemplo de pedido que puede ir a exportación.",
                transcript=[
                    {"sender": "TERMES - MONTBRIÓ", "direction": "inbound", "text": "Ens fa falta 24 de 104.11 FILET DE VEDELLA TALLAT I AL BUIT i 4 serveis de picking.", "timestamp_label": "08:03"},
                    {"sender": "Mulet Hidalgo", "direction": "outbound", "text": "Hecho. Lo preparo para exportar.", "timestamp_label": "08:04"},
                ],
                lines=[
                    {"product_id": product_ids["104.11"], "reference": "104.11", "name": "FILET DE VEDELLA TALLAT I AL BUIT", "quantity": 24, "unit": "u", "confidence": 0.95, "original_text": "24 u. 104.11 FILET DE VEDELLA TALLAT I AL BUIT"},
                    {"product_id": product_ids["MH-010"], "reference": "MH-010", "name": "Prueba Uno", "quantity": 4, "unit": "u", "confidence": 0.89, "original_text": "4 u. MH-010 Prueba Uno"},
                ],
                export_filename="MULET_WA_08.csv",
                create_rag_case=True,
            ),
            OrderSeed(
                external_id="mulet-demo-wa-09",
                subject="Pedido muy dudoso",
                customer_code="430000036",
                sender="Tafalla Autoservei",
                detected_name="TAFALLA AUTOSERVEI",
                score=41,
                status="no_importable",
                created_days_ago=6,
                customer_score=0.39,
                review_reasons="La conversación no concreta suficientes referencias y la cantidad queda ambigua.",
                notes="Ideal para enseñar bloqueo y escalado a revisión.",
                transcript=[
                    {"sender": "TAFALLA AUTOSERVEI", "direction": "inbound", "text": "Envia lo de siempre para mañana, unas 15 cajas y algo más de carne.", "timestamp_label": "12:08"},
                    {"sender": "Mulet Hidalgo", "direction": "outbound", "text": "Necesito referencias o lo dejamos en revisión.", "timestamp_label": "12:09"},
                    {"sender": "TAFALLA AUTOSERVEI", "direction": "inbound", "text": "Te lo confirmo luego.", "timestamp_label": "12:12"},
                ],
                lines=[
                    {"product_id": None, "reference": None, "name": "Pedido sin referencia", "quantity": 15, "unit": "cajas", "confidence": 0.29, "original_text": "unas 15 cajas y algo más de carne", "validated": False, "doubt_reason": "Sin referencia suficiente"},
                ],
                create_alert=True,
                create_correction=True,
            ),
            OrderSeed(
                external_id="mulet-demo-wa-10",
                subject="Pedido mínimo y poco fiable",
                customer_code="430000017",
                sender="Can Marques",
                detected_name="CAN MARQUES",
                score=29,
                status="no_importable",
                created_days_ago=7,
                customer_score=0.21,
                review_reasons="Muy poca información útil y producto no identificado.",
                notes="Pedagogía pura: el agente detecta señal, pero no puede asegurar pedido.",
                transcript=[
                    {"sender": "CAN MARQUES", "direction": "inbound", "text": "Ponme lo habitual para el finde, ya sabes.", "timestamp_label": "17:40"},
                    {"sender": "Mulet Hidalgo", "direction": "outbound", "text": "Si me pasas referencias te lo dejo cerrado.", "timestamp_label": "17:42"},
                ],
                lines=[
                    {"product_id": None, "reference": None, "name": "Lo habitual", "quantity": None, "unit": "", "confidence": 0.18, "original_text": "lo habitual para el finde", "validated": False, "doubt_reason": "No hay referencias claras"},
                ],
                create_alert=True,
            ),
        ]

        created = 0
        updated = 0
        for spec in specs:
            order, is_created = _seed_order(db, company, admin, channel, spec)
            if is_created:
                created += 1
            else:
                updated += 1
            db.commit()

        return {"created": created, "updated": updated, "total": len(specs)}


def main() -> None:
    result = seed_mulet_demo_whatsapp_orders()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
