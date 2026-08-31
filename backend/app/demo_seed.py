from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.encryption import encrypt_secret
from app.core.permissions import DEFAULT_ROLE_PERMISSIONS
from app.core.security import hash_password
from app.db.models import (
    Alert,
    AuditLog,
    BrandingSettings,
    ChannelSetting,
    Company,
    Customer,
    CustomerAlias,
    CustomerContact,
    CustomerContactPoint,
    CustomerDomain,
    CustomerProductKnowledge,
    DecisionSettings,
    Email,
    EmailAttachment,
    EmailSettings,
    ExportFile,
    ExportJob,
    ExportSettings,
    FTPSettings,
    ImportJob,
    ImportMappingTemplate,
    InputChannel,
    InboundMessage,
    LearnedAlias,
    LLMSettings,
    ManualCorrection,
    MessageAttachment,
    Order,
    OrderLine,
    OrderReview,
    Product,
    ProductAlias,
    PromptTemplate,
    PromptVersion,
    RagCase,
    RagDocument,
    Role,
    ScoringResult,
    ScoringSettings,
    User,
    utcnow,
)
from app.logs.service import log_action
from app.settings.branding import DEFAULT_MICROCOPY, DEFAULT_THEME, reset_branding
from app.settings.service import get_or_create_settings


DEMO_COMPANY_NAME = "Anchi Demo"
DEMO_COMPANY_CODE = "anchi-demo"
DEMO_ADMIN_EMAIL = "admin@anchi.local"
DEMO_ADMIN_PASSWORD = "AnchiDemo2026!"


def _now(days: int = 0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _ensure_one(db: Session, model, **filters):
    stmt = select(model)
    for key, value in filters.items():
        stmt = stmt.where(getattr(model, key) == value)
    instance = db.scalar(stmt)
    if instance:
        return instance
    instance = model(**filters)
    db.add(instance)
    db.flush()
    return instance


def _ensure_company(db: Session) -> Company:
    company = db.scalar(select(Company).where(Company.name == DEMO_COMPANY_NAME))
    if not company:
        company = Company(
            name=DEMO_COMPANY_NAME,
            legal_name=DEMO_COMPANY_NAME,
            active=True,
            plan="demo",
            email=DEMO_ADMIN_EMAIL,
            currency="EUR",
            language="es",
            default_language="es",
            timezone="Europe/Madrid",
            date_format="%d/%m/%Y",
            decimal_separator=",",
        )
        db.add(company)
        db.flush()
    return company


def _ensure_role(db: Session, company_id: int, name: str) -> Role:
    role = db.scalar(select(Role).where(Role.company_id == company_id, Role.name == name))
    if role:
        return role
    role = Role(company_id=company_id, name=name, permissions=DEFAULT_ROLE_PERMISSIONS.get(name, ""))
    db.add(role)
    db.flush()
    return role


def _ensure_user(db: Session, company_id: int, admin_role: Role) -> User:
    user = db.scalar(select(User).where(User.email == DEMO_ADMIN_EMAIL))
    if not user:
        user = User(
            company_id=company_id,
            role_id=admin_role.id,
            email=DEMO_ADMIN_EMAIL,
            name="Administrador demo",
            password_hash=hash_password(DEMO_ADMIN_PASSWORD),
            is_active=True,
        )
        db.add(user)
        db.flush()
        return user
    user.company_id = company_id
    user.role_id = admin_role.id
    user.name = user.name or "Administrador demo"
    user.password_hash = user.password_hash or hash_password(DEMO_ADMIN_PASSWORD)
    user.is_active = True
    db.flush()
    return user


def _ensure_branding(db: Session, company_id: int, user_id: int) -> BrandingSettings:
    branding = db.scalar(select(BrandingSettings).where(BrandingSettings.company_id == company_id))
    if not branding:
        branding = BrandingSettings(company_id=company_id)
        db.add(branding)
        db.flush()
    reset_branding(branding, user_id)
    branding.company_name = DEMO_COMPANY_NAME
    branding.app_name = "Anchi"
    branding.primary_claim = "Gestion inteligente de pedidos"
    branding.secondary_claim = ""
    branding.short_description = "Plataforma demo para la revision, validacion y exportacion de pedidos."
    branding.logo_url = None
    branding.dark_logo_url = None
    branding.favicon_url = None
    branding.theme_json = branding.theme_json or "{}"
    branding.microcopy_json = branding.microcopy_json or "{}"
    return branding


def _ensure_settings(db: Session, model, company_id: int):
    instance = db.scalar(select(model).where(model.company_id == company_id))
    if instance:
        return instance
    instance = model(company_id=company_id)
    db.add(instance)
    db.flush()
    return instance


def _ensure_input_channels(db: Session, company_id: int) -> dict[str, InputChannel]:
    channels = {}
    definitions = [
        {"key": "email", "name": "Email", "is_active": True, "is_default": True, "supports_text": True, "supports_attachments": True, "supports_audio": False, "supports_documents": True, "supports_images": False},
        {"key": "whatsapp", "name": "WhatsApp", "is_active": False, "is_default": False, "supports_text": True, "supports_attachments": True, "supports_audio": True, "supports_documents": True, "supports_images": False},
        {"key": "voice", "name": "Teléfono / voz", "is_active": False, "is_default": False, "supports_text": False, "supports_attachments": False, "supports_audio": True, "supports_documents": False, "supports_images": False},
        {"key": "social", "name": "Redes sociales", "is_active": False, "is_default": False, "supports_text": True, "supports_attachments": True, "supports_audio": False, "supports_documents": False, "supports_images": True},
    ]
    for definition in definitions:
        channel = db.scalar(select(InputChannel).where(InputChannel.company_id == company_id, InputChannel.key == definition["key"]))
        if not channel:
            channel = InputChannel(company_id=company_id, channel_type="message", **definition)
            db.add(channel)
            db.flush()
        else:
            for field, value in definition.items():
                setattr(channel, field, value)
        channels[channel.key] = channel
    return channels


def _ensure_customer(db: Session, company_id: int, code: str, fiscal_name: str, **fields) -> Customer:
    customer = db.scalar(select(Customer).where(Customer.company_id == company_id, Customer.code == code))
    if not customer:
        customer = Customer(company_id=company_id, code=code, fiscal_name=fiscal_name, **fields)
        db.add(customer)
        db.flush()
        return customer
    customer.fiscal_name = fiscal_name
    for key, value in fields.items():
        if value is not None:
            setattr(customer, key, value)
    return customer


def _ensure_product(db: Session, company_id: int, reference: str, name: str, **fields) -> Product:
    product = db.scalar(select(Product).where(Product.company_id == company_id, Product.reference == reference))
    if not product:
        product = Product(company_id=company_id, reference=reference, name=name, **fields)
        db.add(product)
        db.flush()
        return product
    product.name = name
    for key, value in fields.items():
        if value is not None:
            setattr(product, key, value)
    return product


def _ensure_order_bundle(
    db: Session,
    *,
    company_id: int,
    channel: InputChannel,
    customer: Customer | None,
    detected_name: str,
    subject: str,
    sender: str,
    body: str,
    email_external_id: str,
    order_status: str,
    score: float,
    created_days_ago: int,
    lines: list[dict],
    detected_type: str = "pedido",
    has_pdf: bool = False,
    pdf_text: str | None = None,
    customer_method: str = "email",
    order_note: str | None = None,
    export_filename: str | None = None,
) -> Order:
    email = db.scalar(select(Email).where(Email.company_id == company_id, Email.external_id == email_external_id))
    if not email:
        email = Email(
            company_id=company_id,
            external_id=email_external_id,
            sender=sender,
            subject=subject,
            body=body,
            extracted_text=pdf_text or body,
            received_at=_now(created_days_ago),
            status="pedido_detectado" if order_status not in {"dudoso", "no_importable"} else "dudoso",
            agent_status="processed_order_detected",
            detected_type=detected_type,
            has_attachments=has_pdf,
            has_pdf=has_pdf,
            last_processed_at=_now(created_days_ago),
        )
        db.add(email)
        db.flush()
    else:
        email.sender = sender
        email.subject = subject
        email.body = body
        email.extracted_text = pdf_text or body
        email.status = "pedido_detectado" if order_status not in {"dudoso", "no_importable"} else "dudoso"
        email.agent_status = "processed_order_detected"
        email.detected_type = detected_type
        email.has_attachments = has_pdf
        email.has_pdf = has_pdf
    if has_pdf and not db.scalar(select(EmailAttachment).where(EmailAttachment.email_id == email.id)):
        storage_path = Path("backend/storage/attachments/mock-1-2026-06-29.pdf")
        db.add(
            EmailAttachment(
                company_id=company_id,
                email_id=email.id,
                filename=storage_path.name,
                content_type="application/pdf",
                is_pdf=True,
                extraction_status="completed",
                storage_path=str(storage_path),
                extracted_text=pdf_text or body,
            )
        )
    inbound = db.scalar(select(InboundMessage).where(InboundMessage.company_id == company_id, InboundMessage.source_external_id == email.external_id))
    if not inbound:
        inbound = InboundMessage(
            company_id=company_id,
            channel_id=channel.id,
            source_external_id=email.external_id,
            sender=sender,
            subject=subject,
            original_content=pdf_text or body,
            raw_payload_json='{"source":"demo"}',
            content_type="email",
            status="received",
            processing_step="classified",
            detected_type=detected_type,
            normalized_text=body,
            classification_json='{"type":"pedido","confidence":0.93}',
            extraction_json='{}',
            customer_id=customer.id if customer else None,
            score=score,
            has_attachments=has_pdf,
            has_pdf=has_pdf,
            last_processed_at=_now(created_days_ago),
        )
        db.add(inbound)
        db.flush()
    order = db.scalar(select(Order).where(Order.company_id == company_id, Order.email_id == email.id))
    if not order:
        order = Order(
            company_id=company_id,
            email_id=email.id,
            customer_id=customer.id if customer else None,
            validated_customer_id=customer.id if customer else None,
            customer_detected_name=detected_name,
            customer_identification_method=customer_method,
            customer_score=0.95 if customer else 0.45,
            order_date=_now(created_days_ago).date().isoformat(),
            requested_delivery_date=None,
            notes=order_note,
            score=score,
            status=order_status,
            review_reasons="Demo",
            created_at=_now(created_days_ago),
            confirmed_at=_now(created_days_ago) if order_status in {"pedido_confirmado", "pedido_exportado"} else None,
            exported_at=_now(created_days_ago) if order_status == "pedido_exportado" else None,
        )
        db.add(order)
        db.flush()
    else:
        order.customer_id = customer.id if customer else None
        order.validated_customer_id = customer.id if customer else None
        order.customer_detected_name = detected_name
        order.customer_identification_method = customer_method
        order.score = score
        order.status = order_status
        order.notes = order_note
    if not order.lines:
        for line_data in lines:
            db.add(
                OrderLine(
                    company_id=company_id,
                    order_id=order.id,
                    product_id=line_data.get("product_id"),
                    validated_product_id=line_data.get("product_id"),
                    original_text=line_data.get("original_text"),
                    detected_reference=line_data.get("reference"),
                    detected_product=line_data.get("name"),
                    quantity=line_data.get("quantity"),
                    unit=line_data.get("unit"),
                    extraction_confidence=line_data.get("confidence", 0.9),
                    line_score=line_data.get("line_score", 0.9),
                    validation_status="validated" if line_data.get("product_id") and line_data.get("quantity") else "pending",
                    doubt_reason=line_data.get("doubt_reason"),
                )
            )
    review = db.scalar(select(OrderReview).where(OrderReview.company_id == company_id, OrderReview.order_id == order.id))
    if not review:
        db.add(OrderReview(company_id=company_id, order_id=order.id, status="approved" if order_status in {"pedido_confirmado", "pedido_exportado"} else "pending", reviewed_at=_now(created_days_ago) if order_status in {"pedido_confirmado", "pedido_exportado"} else None, comments="Pedido demo"))
    scoring = db.scalar(select(ScoringResult).where(ScoringResult.company_id == company_id, ScoringResult.order_id == order.id))
    if not scoring:
        db.add(ScoringResult(company_id=company_id, order_id=order.id, total_score=score, customer_score=25, product_score=35, confidence_score=25, rule_score=10, details_json='{"demo":true}'))
    if order_status == "pedido_exportado" and export_filename and not db.scalar(select(ExportFile).where(ExportFile.company_id == company_id, ExportFile.order_id == order.id)):
        db.add(ExportFile(company_id=company_id, order_id=order.id, filename=export_filename, content="demo-export", status="generated"))
        db.add(ExportJob(company_id=company_id, order_id=order.id, file_path=export_filename, export_format="csv", destination_type="sftp", status="completed", status_message="Exportación demo completada", payload_json='{"demo":true}', exported_at=_now(created_days_ago)))
    if order_status in {"dudoso", "no_importable"} and not db.scalar(select(Alert).where(Alert.company_id == company_id, Alert.order_id == order.id)):
        db.add(Alert(company_id=company_id, order_id=order.id, alert_type="order_review_required", severity="medium" if order_status == "dudoso" else "high", status="open", title=f"Revisar pedido demo: {subject}", message=order_note or "Pedido demo pendiente de revisión", payload_json='{"demo":true}'))
    return order


def seed_demo_base(db: Session) -> dict[str, int]:
    company = _ensure_company(db)
    admin_role = _ensure_role(db, company.id, "Administrador")
    _ensure_role(db, company.id, "Superadmin")
    _ensure_role(db, company.id, "Supervisor")
    _ensure_role(db, company.id, "Operador")
    _ensure_role(db, company.id, "Solo lectura")
    admin = _ensure_user(db, company.id, admin_role)
    _ensure_branding(db, company.id, admin.id)

    email_settings = _ensure_settings(db, EmailSettings, company.id)
    email_settings.provider = "imap"
    email_settings.connection_method = "password"
    email_settings.imap_host = "imap.demo.local"
    email_settings.imap_port = 993
    email_settings.imap_use_ssl = True
    email_settings.imap_security = "ssl_tls"
    email_settings.imap_username = DEMO_ADMIN_EMAIL
    email_settings.imap_password_encrypted = encrypt_secret("demo-imap-password")
    email_settings.connected_email = DEMO_ADMIN_EMAIL
    email_settings.inbox_folder = "INBOX"
    email_settings.processed_folder = "Procesados"
    email_settings.error_folder = "Errores"
    email_settings.no_order_folder = "Sin pedido"
    email_settings.doubtful_folder = "Dudosos"
    email_settings.read_limit = 25
    email_settings.test_read_limit = 10
    email_settings.auto_sync_enabled = False
    email_settings.read_unread_only = True
    email_settings.read_from_date = "2026-01-01"
    email_settings.mark_as_read_after_import = False
    email_settings.move_after_processing = False
    email_settings.post_process_action = "mark_read"
    email_settings.polling_frequency_minutes = 15
    email_settings.smtp_provider = "smtp"
    email_settings.smtp_host = "smtp.demo.local"
    email_settings.smtp_port = 587
    email_settings.smtp_security = "starttls"
    email_settings.smtp_username = DEMO_ADMIN_EMAIL
    email_settings.smtp_password_encrypted = encrypt_secret("demo-smtp-password")
    email_settings.from_email = DEMO_ADMIN_EMAIL
    email_settings.from_name = DEMO_COMPANY_NAME
    email_settings.reply_to = DEMO_ADMIN_EMAIL
    email_settings.save_internal_copy = True
    email_settings.preserve_thread_headers = True
    email_settings.auto_process_on_fetch = False
    email_settings.process_without_attachments = True
    email_settings.avoid_duplicates_by_message_id = True
    email_settings.allow_reprocess = False
    email_settings.auto_create_order_if_detected = True
    email_settings.always_human_review = True
    email_settings.mark_doubtful_below_threshold = True
    email_settings.mark_no_order_if_detected = True
    email_settings.minimum_score_auto_order = 90
    email_settings.signature_text = "Equipo Anchi"

    llm = _ensure_settings(db, LLMSettings, company.id)
    llm.agent_enabled = True
    llm.agent_mode = "semiautomatico"
    llm.safety_level = "equilibrado"
    llm.provider = "disabled"
    llm.classification_model = "gpt-4.1-mini"
    llm.extraction_model = "gpt-4.1-mini"
    llm.validation_model = "gpt-4.1-mini"
    llm.use_same_model_for_all = True
    llm.can_read_email = True
    llm.can_extract_pdf = True
    llm.can_classify_email = True
    llm.can_extract_order = True
    llm.can_suggest_customer = True
    llm.can_suggest_products = True
    llm.can_calculate_score = True
    llm.allow_auto_confirm = False
    llm.allow_auto_export = False
    llm.debug_mode = True

    scoring = _ensure_settings(db, ScoringSettings, company.id)
    scoring.safe_threshold = 90
    scoring.review_threshold = 75
    scoring.doubtful_threshold = 50
    scoring.blocked_threshold = 49
    scoring.customer_weight = 25
    scoring.products_weight = 40
    scoring.quantities_weight = 20
    scoring.coherence_weight = 10
    scoring.llm_weight = 5
    scoring.block_without_customer = True
    scoring.block_without_reference = True
    scoring.block_without_quantity = True
    scoring.block_below_threshold = True

    decision = _ensure_settings(db, DecisionSettings, company.id)
    decision.enable_exact_match = True
    decision.enable_alias_match = True
    decision.enable_relation_match = True
    decision.enable_history_match = True
    decision.enable_rag_match = True
    decision.enable_llm_support = True
    decision.always_human_review = True
    decision.learning_mode = "supervisado"
    decision.block_conflicting_aliases = True
    decision.block_missing_quantity = True
    decision.block_missing_reference = True

    ftp = _ensure_settings(db, FTPSettings, company.id)
    ftp.connection_type = "sftp"
    ftp.destination_path = "/anchi-demo"
    ftp.passive_mode = True
    ftp.overwrite_files = False
    ftp.retries = 2
    ftp.timeout_seconds = 30

    export = _ensure_settings(db, ExportSettings, company.id)
    export.file_type = "csv"
    export.csv_separator = ";"
    export.encoding = "utf-8"
    export.include_header = True
    export.date_format = "%Y-%m-%d"
    export.decimal_separator = ","
    export.filename_template = "PEDIDO_{codigo_cliente}_{fecha}_{id_pedido}.csv"
    export.header_fields = "order_id,customer_code,order_date"
    export.line_fields = "reference,quantity,unit,description"

    default_prompts = {
        "classification": "Clasifica el correo como pedido, no_pedido, consulta, incidencia o dudoso. Responde JSON valido con tipo_correo, confianza y motivo.",
        "extraction": "Extrae un pedido en JSON valido con cliente, fechas, observaciones y lineas con producto, referencia, cantidad y unidad.",
        "validation": "Valida el pedido extraido contra datos de cliente y producto. Devuelve JSON con advertencias y bloqueos.",
        "non_order": "Resume por que el correo no contiene pedido y clasificalo como consulta, incidencia, no_pedido o dudoso.",
    }
    for purpose, content in default_prompts.items():
        template = db.scalar(select(PromptTemplate).where(PromptTemplate.company_id == company.id, PromptTemplate.purpose == purpose))
        if not template:
            template = PromptTemplate(company_id=company.id, name=purpose.replace("_", " ").title(), purpose=purpose)
            db.add(template)
            db.flush()
        version = db.scalar(select(PromptVersion).where(PromptVersion.template_id == template.id, PromptVersion.version == 1))
        if not version:
            version = PromptVersion(company_id=company.id, template_id=template.id, version=1, content=content, created_by_user_id=admin.id)
            db.add(version)
            db.flush()
        template.active_version_id = version.id

    channels = _ensure_input_channels(db, company.id)

    customers = {
        "C001": _ensure_customer(
            db,
            company.id,
            "C001",
            "Cliente Valid SL",
            commercial_name="Cliente Valid",
            primary_email="valid@example.com",
            delegation="Central",
            phone="900 111 111",
            city="Barcelona",
            province="Barcelona",
            assigned_salesperson="Comercial Demo",
            accounting_code="4300001",
            category="Demo",
            notes="Cliente con pedidos recurrentes por email.\nPedido minimo 150 €.\nProducto no habitual requiere revision.\nRevisar cantidades superiores a 100 unidades.",
        ),
        "C002": _ensure_customer(
            db,
            company.id,
            "C002",
            "Restaurante Demo Norte",
            commercial_name="Demo Norte",
            primary_email="compras@norte.example",
            delegation="Norte",
            city="Bilbao",
            province="Bizkaia",
            assigned_salesperson="Comercial Norte",
            accounting_code="4300002",
            category="Restauracion",
            notes="Cliente con pedidos mixtos y validacion manual frecuente.",
        ),
        "C003": _ensure_customer(
            db,
            company.id,
            "C003",
            "Distribuciones Prueba SL",
            commercial_name="Distribuciones Prueba",
            primary_email="pedidos@distribuciones-prueba.example",
            delegation="Distribucion",
            city="Madrid",
            province="Madrid",
            assigned_salesperson="Comercial Distribucion",
            accounting_code="4300003",
            category="Distribucion",
            notes="Cliente con alto volumen y muchas referencias repetidas.",
        ),
        "C004": _ensure_customer(
            db,
            company.id,
            "C004",
            "Cliente Sin Conocimiento SL",
            commercial_name="Sin Conocimiento",
            primary_email="sinconocimiento@example.com",
            delegation="Sur",
            city="Sevilla",
            province="Sevilla",
            assigned_salesperson="Sin asignar",
            accounting_code="4300004",
            category="Demo",
            notes="Ficha basica sin historial ni condiciones.",
        ),
        "C005": _ensure_customer(
            db,
            company.id,
            "C005",
            "Cliente Con Condiciones Especiales SL",
            commercial_name="Condiciones Especiales",
            primary_email="condiciones@example.com",
            delegation="Especial",
            city="Valencia",
            province="Valencia",
            assigned_salesperson="Comercial Especial",
            accounting_code="4300005",
            category="Especial",
            notes="Pedido minimo 250 €.\nServicio urgente sujeto a aprobacion.\nCliente con condiciones especiales.",
        ),
    }

    customer_contact_definitions = [
        CustomerDomain(company_id=company.id, customer_id=customers["C001"].id, domain="example.com"),
        CustomerAlias(company_id=company.id, customer_id=customers["C001"].id, alias="Cliente Valid"),
        CustomerAlias(company_id=company.id, customer_id=customers["C001"].id, alias="Cliente Valid SL"),
        CustomerContactPoint(company_id=company.id, customer_id=customers["C001"].id, type="email", value="valid@example.com", label="principal", contact_name="Compras", contact_role="Compras", is_primary=True, confidence=0.98, source="manual", first_seen_at=_now(5), last_seen_at=_now(1)),
        CustomerContactPoint(company_id=company.id, customer_id=customers["C001"].id, type="domain", value="example.com", label="dominio", is_primary=True, confidence=0.95, source="manual", first_seen_at=_now(5), last_seen_at=_now(1)),
        CustomerContact(company_id=company.id, customer_id=customers["C001"].id, contact_type="email", name="Compras", email="valid@example.com", is_primary=True, notes="Contacto demo"),
    ]
    for item in customer_contact_definitions:
        if isinstance(item, CustomerDomain):
            exists = db.scalar(select(CustomerDomain).where(CustomerDomain.company_id == item.company_id, CustomerDomain.customer_id == item.customer_id, CustomerDomain.domain == item.domain))
        elif isinstance(item, CustomerAlias):
            exists = db.scalar(select(CustomerAlias).where(CustomerAlias.company_id == item.company_id, CustomerAlias.customer_id == item.customer_id, CustomerAlias.alias == item.alias))
        elif isinstance(item, CustomerContactPoint):
            exists = db.scalar(select(CustomerContactPoint).where(CustomerContactPoint.company_id == item.company_id, CustomerContactPoint.type == item.type, CustomerContactPoint.value == item.value))
        else:
            exists = db.scalar(select(CustomerContact).where(CustomerContact.company_id == item.company_id, CustomerContact.customer_id == item.customer_id, CustomerContact.contact_type == item.contact_type, CustomerContact.email == item.email))
        if not exists:
            db.add(item)

    extra_customer_specs = [
        {"code": "C006", "fiscal_name": "Distribuciones Levante SL", "commercial_name": "Distribuciones Levante", "primary_email": "levante@example.com", "delegation": "Levante", "city": "Alicante", "province": "Alicante", "assigned_salesperson": "Comercial Levante", "accounting_code": "4300006", "category": "Distribucion", "notes": "Cliente demo con pedidos regulares de envases."},
        {"code": "C007", "fiscal_name": "Hosteleria Costa SL", "commercial_name": "Hosteleria Costa", "primary_email": "costa@example.com", "delegation": "Costa", "city": "Murcia", "province": "Murcia", "assigned_salesperson": "Comercial Costa", "accounting_code": "4300007", "category": "Hosteleria", "notes": "Pedidos mixtos con prioridad en entregas rapidas."},
        {"code": "C008", "fiscal_name": "Alimentacion Norte SL", "commercial_name": "Alimentacion Norte", "primary_email": "norte@example.com", "delegation": "Norte", "city": "Santander", "province": "Cantabria", "assigned_salesperson": "Comercial Norte", "accounting_code": "4300008", "category": "Alimentacion", "notes": "Cliente demo con compras estables de temporada."},
        {"code": "C009", "fiscal_name": "Catering Madrid SL", "commercial_name": "Catering Madrid", "primary_email": "catering.madrid@example.com", "delegation": "Centro", "city": "Madrid", "province": "Madrid", "assigned_salesperson": "Comercial Centro", "accounting_code": "4300009", "category": "Catering", "notes": "Pedidos para eventos y campañas de fin de semana."},
        {"code": "C010", "fiscal_name": "Suministros Delta SL", "commercial_name": "Suministros Delta", "primary_email": "delta@example.com", "delegation": "Centro", "city": "Zaragoza", "province": "Zaragoza", "assigned_salesperson": "Comercial Delta", "accounting_code": "4300010", "category": "Suministros", "notes": "Cliente demo con validacion de cantidades altas."},
        {"code": "C011", "fiscal_name": "Comercial Bahia SL", "commercial_name": "Comercial Bahia", "primary_email": "bahia@example.com", "delegation": "Sur", "city": "Cadiz", "province": "Cadiz", "assigned_salesperson": "Comercial Bahia", "accounting_code": "4300011", "category": "Distribucion", "notes": "Cliente con pedidos cortos y urgentes."},
        {"code": "C012", "fiscal_name": "Proveeduria Centro SL", "commercial_name": "Proveeduria Centro", "primary_email": "proveeduria.centro@example.com", "delegation": "Centro", "city": "Toledo", "province": "Toledo", "assigned_salesperson": "Comercial Centro", "accounting_code": "4300012", "category": "Distribucion", "notes": "Cliente de prueba para verificaciones de scoring."},
        {"code": "C013", "fiscal_name": "Restauracion Sur SL", "commercial_name": "Restauracion Sur", "primary_email": "sur@example.com", "delegation": "Sur", "city": "Malaga", "province": "Malaga", "assigned_salesperson": "Comercial Sur", "accounting_code": "4300013", "category": "Restauracion", "notes": "Pedidos con multiples referencias pequeñas."},
        {"code": "C014", "fiscal_name": "Ecopack Iberia SL", "commercial_name": "Ecopack Iberia", "primary_email": "ecopack@example.com", "delegation": "Nacional", "city": "Valladolid", "province": "Valladolid", "assigned_salesperson": "Comercial Iberia", "accounting_code": "4300014", "category": "Ecommerce", "notes": "Cliente demo con productos sostenibles."},
        {"code": "C015", "fiscal_name": "Logistica Verde SL", "commercial_name": "Logistica Verde", "primary_email": "verde@example.com", "delegation": "Norte", "city": "Oviedo", "province": "Asturias", "assigned_salesperson": "Comercial Verde", "accounting_code": "4300015", "category": "Logistica", "notes": "Cliente con pedidos recurrentes de servicio."},
        {"code": "C016", "fiscal_name": "Fabricados del Este SL", "commercial_name": "Fabricados del Este", "primary_email": "este@example.com", "delegation": "Este", "city": "Tarragona", "province": "Tarragona", "assigned_salesperson": "Comercial Este", "accounting_code": "4300016", "category": "Industria", "notes": "Cliente de prueba con referencias mezcladas."},
        {"code": "C017", "fiscal_name": "Grupo Cocina SL", "commercial_name": "Grupo Cocina", "primary_email": "cocina@example.com", "delegation": "Centro", "city": "Salamanca", "province": "Salamanca", "assigned_salesperson": "Comercial Cocina", "accounting_code": "4300017", "category": "Hosteleria", "notes": "Pedidos frecuentes de vajilla y envases."},
        {"code": "C018", "fiscal_name": "Eventos y Banquetes SL", "commercial_name": "Eventos y Banquetes", "primary_email": "eventos@example.com", "delegation": "Nacional", "city": "Sevilla", "province": "Sevilla", "assigned_salesperson": "Comercial Eventos", "accounting_code": "4300018", "category": "Eventos", "notes": "Pedidos por lote con servicio adicional."},
        {"code": "C019", "fiscal_name": "Market Express SL", "commercial_name": "Market Express", "primary_email": "express@example.com", "delegation": "Centro", "city": "Madrid", "province": "Madrid", "assigned_salesperson": "Comercial Express", "accounting_code": "4300019", "category": "Retail", "notes": "Cliente de prueba para pedidos rapidos y simples."},
        {"code": "C020", "fiscal_name": "Ultra Food Service SL", "commercial_name": "Ultra Food Service", "primary_email": "ultra@example.com", "delegation": "Nacional", "city": "Bilbao", "province": "Bizkaia", "assigned_salesperson": "Comercial Ultra", "accounting_code": "4300020", "category": "Food Service", "notes": "Cliente demo para pedidos complejos y validacion fina."},
    ]
    for spec in extra_customer_specs:
        customers[spec["code"]] = _ensure_customer(db, company.id, spec["code"], spec["fiscal_name"], **{k: v for k, v in spec.items() if k not in {"code", "fiscal_name"}})

    products = {
        "KRAFT-30-BOX": _ensure_product(db, company.id, "KRAFT-30-BOX", "Kraft 30 Box", family="Envases", subfamily="Cajas", sale_unit="unidades", brand="Anchi", usual_supplier="Proveedor Demo", sale_price=12.5, discount_percent=0, size_group="STD", colors="Kraft", entry_date="2026-06-29", article_type="Venta", description_cont="Caja kraft 30."),
        "KRAFT-45-BOX": _ensure_product(db, company.id, "KRAFT-45-BOX", "Kraft 45 Box", family="Envases", subfamily="Cajas", sale_unit="unidades", brand="Anchi", usual_supplier="Proveedor Demo", sale_price=15.0, discount_percent=0, size_group="STD", colors="Kraft", entry_date="2026-06-29", article_type="Venta", description_cont="Caja kraft 45."),
        "BOLSA-ECO-10": _ensure_product(db, company.id, "BOLSA-ECO-10", "Bolsa Eco 10", family="Bolsas", subfamily="Eco", sale_unit="paquetes", brand="Anchi", usual_supplier="Proveedor Demo", sale_price=8.9, discount_percent=0, size_group="STD", colors="Verde", entry_date="2026-06-29", article_type="Venta", description_cont="Bolsa eco."),
        "ENVASE-ALU-500": _ensure_product(db, company.id, "ENVASE-ALU-500", "Envase Alu 500", family="Envases", subfamily="Aluminio", sale_unit="unidades", brand="Anchi", usual_supplier="Proveedor Demo", sale_price=18.2, discount_percent=0, size_group="STD", colors="Plateado", entry_date="2026-06-29", article_type="Venta", description_cont="Envase aluminio."),
        "VASO-BIO-250": _ensure_product(db, company.id, "VASO-BIO-250", "Vaso Bio 250", family="Vasos", subfamily="Bio", sale_unit="unidades", brand="Anchi", usual_supplier="Proveedor Demo", sale_price=9.4, discount_percent=0, size_group="STD", colors="Natural", entry_date="2026-06-29", article_type="Venta", description_cont="Vaso biodegradable."),
        "TAPA-TRANSP-500": _ensure_product(db, company.id, "TAPA-TRANSP-500", "Tapa Transparente 500", family="Accesorios", subfamily="Tapas", sale_unit="unidades", brand="Anchi", usual_supplier="Proveedor Demo", sale_price=6.6, discount_percent=0, size_group="STD", colors="Transparente", entry_date="2026-06-29", article_type="Venta", description_cont="Tapa transparente."),
        "CAJA-PIZZA-33": _ensure_product(db, company.id, "CAJA-PIZZA-33", "Caja Pizza 33", family="Envases", subfamily="Pizza", sale_unit="unidades", brand="Anchi", usual_supplier="Proveedor Demo", sale_price=11.7, discount_percent=0, size_group="STD", colors="Kraft", entry_date="2026-06-29", article_type="Venta", description_cont="Caja pizza."),
        "SERV-TRANSPORTE": _ensure_product(db, company.id, "SERV-TRANSPORTE", "Servicio Transporte", family="Servicios", subfamily="Logistica", sale_unit="servicio", brand="Anchi", usual_supplier="Anchi", sale_price=25.0, discount_percent=0, size_group="STD", colors="N/A", entry_date="2026-06-29", article_type="Servicio", description_cont="Servicio de transporte."),
        "SERV-URGENTE": _ensure_product(db, company.id, "SERV-URGENTE", "Servicio Urgente", family="Servicios", subfamily="Urgencia", sale_unit="servicio", brand="Anchi", usual_supplier="Anchi", sale_price=40.0, discount_percent=0, size_group="STD", colors="N/A", entry_date="2026-06-29", article_type="Servicio", description_cont="Servicio urgente."),
        "PROD-DESCONOCIDO-DEMO": _ensure_product(db, company.id, "PROD-DESCONOCIDO-DEMO", "Producto Desconocido Demo", family="Demo", subfamily="Sin clasificar", sale_unit="unidades", brand="Anchi", usual_supplier="Proveedor Demo", sale_price=4.2, discount_percent=0, size_group="STD", colors="Mixto", entry_date="2026-06-29", article_type="Venta", description_cont="Producto de prueba sin equivalente."),
    }

    extra_product_specs = [
        {"reference": "KRAFT-60-BOX", "name": "Kraft 60 Box", "family": "Envases", "subfamily": "Cajas", "sale_unit": "unidades", "brand": "Anchi", "usual_supplier": "Proveedor Demo", "sale_price": 19.8, "colors": "Kraft", "description_cont": "Caja kraft 60."},
        {"reference": "BOLSA-ECO-20", "name": "Bolsa Eco 20", "family": "Bolsas", "subfamily": "Eco", "sale_unit": "paquetes", "brand": "Anchi", "usual_supplier": "Proveedor Demo", "sale_price": 11.2, "colors": "Verde", "description_cont": "Bolsa eco 20."},
        {"reference": "BOLSA-KRAFT-05", "name": "Bolsa Kraft 05", "family": "Bolsas", "subfamily": "Kraft", "sale_unit": "paquetes", "brand": "Anchi", "usual_supplier": "Proveedor Demo", "sale_price": 7.6, "colors": "Marron", "description_cont": "Bolsa kraft 05."},
        {"reference": "ENVASE-ALU-750", "name": "Envase Alu 750", "family": "Envases", "subfamily": "Aluminio", "sale_unit": "unidades", "brand": "Anchi", "usual_supplier": "Proveedor Demo", "sale_price": 20.4, "colors": "Plateado", "description_cont": "Envase aluminio 750."},
        {"reference": "VASO-BIO-350", "name": "Vaso Bio 350", "family": "Vasos", "subfamily": "Bio", "sale_unit": "unidades", "brand": "Anchi", "usual_supplier": "Proveedor Demo", "sale_price": 10.5, "colors": "Natural", "description_cont": "Vaso biodegradable 350."},
        {"reference": "TAPA-TRANSP-750", "name": "Tapa Transparente 750", "family": "Accesorios", "subfamily": "Tapas", "sale_unit": "unidades", "brand": "Anchi", "usual_supplier": "Proveedor Demo", "sale_price": 8.1, "colors": "Transparente", "description_cont": "Tapa transparente 750."},
        {"reference": "CAJA-PIZZA-40", "name": "Caja Pizza 40", "family": "Envases", "subfamily": "Pizza", "sale_unit": "unidades", "brand": "Anchi", "usual_supplier": "Proveedor Demo", "sale_price": 13.4, "colors": "Kraft", "description_cont": "Caja pizza 40."},
        {"reference": "SERV-PICKING", "name": "Servicio Picking", "family": "Servicios", "subfamily": "Logistica", "sale_unit": "servicio", "brand": "Anchi", "usual_supplier": "Anchi", "sale_price": 18.0, "colors": "N/A", "description_cont": "Servicio de picking."},
        {"reference": "SERV-ETIQUETADO", "name": "Servicio Etiquetado", "family": "Servicios", "subfamily": "Preparacion", "sale_unit": "servicio", "brand": "Anchi", "usual_supplier": "Anchi", "sale_price": 22.0, "colors": "N/A", "description_cont": "Servicio de etiquetado."},
        {"reference": "KIT-MUESTRA-DEMO", "name": "Kit Muestra Demo", "family": "Demo", "subfamily": "Muestras", "sale_unit": "kit", "brand": "Anchi", "usual_supplier": "Proveedor Demo", "sale_price": 5.0, "colors": "Mixto", "description_cont": "Kit de muestra demo."},
    ]
    for spec in extra_product_specs:
        products[spec["reference"]] = _ensure_product(db, company.id, spec["reference"], spec["name"], **{k: v for k, v in spec.items() if k not in {"reference", "name"}})

    for product_alias in [
        ProductAlias(company_id=company.id, product_id=products["KRAFT-30-BOX"].id, alias="Caja Kraft 30"),
        ProductAlias(company_id=company.id, product_id=products["KRAFT-30-BOX"].id, alias="Kraft 30"),
        ProductAlias(company_id=company.id, product_id=products["BOLSA-ECO-10"].id, alias="Bolsa eco"),
        ProductAlias(company_id=company.id, product_id=products["CAJA-PIZZA-33"].id, alias="Caja pizza 33"),
        ProductAlias(company_id=company.id, product_id=products["SERV-URGENTE"].id, alias="urgente"),
        ProductAlias(company_id=company.id, product_id=products["KRAFT-60-BOX"].id, alias="Caja Kraft 60"),
        ProductAlias(company_id=company.id, product_id=products["BOLSA-KRAFT-05"].id, alias="Bolsa kraft"),
        ProductAlias(company_id=company.id, product_id=products["SERV-PICKING"].id, alias="picking"),
    ]:
        exists = db.scalar(select(ProductAlias).where(ProductAlias.company_id == product_alias.company_id, ProductAlias.product_id == product_alias.product_id, ProductAlias.alias == product_alias.alias))
        if not exists:
            db.add(product_alias)

    for product in products.values():
        knowledge = db.scalar(select(CustomerProductKnowledge).where(CustomerProductKnowledge.company_id == company.id, CustomerProductKnowledge.customer_id == customers["C001"].id, CustomerProductKnowledge.product_id == product.id))
        if not knowledge and product.reference in {"KRAFT-30-BOX", "BOLSA-ECO-10"}:
            db.add(
                CustomerProductKnowledge(
                    company_id=company.id,
                    customer_id=customers["C001"].id,
                    product_id=product.id,
                    product_reference=product.reference,
                    product_name=product.name,
                    customer_alias_used="Cliente Valid",
                    source_context="demo",
                    times_ordered=7 if product.reference == "KRAFT-30-BOX" else 4,
                    confirmed_count=6,
                    manual_count=1,
                    last_quantity=120 if product.reference == "KRAFT-30-BOX" else 25,
                    total_quantity=700 if product.reference == "KRAFT-30-BOX" else 100,
                    average_quantity=100 if product.reference == "KRAFT-30-BOX" else 25,
                    min_quantity=60 if product.reference == "KRAFT-30-BOX" else 10,
                    max_quantity=140 if product.reference == "KRAFT-30-BOX" else 40,
                    usual_unit="unidades" if product.reference == "KRAFT-30-BOX" else "paquetes",
                    comments_summary="Prioridad habitual. Revisar cantidades superiores a 100 unidades." if product.reference == "KRAFT-30-BOX" else "Nombre frecuente: Bolsa eco.",
                    confidence=0.96,
                    is_habitual=True,
                    status="habitual",
                    last_order_at=_now(3),
                    last_exported_at=_now(2),
                )
            )

    learned_aliases = [
        LearnedAlias(company_id=company.id, alias_type="customer", alias="Cliente Valid", canonical_value="Cliente Valid SL", customer_id=customers["C001"].id, confidence=0.97, source="demo", approved=True, approved_by=admin.id),
        LearnedAlias(company_id=company.id, alias_type="product", alias="Caja Kraft 30", canonical_value="KRAFT-30-BOX", product_id=products["KRAFT-30-BOX"].id, confidence=0.98, source="demo", approved=True, approved_by=admin.id),
    ]
    for alias in learned_aliases:
        exists = db.scalar(select(LearnedAlias).where(LearnedAlias.company_id == alias.company_id, LearnedAlias.alias_type == alias.alias_type, LearnedAlias.alias == alias.alias))
        if not exists:
            db.add(alias)

    manual_correction = db.scalar(select(ManualCorrection).where(ManualCorrection.company_id == company.id, ManualCorrection.entity_type == "product", ManualCorrection.field_name == "reference", ManualCorrection.original_value == "Caja Kraft 30"))
    if not manual_correction:
        db.add(ManualCorrection(company_id=company.id, entity_type="product", field_name="reference", original_value="Caja Kraft 30", corrected_value="KRAFT-30-BOX", agent_value="Kraft 30 Box", corrected_entity_id=products["KRAFT-30-BOX"].id, reason="Alias demo", should_learn=True, created_by_user_id=admin.id))

    rag_document = db.scalar(select(RagDocument).where(RagDocument.company_id == company.id, RagDocument.title == "Cliente Valid SL - conocimiento demo"))
    if not rag_document:
        db.add(RagDocument(company_id=company.id, source_type="manual", source_entity="customer", source_entity_id=customers["C001"].id, title="Cliente Valid SL - conocimiento demo", content_text="Cliente recurrente. Priorizar artículos habituales antes de buscar en productos globales.", metadata_json='{"demo":true}', embedding_status="indexed"))
    rag_case = db.scalar(select(RagCase).where(RagCase.company_id == company.id, RagCase.summary == "Cliente Valid SL pide Caja Kraft 30 y Bolsa eco"))
    if not rag_case:
        db.add(RagCase(company_id=company.id, customer_id=customers["C001"].id, summary="Cliente Valid SL pide Caja Kraft 30 y Bolsa eco", resolved_action="confirmar_pedido", resolution_json='{"customer":"C001"}', similarity_score=0.94))
    import_job = db.scalar(select(ImportJob).where(ImportJob.company_id == company.id, ImportJob.filename == "demo_clientes.xlsx"))
    if not import_job:
        db.add(ImportJob(company_id=company.id, entity_type="customers", filename="demo_clientes.xlsx", status="completed", rows_total=5, rows_created=5, rows_updated=0, rows_ignored=0, errors=None, mapping_used='{"demo":true}', user_id=admin.id))

    _ensure_order_bundle(
        db,
        company_id=company.id,
        channel=channels["email"],
        customer=customers["C001"],
        detected_name="Cliente Valid",
        sender="valid@example.com",
        subject="Pedido recurrente de material",
        body="Adjuntamos pedido recurrente de material.",
        email_external_id="demo-order-1",
        order_status="pedido_pendiente_revision",
        score=92,
        created_days_ago=4,
        lines=[
            {"reference": "KRAFT-30-BOX", "name": "Caja Kraft 30", "product_id": products["KRAFT-30-BOX"].id, "quantity": 120, "unit": "unidades", "original_text": "120 cajas Kraft 30", "confidence": 0.96},
            {"reference": "BOLSA-ECO-10", "name": "Bolsa eco", "product_id": products["BOLSA-ECO-10"].id, "quantity": 25, "unit": "paquetes", "original_text": "25 bolsas eco", "confidence": 0.94},
        ],
        has_pdf=True,
        pdf_text="Cliente Valid SL solicita 120 unidades de Caja Kraft 30 y 25 paquetes de Bolsa eco.",
        order_note="Pedido mínimo superado, listo para revisión humana.",
        export_filename="PEDIDO_C001_2026-07-04_1.csv",
    )
    _ensure_order_bundle(
        db,
        company_id=company.id,
        channel=channels["email"],
        customer=customers["C002"],
        detected_name="Restaurante Demo Norte",
        sender="compras@norte.example",
        subject="Pedido en revisión",
        body="Pedido con una referencia no habitual.",
        email_external_id="demo-order-2",
        order_status="dudoso",
        score=68,
        created_days_ago=3,
        lines=[
            {"reference": "KRAFT-45-BOX", "name": "Caja Kraft 45", "product_id": products["KRAFT-45-BOX"].id, "quantity": 18, "unit": "unidades", "original_text": "18 cajas Kraft 45", "confidence": 0.81},
            {"reference": None, "name": "Producto nuevo", "product_id": None, "quantity": 3, "unit": "unidades", "original_text": "3 unidades de un producto nuevo", "confidence": 0.52, "doubt_reason": "Producto no encontrado"},
        ],
        order_note="Pedido con línea dudosa pendiente de validación.",
    )
    _ensure_order_bundle(
        db,
        company_id=company.id,
        channel=channels["email"],
        customer=customers["C003"],
        detected_name="Distribuciones Prueba SL",
        sender="pedidos@distribuciones-prueba.example",
        subject="Pedido confirmado",
        body="Pedido validado por el equipo demo.",
        email_external_id="demo-order-3",
        order_status="pedido_confirmado",
        score=88,
        created_days_ago=2,
        lines=[
            {"reference": "CAJA-PIZZA-33", "name": "Caja Pizza 33", "product_id": products["CAJA-PIZZA-33"].id, "quantity": 40, "unit": "unidades", "original_text": "40 cajas pizza 33", "confidence": 0.95},
            {"reference": "SERV-TRANSPORTE", "name": "Servicio Transporte", "product_id": products["SERV-TRANSPORTE"].id, "quantity": 1, "unit": "servicio", "original_text": "1 servicio de transporte", "confidence": 0.93},
        ],
        order_note="Pedido listo para envío a gestión.",
    )
    _ensure_order_bundle(
        db,
        company_id=company.id,
        channel=channels["email"],
        customer=customers["C005"],
        detected_name="Cliente Con Condiciones Especiales SL",
        sender="condiciones@example.com",
        subject="Pedido exportado",
        body="Pedido con servicio urgente aprobado.",
        email_external_id="demo-order-4",
        order_status="pedido_exportado",
        score=96,
        created_days_ago=1,
        lines=[
            {"reference": "ENVASE-ALU-500", "name": "Envase Alu 500", "product_id": products["ENVASE-ALU-500"].id, "quantity": 60, "unit": "unidades", "original_text": "60 envases alu 500", "confidence": 0.97},
            {"reference": "SERV-URGENTE", "name": "Servicio Urgente", "product_id": products["SERV-URGENTE"].id, "quantity": 1, "unit": "servicio", "original_text": "1 servicio urgente", "confidence": 0.98},
        ],
        order_note="Pedido exportado correctamente.",
        export_filename="PEDIDO_C005_2026-07-07_4.csv",
    )
    _ensure_order_bundle(
        db,
        company_id=company.id,
        channel=channels["email"],
        customer=customers["C004"],
        detected_name="Cliente Sin Conocimiento",
        sender="sinconocimiento@example.com",
        subject="Pedido dudoso",
        body="Pedido con producto no catalogado.",
        email_external_id="demo-order-5",
        order_status="no_importable",
        score=44,
        created_days_ago=0,
        lines=[
            {"reference": "PROD-DESCONOCIDO-DEMO", "name": "Producto desconocido demo", "product_id": products["PROD-DESCONOCIDO-DEMO"].id, "quantity": 7, "unit": "unidades", "original_text": "7 productos no catalogados", "confidence": 0.48, "doubt_reason": "Producto sin equivalencia"},
        ],
        order_note="Sin conocimiento histórico ni condiciones previas.",
    )

    _ensure_order_bundle(
        db,
        company_id=company.id,
        channel=channels["email"],
        customer=customers["C006"],
        detected_name="Distribuciones Levante SL",
        sender="levante@example.com",
        subject="Pedido estandar de envases",
        body="Pedido con referencias habituales y cantidad moderada.",
        email_external_id="demo-order-6",
        order_status="pedido_pendiente_revision",
        score=86,
        created_days_ago=5,
        lines=[
            {"reference": "KRAFT-60-BOX", "name": "Kraft 60 Box", "product_id": products["KRAFT-60-BOX"].id, "quantity": 80, "unit": "unidades", "original_text": "80 cajas kraft 60", "confidence": 0.95},
            {"reference": "BOLSA-ECO-20", "name": "Bolsa Eco 20", "product_id": products["BOLSA-ECO-20"].id, "quantity": 20, "unit": "paquetes", "original_text": "20 bolsas eco 20", "confidence": 0.93},
        ],
        order_note="Pedido demo pendiente de revisión rápida.",
    )
    _ensure_order_bundle(
        db,
        company_id=company.id,
        channel=channels["email"],
        customer=customers["C007"],
        detected_name="Hosteleria Costa SL",
        sender="costa@example.com",
        subject="Pedido con linea dudosa",
        body="Pedido con una linea no habitual para comprobacion.",
        email_external_id="demo-order-7",
        order_status="dudoso",
        score=63,
        created_days_ago=5,
        lines=[
            {"reference": "BOLSA-KRAFT-05", "name": "Bolsa Kraft 05", "product_id": products["BOLSA-KRAFT-05"].id, "quantity": 15, "unit": "paquetes", "original_text": "15 bolsas kraft", "confidence": 0.82},
            {"reference": None, "name": "Linea no catalogada", "product_id": None, "quantity": 2, "unit": "unidades", "original_text": "2 unidades de un articulo nuevo", "confidence": 0.5, "doubt_reason": "Articulo no localizado"},
        ],
        order_note="Requiere validacion manual de la segunda linea.",
    )
    _ensure_order_bundle(
        db,
        company_id=company.id,
        channel=channels["email"],
        customer=customers["C008"],
        detected_name="Alimentacion Norte SL",
        sender="norte@example.com",
        subject="Pedido confirmado de temporada",
        body="Pedido confirmado por el equipo demo.",
        email_external_id="demo-order-8",
        order_status="pedido_confirmado",
        score=89,
        created_days_ago=4,
        lines=[
            {"reference": "ENVASE-ALU-750", "name": "Envase Alu 750", "product_id": products["ENVASE-ALU-750"].id, "quantity": 55, "unit": "unidades", "original_text": "55 envases alu 750", "confidence": 0.96},
            {"reference": "VASO-BIO-350", "name": "Vaso Bio 350", "product_id": products["VASO-BIO-350"].id, "quantity": 40, "unit": "unidades", "original_text": "40 vasos bio 350", "confidence": 0.94},
        ],
        order_note="Pedido validado y listo para gestion interna.",
    )
    _ensure_order_bundle(
        db,
        company_id=company.id,
        channel=channels["email"],
        customer=customers["C009"],
        detected_name="Catering Madrid SL",
        sender="catering.madrid@example.com",
        subject="Pedido exportado para evento",
        body="Pedido de evento con servicio extra.",
        email_external_id="demo-order-9",
        order_status="pedido_exportado",
        score=97,
        created_days_ago=3,
        lines=[
            {"reference": "CAJA-PIZZA-40", "name": "Caja Pizza 40", "product_id": products["CAJA-PIZZA-40"].id, "quantity": 70, "unit": "unidades", "original_text": "70 cajas pizza 40", "confidence": 0.97},
            {"reference": "SERV-ETIQUETADO", "name": "Servicio Etiquetado", "product_id": products["SERV-ETIQUETADO"].id, "quantity": 1, "unit": "servicio", "original_text": "1 servicio de etiquetado", "confidence": 0.95},
        ],
        order_note="Pedido exportado correctamente para el flujo demo.",
        export_filename="PEDIDO_C009_2026-07-07_9.csv",
    )
    _ensure_order_bundle(
        db,
        company_id=company.id,
        channel=channels["email"],
        customer=customers["C010"],
        detected_name="Suministros Delta SL",
        sender="delta@example.com",
        subject="Pedido no importable",
        body="Pedido con datos insuficientes para importar automaticamente.",
        email_external_id="demo-order-10",
        order_status="no_importable",
        score=42,
        created_days_ago=2,
        lines=[
            {"reference": "KIT-MUESTRA-DEMO", "name": "Kit Muestra Demo", "product_id": products["KIT-MUESTRA-DEMO"].id, "quantity": 3, "unit": "kit", "original_text": "3 kits de muestra", "confidence": 0.55, "doubt_reason": "Pedido parcial"},
        ],
        order_note="Pedido bloqueado por falta de informacion suficiente.",
    )

    additional_orders = [
        {
            "code": "C011",
            "detected_name": "Comercial Bahia SL",
            "sender": "bahia@example.com",
            "subject": "Pedido seguro de reposicion",
            "body": "Pedido sencillo con referencias habituales.",
            "external_id": "demo-order-11",
            "status": "pedido_pendiente_revision",
            "score": 94,
            "days": 6,
            "note": "Pedido muy fiable, solo necesita revision rapida.",
            "lines": [
                {"reference": "KRAFT-30-BOX", "name": "Caja Kraft 30", "product_id": products["KRAFT-30-BOX"].id, "quantity": 150, "unit": "unidades", "original_text": "150 cajas kraft 30", "confidence": 0.97},
                {"reference": "BOLSA-ECO-20", "name": "Bolsa Eco 20", "product_id": products["BOLSA-ECO-20"].id, "quantity": 12, "unit": "paquetes", "original_text": "12 bolsas eco 20", "confidence": 0.95},
            ],
        },
        {
            "code": "C012",
            "detected_name": "Proveeduria Centro SL",
            "sender": "proveeduria.centro@example.com",
            "subject": "Pedido de prueba revisable",
            "body": "Pedido correcto con una linea que requiere validacion.",
            "external_id": "demo-order-12",
            "status": "pedido_pendiente_revision",
            "score": 82,
            "days": 6,
            "note": "Buena confianza general, revisar solo una parte.",
            "lines": [
                {"reference": "KRAFT-60-BOX", "name": "Kraft 60 Box", "product_id": products["KRAFT-60-BOX"].id, "quantity": 60, "unit": "unidades", "original_text": "60 cajas kraft 60", "confidence": 0.92},
                {"reference": "SERV-PICKING", "name": "Servicio Picking", "product_id": products["SERV-PICKING"].id, "quantity": 1, "unit": "servicio", "original_text": "1 servicio de picking", "confidence": 0.88},
            ],
        },
        {
            "code": "C013",
            "detected_name": "Restauracion Sur SL",
            "sender": "sur@example.com",
            "subject": "Pedido con una duda de producto",
            "body": "Pedido casi completo con una referencia poco clara.",
            "external_id": "demo-order-13",
            "status": "dudoso",
            "score": 71,
            "days": 5,
            "note": "Duda puntual en una linea de producto.",
            "lines": [
                {"reference": "CAJA-PIZZA-33", "name": "Caja Pizza 33", "product_id": products["CAJA-PIZZA-33"].id, "quantity": 45, "unit": "unidades", "original_text": "45 cajas pizza 33", "confidence": 0.9},
                {"reference": None, "name": "Producto no identificado", "product_id": None, "quantity": 4, "unit": "unidades", "original_text": "4 unidades de articulo sin referencia", "confidence": 0.51, "doubt_reason": "Referencia incompleta"},
            ],
        },
        {
            "code": "C014",
            "detected_name": "Ecopack Iberia SL",
            "sender": "ecopack@example.com",
            "subject": "Pedido con varias validaciones",
            "body": "Pedido correcto, pero con cantidades a revisar.",
            "external_id": "demo-order-14",
            "status": "dudoso",
            "score": 58,
            "days": 5,
            "note": "Necesita revision manual por cifras y equivalencias.",
            "lines": [
                {"reference": "VASO-BIO-350", "name": "Vaso Bio 350", "product_id": products["VASO-BIO-350"].id, "quantity": 90, "unit": "unidades", "original_text": "90 vasos bio 350", "confidence": 0.87},
                {"reference": "TAPA-TRANSP-750", "name": "Tapa Transparente 750", "product_id": products["TAPA-TRANSP-750"].id, "quantity": 90, "unit": "unidades", "original_text": "90 tapas transparentes", "confidence": 0.76},
            ],
        },
        {
            "code": "C015",
            "detected_name": "Logistica Verde SL",
            "sender": "verde@example.com",
            "subject": "Pedido bloqueado por falta de datos",
            "body": "Pedido incompleto, falta referencia y cantidad exacta.",
            "external_id": "demo-order-15",
            "status": "no_importable",
            "score": 47,
            "days": 4,
            "note": "Bloqueado hasta completar la informacion.",
            "lines": [
                {"reference": "SERV-URGENTE", "name": "Servicio Urgente", "product_id": products["SERV-URGENTE"].id, "quantity": 1, "unit": "servicio", "original_text": "urgente", "confidence": 0.61, "doubt_reason": "Falta detalle"},
            ],
        },
        {
            "code": "C016",
            "detected_name": "Fabricados del Este SL",
            "sender": "este@example.com",
            "subject": "Pedido confirmado de producto mixto",
            "body": "Pedido claro con referencias conocidas.",
            "external_id": "demo-order-16",
            "status": "pedido_confirmado",
            "score": 89,
            "days": 4,
            "note": "Pedido validado manualmente.",
            "lines": [
                {"reference": "ENVASE-ALU-750", "name": "Envase Alu 750", "product_id": products["ENVASE-ALU-750"].id, "quantity": 110, "unit": "unidades", "original_text": "110 envases alu 750", "confidence": 0.96},
                {"reference": "BOLSA-KRAFT-05", "name": "Bolsa Kraft 05", "product_id": products["BOLSA-KRAFT-05"].id, "quantity": 15, "unit": "paquetes", "original_text": "15 bolsas kraft 05", "confidence": 0.91},
            ],
        },
        {
            "code": "C017",
            "detected_name": "Grupo Cocina SL",
            "sender": "cocina@example.com",
            "subject": "Pedido con revisiones menores",
            "body": "Pedido correcto con alguna equivalencia a revisar.",
            "external_id": "demo-order-17",
            "status": "pedido_pendiente_revision",
            "score": 76,
            "days": 3,
            "note": "Pendiente de una confirmacion final.",
            "lines": [
                {"reference": "KRAFT-30-BOX", "name": "Caja Kraft 30", "product_id": products["KRAFT-30-BOX"].id, "quantity": 75, "unit": "unidades", "original_text": "75 cajas kraft 30", "confidence": 0.91},
                {"reference": "SERV-PICKING", "name": "Servicio Picking", "product_id": products["SERV-PICKING"].id, "quantity": 1, "unit": "servicio", "original_text": "1 servicio picking", "confidence": 0.84},
            ],
        },
        {
            "code": "C018",
            "detected_name": "Eventos y Banquetes SL",
            "sender": "eventos@example.com",
            "subject": "Pedido exportado evento fin de semana",
            "body": "Pedido listo para exportar con alta confianza.",
            "external_id": "demo-order-18",
            "status": "pedido_exportado",
            "score": 92,
            "days": 3,
            "note": "Exportado correctamente para el demo.",
            "lines": [
                {"reference": "CAJA-PIZZA-40", "name": "Caja Pizza 40", "product_id": products["CAJA-PIZZA-40"].id, "quantity": 120, "unit": "unidades", "original_text": "120 cajas pizza 40", "confidence": 0.97},
                {"reference": "SERV-ETIQUETADO", "name": "Servicio Etiquetado", "product_id": products["SERV-ETIQUETADO"].id, "quantity": 1, "unit": "servicio", "original_text": "1 servicio de etiquetado", "confidence": 0.96},
            ],
            "export": "PEDIDO_C018_2026-07-07_18.csv",
        },
        {
            "code": "C019",
            "detected_name": "Market Express SL",
            "sender": "express@example.com",
            "subject": "Pedido rapido de bajo riesgo",
            "body": "Pedido corto y bastante claro.",
            "external_id": "demo-order-19",
            "status": "pedido_confirmado",
            "score": 88,
            "days": 2,
            "note": "Pedido rapido con poca friccion.",
            "lines": [
                {"reference": "KRAFT-45-BOX", "name": "Kraft 45 Box", "product_id": products["KRAFT-45-BOX"].id, "quantity": 50, "unit": "unidades", "original_text": "50 cajas kraft 45", "confidence": 0.93},
                {"reference": "BOLSA-ECO-10", "name": "Bolsa Eco 10", "product_id": products["BOLSA-ECO-10"].id, "quantity": 20, "unit": "paquetes", "original_text": "20 bolsas eco 10", "confidence": 0.9},
            ],
        },
        {
            "code": "C020",
            "detected_name": "Ultra Food Service SL",
            "sender": "ultra@example.com",
            "subject": "Pedido con bloqueo por referencia incompleta",
            "body": "Pedido con parte del contenido ausente.",
            "external_id": "demo-order-20",
            "status": "no_importable",
            "score": 38,
            "days": 1,
            "note": "Falta informacion suficiente para importar.",
            "lines": [
                {"reference": None, "name": "Linea incompleta", "product_id": None, "quantity": 5, "unit": "unidades", "original_text": "5 unidades sin referencia clara", "confidence": 0.44, "doubt_reason": "No hay referencia valida"},
            ],
        },
    ]

    for spec in additional_orders:
        _ensure_order_bundle(
            db,
            company_id=company.id,
            channel=channels["email"],
            customer=customers[spec["code"]],
            detected_name=spec["detected_name"],
            sender=spec["sender"],
            subject=spec["subject"],
            body=spec["body"],
            email_external_id=spec["external_id"],
            order_status=spec["status"],
            score=spec["score"],
            created_days_ago=spec["days"],
            lines=spec["lines"],
            order_note=spec["note"],
            export_filename=spec.get("export"),
        )

    special_doc = db.scalar(select(RagDocument).where(RagDocument.company_id == company.id, RagDocument.title == "Cliente Con Condiciones Especiales SL - condiciones"))
    if not special_doc:
        db.add(RagDocument(company_id=company.id, source_type="manual", source_entity="customer", source_entity_id=customers["C005"].id, title="Cliente Con Condiciones Especiales SL - condiciones", content_text="Pedido mínimo 250 €. Servicio urgente sujeto a aprobación.", metadata_json='{"demo":true,"conditions":true}', embedding_status="indexed"))
    special_contact = db.scalar(select(CustomerContactPoint).where(CustomerContactPoint.company_id == company.id, CustomerContactPoint.type == "email", CustomerContactPoint.value == "condiciones@example.com"))
    if not special_contact:
        db.add(CustomerContactPoint(company_id=company.id, customer_id=customers["C005"].id, type="email", value="condiciones@example.com", label="principal", contact_name="Compras", is_primary=True, confidence=0.97, source="manual", first_seen_at=_now(10), last_seen_at=_now(1)))

    db.commit()
    log_action(db, company_id=company.id, user=admin, action="demo.seed", entity_type="organization", entity_id=company.id, message="Demo de Anchi restaurada")
    return {
        "company_id": company.id,
        "customers": db.scalar(select(func.count()).select_from(Customer).where(Customer.company_id == company.id)) or 0,
        "products": db.scalar(select(func.count()).select_from(Product).where(Product.company_id == company.id)) or 0,
        "orders": db.scalar(select(func.count()).select_from(Order).where(Order.company_id == company.id)) or 0,
        "imports": db.scalar(select(func.count()).select_from(ImportJob).where(ImportJob.company_id == company.id)) or 0,
    }


def reset_demo_company(db: Session) -> dict[str, int]:
    company = db.scalar(select(Company).where(Company.name == DEMO_COMPANY_NAME))
    if not company:
        return seed_demo_base(db)
    company_id = company.id
    tables = [
        MessageAttachment,
        EmailAttachment,
        OrderLine,
        OrderReview,
        ScoringResult,
        ExportJob,
        ExportFile,
        Alert,
        RagCase,
        RagDocument,
        LearnedAlias,
        ManualCorrection,
        CustomerProductKnowledge,
        ProductAlias,
        CustomerContactPoint,
        CustomerContact,
        CustomerAlias,
        CustomerDomain,
        Customer,
        Product,
        Order,
        Email,
        InboundMessage,
        ImportMappingTemplate,
        ImportJob,
        ChannelSetting,
        InputChannel,
        PromptVersion,
        PromptTemplate,
        FTPSettings,
        EmailSettings,
        ExportSettings,
        ExportFile,
        ScoringSettings,
        DecisionSettings,
        LLMSettings,
        AuditLog,
    ]
    for table in tables:
        db.execute(delete(table).where(table.company_id == company_id))
    for role_name in ["Administrador", "Superadmin", "Supervisor", "Operador", "Solo lectura"]:
        db.execute(delete(Role).where(Role.company_id == company_id, Role.name == role_name))
    db.commit()
    return seed_demo_base(db)
