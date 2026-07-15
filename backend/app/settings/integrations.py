import imaplib
import json
import re
import socket
import smtplib
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email import policy
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import EmailMessage
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.encryption import decrypt_secret
from app.db.models import Email, EmailAttachment, EmailSettings, InboundMessage, InputChannel, LLMSettings, MessageAttachment
from app.jobs.service import enqueue_job
from app.logs.service import log_action


ATTACHMENTS_DIR = Path(__file__).resolve().parents[1] / "storage" / "attachments"
IMAP_RECENT_MESSAGES_LIMIT = 3
IMAP_DEFAULT_INITIAL_LIMIT = 20
IMAP_MAX_MESSAGES_PER_RUN = 50
IMAP_MAX_ATTACHMENTS_PER_EMAIL = 10
IMAP_MAX_ATTACHMENT_SIZE_MB = 10
IMAP_TIMEOUT_SECONDS = 20
SYNC_LOCKS: dict[int, threading.Lock] = {}


def classify_integration_error(error: Exception | str) -> str:
    message = str(error).lower()
    if any(marker in message for marker in ("auth", "credential", "password", "invalid login", "permission denied")):
        return "authentication_failed" if "permission" not in message else "permission_denied"
    if "timeout" in message or "timed out" in message:
        return "timeout"
    if any(marker in message for marker in ("rate limit", "too many requests")):
        return "rate_limited"
    if any(marker in message for marker in ("faltan", "missing", "incomplet", "required", "invalid configuration")):
        return "invalid_configuration"
    if any(marker in message for marker in ("denied", "forbidden")):
        return "permission_denied"
    if any(marker in message for marker in ("network", "connection", "refused", "unreachable", "socket", "smtp", "imap")):
        return "connection_failed"
    return "unexpected_error"


def validate_imap_config(settings: EmailSettings) -> dict:
    password = decrypt_secret(settings.imap_password_encrypted)
    if not settings.imap_host or not settings.imap_username or not password:
        return {"ok": False, "error_type": "invalid_configuration", "message": "Faltan host, usuario o password IMAP."}
    return {"ok": True, "error_type": None, "message": "Configuracion IMAP valida."}


def validate_smtp_config(settings: EmailSettings) -> dict:
    password = decrypt_secret(settings.smtp_password_encrypted)
    if not settings.smtp_host or not settings.smtp_username or not password or not (settings.from_email or settings.smtp_username):
        return {"ok": False, "error_type": "invalid_configuration", "message": "Faltan host, usuario o password SMTP."}
    return {"ok": True, "error_type": None, "message": "Configuracion SMTP valida."}


def validate_openai_config(settings: LLMSettings) -> dict:
    if settings.provider not in {"openai", "openai_compatible", "azure_openai"}:
        return {"ok": False, "error_type": "invalid_configuration", "message": f"Proveedor IA no soportado: {settings.provider}."}
    if not decrypt_secret(settings.api_key_encrypted):
        return {"ok": False, "error_type": "invalid_configuration", "message": "Falta API key de OpenAI."}
    return {"ok": True, "error_type": None, "message": "Configuracion IA valida."}


def redact_email_config(settings: EmailSettings) -> dict:
    return {
        "imap_host": settings.imap_host,
        "imap_port": settings.imap_port,
        "imap_username": settings.imap_username,
        "imap_password": "••••••••" if settings.imap_password_encrypted else "",
        "smtp_host": settings.smtp_host,
        "smtp_port": settings.smtp_port,
        "smtp_username": settings.smtp_username,
        "smtp_password": "••••••••" if settings.smtp_password_encrypted else "",
        "from_email": settings.from_email,
        "provider": settings.provider,
        "smtp_provider": settings.smtp_provider,
    }


def redact_llm_config(settings: LLMSettings) -> dict:
    return {
        "provider": settings.provider,
        "base_url": settings.base_url,
        "classification_model": settings.classification_model,
        "extraction_model": settings.extraction_model,
        "validation_model": settings.validation_model,
        "api_key": "••••••••" if settings.api_key_encrypted else "",
    }


def _parse_imap_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _imap_search_criteria(*, start_date: date | None = None, end_date: date | None = None, unread_only: bool = True) -> list[str]:
    criteria: list[str] = []
    if start_date:
        criteria.extend(["SINCE", start_date.strftime("%d-%b-%Y")])
    if end_date:
        criteria.extend(["BEFORE", (end_date + timedelta(days=1)).strftime("%d-%b-%Y")])
    if unread_only:
        criteria.append("UNSEEN")
    return criteria or ["ALL"]


def test_imap_connection(settings: EmailSettings) -> dict:
    validation = validate_imap_config(settings)
    if not validation["ok"]:
        return {"ok": False, "error_type": validation["error_type"], "found": 0, "new": 0, "duplicates": 0, "last_email": "", "message": validation["message"]}
    password = decrypt_secret(settings.imap_password_encrypted)
    try:
        client = _imap_client(settings)
        client.login(settings.imap_username, password)
        status, data = client.select(settings.inbox_folder or "INBOX", readonly=True)
        search_status, search_data = client.search(None, "UNSEEN" if settings.read_unread_only else "ALL")
        ids = (search_data[0] or b"").split() if search_status == "OK" else []
        last_email = ""
        if ids:
            fetch_status, msg_data = client.fetch(ids[-1], "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
            if fetch_status == "OK" and msg_data and msg_data[0]:
                last_email = msg_data[0][1].decode(errors="ignore").replace("\r", " ").replace("\n", " ")[:300]
        client.logout()
        if status != "OK":
            return {"ok": False, "found": 0, "new": 0, "duplicates": 0, "last_email": "", "message": "No se pudo abrir la carpeta IMAP."}
        found = int((data[0] or b"0").decode(errors="ignore") or 0)
        return {"ok": True, "found": found, "new": len(ids), "duplicates": 0, "last_email": last_email, "message": f"Conexion correcta. Correos en carpeta: {found}. Coinciden con el filtro: {len(ids)}."}
    except (imaplib.IMAP4.error, socket.timeout, OSError) as exc:
        return {"ok": False, "error_type": classify_integration_error(exc), "found": 0, "new": 0, "duplicates": 0, "last_email": "", "message": f"Error IMAP: {exc}"}


def _imap_client(settings: EmailSettings):
    timeout = IMAP_TIMEOUT_SECONDS
    if settings.imap_security == "ssl_tls" or settings.imap_use_ssl:
        return imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, timeout=timeout)
    client = imaplib.IMAP4(settings.imap_host, settings.imap_port, timeout=timeout)
    if settings.imap_security == "starttls":
        client.starttls()
    return client


def _fetch_imap_emails(
    db: Session,
    settings: EmailSettings,
    company_id: int,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    unread_only: bool = True,
    limit: int | None = None,
    auto_process: bool = False,
    label: str = "Lectura IMAP",
) -> dict:
    password = decrypt_secret(settings.imap_password_encrypted)
    if not settings.imap_host or not settings.imap_username or not password:
        return {"ok": False, "found": 0, "saved": 0, "message": "Faltan host, usuario o password IMAP."}
    sync_lock = SYNC_LOCKS.setdefault(company_id, threading.Lock())
    if not sync_lock.acquire(blocking=False):
        return {"ok": False, "found": 0, "saved": 0, "message": "Ya hay una sincronizacion IMAP en curso."}
    found = saved = attachments_saved = 0
    duplicates = 0
    try:
        settings.last_sync_message = f"{label} en curso"
        settings.last_sync_error = None
        db.commit()
    except Exception:
        db.rollback()
    log_action(db, company_id=company_id, user=None, action="email.fetch_started", entity_type="email", message=f"{label} iniciada")
    try:
        client = _imap_client(settings)
        client.login(settings.imap_username, password)
        client.select(settings.inbox_folder or "INBOX", readonly=not settings.mark_as_read_after_import)
        status, data = client.search(None, *_imap_search_criteria(start_date=start_date, end_date=end_date, unread_only=unread_only))
        if status != "OK":
            client.logout()
            return {"ok": False, "found": 0, "saved": 0, "message": "No se pudieron listar correos."}
        ids = (data[0] or b"").split()
        if start_date or end_date:
            ids = sorted(ids, key=lambda raw: int(raw.decode(errors="ignore") or 0))
        if limit:
            limit = max(int(limit), 1)
            limit = min(limit, IMAP_MAX_MESSAGES_PER_RUN)
            ids = ids[:limit] if (start_date or end_date) else ids[-limit:]
        found = len(ids)
        saved_email_ids: list[int] = []
        for msg_id in ids:
            status, msg_data = client.fetch(msg_id, "(UID RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            fetch_meta = msg_data[0][0].decode(errors="ignore") if isinstance(msg_data[0], tuple) else ""
            uid_match = re.search(r"UID\s+(\d+)", fetch_meta)
            uid = uid_match.group(1) if uid_match else msg_id.decode(errors="ignore")
            msg = message_from_bytes(raw, policy=policy.default)
            message_id = msg.get("Message-ID") or f"imap-{settings.connected_email or settings.imap_username}-{uid}"
            exists = db.query(Email).filter(Email.company_id == company_id, Email.external_id == message_id).one_or_none()
            if exists:
                duplicates += 1
                log_action(db, company_id=company_id, user=None, action="email.duplicate_ignored", entity_type="email", entity_id=exists.id, message=f"Duplicado ignorado: {message_id}")
                continue
            subject = _decode_mime_header(msg.get("Subject", ""))
            sender = _decode_mime_header(msg.get("From", ""))
            body = _extract_body(msg)
            email = Email(company_id=company_id, external_id=message_id, sender=sender, subject=subject, body=body, extracted_text=body, status="pending", agent_status="not_processed", detected_type=None)
            db.add(email)
            db.flush()
            inbound_message = _create_inbound_message(db, company_id, email, settings, msg, body)
            attachment_count = _save_attachments(
                db,
                company_id,
                email,
                msg,
                inbound_message=inbound_message,
                max_attachments=IMAP_MAX_ATTACHMENTS_PER_EMAIL,
                max_attachment_size_mb=IMAP_MAX_ATTACHMENT_SIZE_MB,
            )
            attachments_saved += attachment_count
            email.has_attachments = attachment_count > 0
            email.has_pdf = any(att.is_pdf for att in email.attachments)
            pdf_texts = [att.extracted_text for att in email.attachments if att.is_pdf and att.extracted_text]
            if pdf_texts:
                email.extracted_text = "\n\n".join(pdf_texts)
                inbound_message.normalized_text = email.extracted_text
            inbound_message.has_attachments = email.has_attachments
            inbound_message.has_pdf = email.has_pdf
            inbound_message.original_content = body
            saved += 1
            saved_email_ids.append(email.id)
            log_action(db, company_id=company_id, user=None, action="email.saved", entity_type="email", entity_id=email.id, message=f"Correo guardado: {subject[:120]}")
            if settings.mark_as_read_after_import:
                client.store(msg_id, "+FLAGS", "\\Seen")
        db.commit()
        if (auto_process or settings.auto_process_on_fetch) and saved_email_ids:
            for email_id in saved_email_ids:
                enqueue_job(
                    db,
                    company_id=company_id,
                    job_type="process_email",
                    payload={"email_id": email_id},
                    created_by_user_id=None,
                )
            db.commit()
        client.logout()
        _update_sync_status(settings, True, saved, duplicates, f"{found} correos encontrados, {saved} nuevos guardados, {duplicates} duplicados ignorados, {attachments_saved} adjuntos guardados.")
        db.commit()
        log_action(db, company_id=company_id, user=None, action="email.fetch_completed", entity_type="email", message=settings.last_sync_message or "")
        return {"ok": True, "found": found, "saved": saved, "duplicates": duplicates, "attachments": attachments_saved, "message": settings.last_sync_message}
    except (imaplib.IMAP4.error, socket.timeout, OSError) as exc:
        message = f"Error IMAP: {exc}"
        _update_sync_status(settings, False, saved, duplicates, message, message)
        db.commit()
        log_action(db, company_id=company_id, user=None, action="email.fetch_error", entity_type="email", message=message)
        return {"ok": False, "found": found, "saved": saved, "duplicates": duplicates, "attachments": attachments_saved, "message": message}
    finally:
        sync_lock.release()


def read_latest_imap_emails(
    db: Session,
    settings: EmailSettings,
    company_id: int,
    *,
    auto_process: bool = False,
    unread_only: bool | None = None,
    limit: int | None = None,
) -> dict:
    effective_limit = limit if limit is not None else IMAP_RECENT_MESSAGES_LIMIT
    return _fetch_imap_emails(
        db,
        settings,
        company_id,
        unread_only=False if unread_only is None else unread_only,
        limit=min(max(int(effective_limit or IMAP_RECENT_MESSAGES_LIMIT), 1), IMAP_MAX_MESSAGES_PER_RUN),
        auto_process=auto_process,
        label="Lectura IMAP",
    )


def backfill_imap_emails(
    db: Session,
    settings: EmailSettings,
    company_id: int,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int | None = None,
) -> dict:
    start_date = _parse_imap_date(from_date or settings.read_from_date)
    if not start_date:
        start_date = datetime.now(timezone.utc).date() - timedelta(days=30)
    end_date = _parse_imap_date(to_date)
    if end_date and start_date and end_date < start_date:
        return {"ok": False, "found": 0, "saved": 0, "message": "La fecha final no puede ser anterior a la inicial."}
    if not start_date:
        return {"ok": False, "found": 0, "saved": 0, "message": "Indica una fecha valida para el backfill (AAAA-MM-DD)."}
    return _fetch_imap_emails(
        db,
        settings,
        company_id,
        start_date=start_date,
        end_date=end_date,
        unread_only=False,
        limit=limit,
        auto_process=False,
        label=f"Backfill IMAP desde {start_date.strftime('%d/%m/%Y')}{' hasta ' + end_date.strftime('%d/%m/%Y') if end_date else ''}",
    )


def _update_sync_status(settings: EmailSettings, ok: bool, saved: int, duplicates: int, message: str, error: str | None = None) -> None:
    settings.last_sync_at = datetime.now(timezone.utc)
    settings.last_sync_ok = ok
    settings.last_sync_message = message
    settings.last_sync_error = error
    settings.last_sync_new = saved
    settings.last_sync_duplicates = duplicates


def _decode_mime_header(value: str) -> str:
    try:
        return str(make_header(decode_header(value or ""))).strip()
    except Exception:
        return value or ""


def _extract_body(msg: EmailMessage) -> str:
    if msg.is_multipart():
        plain_parts: list[str] = []
        html_parts: list[str] = []
        for part in msg.walk():
            if part.get_content_disposition() == "attachment":
                continue
            content_type = part.get_content_type()
            if content_type not in {"text/plain", "text/html"}:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            text = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
            if content_type == "text/plain":
                plain_parts.append(text)
            else:
                html_parts.append(_strip_html(text))
        return "\n\n".join(plain_parts or html_parts).strip()
    payload = msg.get_payload(decode=True)
    return payload.decode(msg.get_content_charset() or "utf-8", errors="ignore").strip() if payload else ""


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _safe_filename(filename: str) -> str:
    name = _decode_mime_header(filename or "adjunto")
    name = re.sub(r"[^\w.\- áéíóúàèìòùäëïöüñÁÉÍÓÚÀÈÌÒÙÄËÏÖÜÑ]+", "_", name, flags=re.UNICODE).strip("._ ")
    return name[:160] or "adjunto"


def _is_attachment(part: EmailMessage) -> bool:
    disposition = (part.get_content_disposition() or "").lower()
    filename = part.get_filename()
    content_type = part.get_content_type()
    return bool(filename) or disposition in {"attachment", "inline"} and content_type not in {"text/plain", "text/html"}


def _create_input_channel(db: Session, company_id: int) -> InputChannel:
    channel = db.query(InputChannel).filter(InputChannel.company_id == company_id, InputChannel.key == "email").one_or_none()
    if channel:
        return channel
    channel = InputChannel(
        company_id=company_id,
        key="email",
        name="Email",
        channel_type="message",
        is_active=True,
        is_default=True,
        supports_text=True,
        supports_attachments=True,
        supports_audio=False,
        supports_documents=True,
    )
    db.add(channel)
    db.flush()
    return channel


def _create_inbound_message(db: Session, company_id: int, email: Email, settings: EmailSettings, msg: EmailMessage, body: str) -> InboundMessage:
    channel = _create_input_channel(db, company_id)
    inbound_message = InboundMessage(
        company_id=company_id,
        channel_id=channel.id,
        source_external_id=email.external_id,
        source_thread_id=msg.get("In-Reply-To") or msg.get("References"),
        sender=email.sender,
        recipient=settings.connected_email or settings.imap_username,
        subject=email.subject,
        original_content=body,
        raw_payload_json=json.dumps(
            {
                "message_id": email.external_id,
                "from": email.sender,
                "subject": email.subject,
                "date": msg.get("Date"),
                "imap_mailbox": settings.mailbox,
            },
            ensure_ascii=False,
        ),
        content_type="email",
        status="received",
        processing_step="received",
        has_attachments=False,
        has_pdf=False,
        has_audio=False,
    )
    db.add(inbound_message)
    db.flush()
    return inbound_message


def _save_attachments(
    db: Session,
    company_id: int,
    email: Email,
    msg: EmailMessage,
    inbound_message: InboundMessage | None = None,
    *,
    max_attachments: int = IMAP_MAX_ATTACHMENTS_PER_EMAIL,
    max_attachment_size_mb: int = IMAP_MAX_ATTACHMENT_SIZE_MB,
) -> int:
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    count = 0
    for part in msg.walk():
        if not _is_attachment(part):
            continue
        if count >= max_attachments:
            break
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        if len(payload) > max_attachment_size_mb * 1024 * 1024:
            log_action(
                db,
                company_id=company_id,
                user=None,
                action="email.attachment_skipped",
                entity_type="email_attachment",
                message=f"Adjunto omitido por tamano: {part.get_filename() or 'adjunto'}",
            )
            continue
        filename = _safe_filename(part.get_filename() or f"adjunto-{count + 1}")
        content_type = part.get_content_type() or "application/octet-stream"
        is_pdf = content_type == "application/pdf" or filename.lower().endswith(".pdf")
        internal_name = f"email-{email.id}-{uuid4().hex[:10]}-{filename}"
        storage_path = ATTACHMENTS_DIR / internal_name
        storage_path.write_bytes(payload)
        attachment = EmailAttachment(
            company_id=company_id,
            email_id=email.id,
            filename=filename,
            content_type=content_type,
            size_bytes=len(payload),
            is_pdf=is_pdf,
            extraction_status="pending" if is_pdf else "not_applicable",
            storage_path=str(storage_path),
        )
        db.add(attachment)
        db.flush()
        if inbound_message:
            message_attachment = MessageAttachment(
                company_id=company_id,
                inbound_message_id=inbound_message.id,
                filename=filename,
                content_type=content_type,
                size_bytes=len(payload),
                storage_path=str(storage_path),
                extracted_text=attachment.extracted_text,
                is_pdf=is_pdf,
                is_image=content_type.startswith("image/"),
                is_audio=content_type.startswith("audio/"),
                extraction_status=attachment.extraction_status,
                extraction_error=attachment.extraction_error,
            )
            db.add(message_attachment)
            db.flush()
        log_action(db, company_id=company_id, user=None, action="email.attachment_saved", entity_type="email_attachment", entity_id=attachment.id, message=f"Adjunto guardado: {filename}")
        if is_pdf:
            _extract_pdf_text(db, attachment)
            if inbound_message:
                message_attachment.extracted_text = attachment.extracted_text
                message_attachment.extraction_status = attachment.extraction_status
                message_attachment.extraction_error = attachment.extraction_error
        count += 1
    return count


def _extract_pdf_text(db: Session, attachment: EmailAttachment) -> None:
    path = Path(attachment.storage_path or "")
    try:
        data = path.read_bytes()
        text = _extract_text_from_pdf_bytes(data)
        if text.strip():
            attachment.extracted_text = text.strip()
            attachment.extraction_status = "extracted"
            log_action(db, company_id=attachment.company_id, user=None, action="email.pdf_text_extracted", entity_type="email_attachment", entity_id=attachment.id, message=f"Texto PDF extraido: {attachment.filename}")
        else:
            attachment.extraction_status = "no_text_found"
            attachment.extraction_error = "El PDF no contiene texto legible. Puede requerir OCR."
            log_action(db, company_id=attachment.company_id, user=None, action="email.pdf_text_error", entity_type="email_attachment", entity_id=attachment.id, message=attachment.extraction_error)
    except Exception as exc:
        attachment.extraction_status = "extraction_error"
        attachment.extraction_error = f"No se pudo extraer texto del PDF: {exc}"
        log_action(db, company_id=attachment.company_id, user=None, action="email.pdf_text_error", entity_type="email_attachment", entity_id=attachment.id, message=attachment.extraction_error)


def _extract_text_from_pdf_bytes(data: bytes) -> str:
    # Fallback ligero para PDFs con texto embebido sin depender de librerias externas.
    chunks: list[str] = []
    raw = data.decode("latin-1", errors="ignore")
    for match in re.finditer(r"\((.*?)\)\s*Tj", raw, re.S):
        chunks.append(_decode_pdf_text(match.group(1)))
    for match in re.finditer(r"\[(.*?)\]\s*TJ", raw, re.S):
        chunks.extend(_decode_pdf_text(item) for item in re.findall(r"\((.*?)\)", match.group(1), re.S))
    text = "\n".join(item for item in chunks if item.strip())
    return re.sub(r"[ \t]+", " ", text)


def _decode_pdf_text(value: str) -> str:
    value = value.replace(r"\(", "(").replace(r"\)", ")").replace(r"\\", "\\")
    value = re.sub(r"\\([nrtbf])", " ", value)
    value = re.sub(r"\\[0-7]{1,3}", " ", value)
    return value.strip()


def _smtp_client(settings: EmailSettings):
    timeout = 30
    if settings.smtp_security == "ssl_tls":
        return smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=timeout)
    client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout)
    if settings.smtp_security == "starttls":
        client.starttls()
    return client


def test_smtp_connection(settings: EmailSettings) -> dict:
    validation = validate_smtp_config(settings)
    if not validation["ok"]:
        return {"ok": False, "error_type": validation["error_type"], "message": validation["message"]}
    password = decrypt_secret(settings.smtp_password_encrypted)
    try:
        client = _smtp_client(settings)
        client.login(settings.smtp_username, password)
        client.quit()
        return {"ok": True, "message": "Conexion SMTP correcta."}
    except smtplib.SMTPAuthenticationError:
        return {"ok": False, "error_type": "authentication_failed", "message": "Error de autenticacion SMTP."}
    except smtplib.SMTPServerDisconnected:
        return {"ok": False, "error_type": "connection_failed", "message": "Error de servidor SMTP: conexion cerrada."}
    except (socket.timeout, TimeoutError):
        return {"ok": False, "error_type": "timeout", "message": "Timeout conectando con SMTP."}
    except (smtplib.SMTPException, OSError) as exc:
        return {"ok": False, "error_type": classify_integration_error(exc), "message": f"Error SMTP: {exc}"}


def send_test_email(settings: EmailSettings, to_email: str, subject: str, message: str) -> dict:
    password = decrypt_secret(settings.smtp_password_encrypted)
    from_email = settings.from_email or settings.smtp_username
    if not settings.smtp_host or not settings.smtp_username or not password or not from_email:
        return {"ok": False, "error_type": "invalid_configuration", "message": "Faltan datos SMTP o remitente."}
    try:
        email = EmailMessage()
        email["From"] = f"{settings.from_name} <{from_email}>" if settings.from_name else from_email
        email["To"] = to_email
        email["Subject"] = subject
        if settings.reply_to:
            email["Reply-To"] = settings.reply_to
        if settings.default_cc:
            email["Cc"] = settings.default_cc
        if settings.default_bcc:
            email["Bcc"] = settings.default_bcc
        email.set_content(message)
        recipients = [to_email]
        recipients += [item.strip() for item in (settings.default_cc or "").split(",") if item.strip()]
        recipients += [item.strip() for item in (settings.default_bcc or "").split(",") if item.strip()]
        client = _smtp_client(settings)
        client.login(settings.smtp_username, password)
        client.send_message(email, from_addr=from_email, to_addrs=recipients)
        client.quit()
        return {"ok": True, "message": "Correo de prueba enviado correctamente."}
    except smtplib.SMTPAuthenticationError:
        return {"ok": False, "error_type": "authentication_failed", "message": "Error de autenticacion SMTP."}
    except smtplib.SMTPRecipientsRefused:
        return {"ok": False, "error_type": "permission_denied", "message": "El servidor rechazo el destinatario."}
    except (socket.timeout, TimeoutError):
        return {"ok": False, "error_type": "timeout", "message": "Timeout enviando el correo de prueba."}
    except (smtplib.SMTPException, OSError) as exc:
        return {"ok": False, "error_type": classify_integration_error(exc), "message": f"Error SMTP: {exc}"}


def call_openai(settings: LLMSettings, messages: list[dict], model: str) -> dict:
    validation = validate_openai_config(settings)
    if not validation["ok"]:
        return {"ok": False, "error_type": validation["error_type"], "message": validation["message"]}
    api_key = decrypt_secret(settings.api_key_encrypted)
    base_url = (settings.base_url or "https://api.openai.com/v1").rstrip("/")
    payload = {
        "model": model,
        "messages": messages,
        "temperature": settings.temperature,
        "max_tokens": settings.max_tokens,
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    last_error = ""
    for _ in range(max(int(settings.retries or 0), 0) + 1):
        try:
            with urllib.request.urlopen(request, timeout=settings.timeout_seconds) as response:
                data = json.loads(response.read().decode())
            return {"ok": True, "message": "Conexion OpenAI correcta.", "content": data["choices"][0]["message"]["content"]}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="ignore")
            if exc.code in {401, 403}:
                return {"ok": False, "error_type": "authentication_failed", "message": "API key invalida o sin permisos para el proveedor IA."}
            if exc.code == 404:
                return {"ok": False, "error_type": "invalid_configuration", "message": f"Modelo o endpoint no encontrado: {model}."}
            last_error = f"Error OpenAI HTTP {exc.code}: {detail[:300]}"
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            last_error = f"Timeout o error de conexion con OpenAI: {exc}"
    return {"ok": False, "error_type": classify_integration_error(last_error or "Error desconocido conectando con OpenAI."), "message": last_error or "Error desconocido conectando con OpenAI."}


def classify_sample(settings: LLMSettings, prompt: str, text: str) -> dict:
    return call_openai(settings, [{"role": "system", "content": prompt}, {"role": "user", "content": text}], settings.classification_model)


def extract_sample(settings: LLMSettings, prompt: str, text: str) -> dict:
    result = call_openai(settings, [{"role": "system", "content": prompt}, {"role": "user", "content": text}], settings.extraction_model)
    if result.get("ok"):
        try:
            json.loads(result.get("content", ""))
        except json.JSONDecodeError:
            result["ok"] = False
            result["message"] = "La extraccion respondio, pero no era JSON valido."
    return result
