from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from urllib.error import URLError

import json

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.agent.platform import LearningService
from app.agent.services import AgentProcessingService, ScoringService
from app.core.config import get_settings
from app.db.models import BackgroundJob, Email, EmailSettings, ExportFile, FTPSettings, Order, ScoringSettings
from app.exports.service import ExportService, FTPService
from app.logs.service import log_action
from app.master.database import MasterSessionLocal
from app.master.models import MasterTenantDatabase
from app.imports.service import confirm_import, guess_mapping, read_preview
from app.orders.routes import _customer_label, _sync_customer_product_knowledge, validate_confirmation
from app.settings.integrations import backfill_imap_emails, read_latest_imap_emails
from app.settings.service import get_or_create_settings
from app.jobs.service import claim_next_job, fail_job, finish_job, job_payload, update_job_progress
from app.tenancy.database import tenant_db_session

logger = logging.getLogger(__name__)
_worker_started = False


def _now():
    return datetime.now(timezone.utc)


def _is_retryable_exception(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, OSError, URLError)):
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "timeout",
            "tempor",
            "connection reset",
            "connection refused",
            "service unavailable",
            "imap",
            "smtp",
            "network",
            "socket",
            "temporarily",
        )
    )


def _process_job(db, job: BackgroundJob) -> dict:
    payload = job_payload(job)
    if job.job_type in {"import_confirm", "import_file"}:
        return _process_import_job(db, job, payload)
    if job.job_type in {"export_order", "export_order_ftp"}:
        return _process_export_job(db, job, payload)
    if job.job_type == "bulk_order_action":
        return _process_bulk_action(db, job, payload)
    if job.job_type == "email_sync":
        settings = get_or_create_settings(db, EmailSettings, job.company_id)
        return read_latest_imap_emails(
            db,
            settings,
            job.company_id,
            auto_process=bool(payload.get("auto_process", False)),
            unread_only=payload.get("unread_only"),
            limit=payload.get("limit"),
        )
    if job.job_type == "process_recent_emails":
        settings = get_or_create_settings(db, EmailSettings, job.company_id)
        limit = max(min(int(payload.get("limit", 3) or 3), 10), 1)
        return read_latest_imap_emails(db, settings, job.company_id, auto_process=True, unread_only=False, limit=limit)
    if job.job_type == "backfill_imap":
        settings = get_or_create_settings(db, EmailSettings, job.company_id)
        return backfill_imap_emails(
            db,
            settings,
            job.company_id,
            payload.get("from_date"),
            payload.get("to_date"),
            payload.get("limit"),
        )
    if job.job_type == "process_pending_emails":
        processor = AgentProcessingService()
        emails = db.scalars(
            select(Email).where(
                Email.company_id == job.company_id,
                Email.agent_status.in_(["not_processed", "pending_reprocess"]),
            )
        ).all()
        processed = 0
        total = len(emails) or 1
        for index, email in enumerate(emails, start=1):
            processor.process_email(db, email)
            processed += 1
            update_job_progress(db, job, int((index / total) * 100))
        return {"ok": True, "processed": processed, "message": f"{processed} correos procesados"}
    if job.job_type == "process_email":
        email_id = int(payload.get("email_id") or 0)
        email = db.get(Email, email_id)
        if not email or email.company_id != job.company_id:
            raise RuntimeError("No se encontró el correo a procesar.")
        result = AgentProcessingService().process_email(db, email)
        return {"ok": True, **result}
    if job.job_type == "process_order":
        order_id = int(payload.get("order_id") or 0)
        order = db.get(Order, order_id)
        if not order or order.company_id != job.company_id:
            raise RuntimeError("No se encontró el pedido a procesar.")
        if order.email:
            result = AgentProcessingService().process_email(db, order.email)
            return {"ok": True, **result}
        return {"ok": False, "message": "El pedido no tiene correo asociado."}
    raise RuntimeError(f"Tipo de job no soportado: {job.job_type}")


def _process_import_job(db, job: BackgroundJob, payload: dict) -> dict:
    token = str(payload.get("token") or "")
    filename = str(payload.get("filename") or "import")
    entity_type = str(payload.get("entity_type") or "customers")
    encoding = str(payload.get("encoding") or "utf-8")
    mapping = payload.get("mapping") or {}
    mode = str(payload.get("mode") or "create_update")
    save_template = bool(payload.get("save_template"))
    template_name = str(payload.get("template_name") or "")
    df = read_preview(token, filename, encoding=encoding)
    if not mapping:
        mapping = guess_mapping(entity_type, [str(column) for column in df.columns])
    dummy_user = type("TenantLikeUser", (), {"id": job.created_by_user_id or 0, "company_id": job.company_id})()
    import_job = confirm_import(
        db,
        company_id=job.company_id,
        user=dummy_user,
        entity_type=entity_type,
        filename=filename,
        df=df,
        mapping=mapping,
        mode=mode,
        save_template=save_template,
        template_name=template_name,
    )
    return {
        "ok": True,
        "message": f"Importacion {entity_type} completada",
        "import_job_id": import_job.id,
        "rows_total": import_job.rows_total,
        "rows_created": import_job.rows_created,
        "rows_updated": import_job.rows_updated,
        "rows_ignored": import_job.rows_ignored,
    }


def _serialize_export(export: ExportFile) -> dict:
    return {
        "id": export.id,
        "filename": export.filename,
        "status": export.status,
        "created_at": export.created_at.isoformat() if export.created_at else None,
    }


def _process_export_job(db, job: BackgroundJob, payload: dict) -> dict:
    order_id = int(payload.get("order_id") or 0)
    order = db.scalar(
        select(Order)
        .where(Order.id == order_id, Order.company_id == job.company_id)
        .options(selectinload(Order.lines), selectinload(Order.validated_customer), selectinload(Order.customer), selectinload(Order.email).selectinload(Email.attachments))
    )
    if not order:
        raise RuntimeError("No se encontró el pedido a exportar.")
    export = db.scalar(select(ExportFile).where(ExportFile.order_id == order.id, ExportFile.company_id == job.company_id).order_by(ExportFile.created_at.desc()))
    if not export:
        export = ExportService().generate_csv(db, order)
    ftp_settings = get_or_create_settings(db, FTPSettings, job.company_id)
    send_via_ftp = job.job_type == "export_order_ftp"
    ok = True
    if send_via_ftp:
        ok = FTPService().send(export) if ftp_settings.host else False
    order.status = "pedido_exportado" if ok else "error_exportacion"
    export.status = "sent" if ok else "error"
    if ok:
        order.exported_at = order.exported_at or _now()
        for line in order.lines or []:
            _sync_customer_product_knowledge(
                db,
                company_id=job.company_id,
                order=order,
                line=line,
                user=type("TenantLikeUser", (), {"id": job.created_by_user_id or 0, "company_id": job.company_id})(),
                source_context="pedido_exportado_ftp" if send_via_ftp else "pedido_exportado",
                exported_at=order.exported_at,
            )
        LearningService().record_case(
            db,
            company_id=job.company_id,
            summary=f"{_customer_label(order)} exportado {'por FTP/SFTP' if send_via_ftp else 'a salida'}.",
            resolved_action="pedido_exportado_ftp" if send_via_ftp else "pedido_exportado",
            resolution_json=json.dumps({"order_id": order.id, "customer": _customer_label(order), "export": export.filename}, ensure_ascii=False),
            customer_id=order.validated_customer_id or order.customer_id,
            order_id=order.id,
        )
    return {
        "ok": ok,
        "message": "Exportacion completada" if ok else "Exportacion fallida",
        "export": _serialize_export(export),
        "order_id": order.id,
    }


def _process_bulk_action(db, job: BackgroundJob, payload: dict) -> dict:
    action = str(payload.get("action") or "")
    target_state = str(payload.get("target_state") or "")
    items = payload.get("selected_items") or []
    if not isinstance(items, list):
        items = []
    processor = AgentProcessingService()
    scoring = get_or_create_settings(db, ScoringSettings, job.company_id)
    processed = 0
    skipped = 0
    total = len(items) or 1
    for item in items:
        if not isinstance(item, dict):
            skipped += 1
            update_job_progress(db, job, int(((processed + skipped) / total) * 100))
            continue
        kind = str(item.get("kind") or "")
        try:
            item_id = int(item.get("id") or 0)
        except (TypeError, ValueError):
            skipped += 1
            update_job_progress(db, job, int(((processed + skipped) / total) * 100))
            continue
        if not item_id:
            skipped += 1
            update_job_progress(db, job, int(((processed + skipped) / total) * 100))
            continue
        if kind == "order":
            order = db.scalar(
                select(Order)
                .where(Order.id == item_id, Order.company_id == job.company_id)
                .options(selectinload(Order.lines), selectinload(Order.customer), selectinload(Order.validated_customer), selectinload(Order.email).selectinload(Email.attachments))
            )
            if not order:
                skipped += 1
                update_job_progress(db, job, int(((processed + skipped) / total) * 100))
                continue
            if action in {"process", "reprocess"}:
                if order.email:
                    processor.process_email(db, order.email)
                else:
                    order.score = ScoringService().score_order(db, order)
                    order.status = ScoringService().status_for_score(db, job.company_id, order.score)
                db.commit()
                processed += 1
            elif action == "confirm":
                errors = validate_confirmation(order, scoring)
                if errors:
                    skipped += 1
                    update_job_progress(db, job, int(((processed + skipped) / total) * 100))
                    continue
                order.status = "pedido_confirmado"
                order.confirmed_at = _now()
                for line in order.lines or []:
                    _sync_customer_product_knowledge(
                        db,
                        company_id=job.company_id,
                        order=order,
                        line=line,
                        user=type("TenantLikeUser", (), {"id": job.created_by_user_id or 0, "company_id": job.company_id})(),
                        source_context="pedido_confirmado",
                        force_habitual=False,
                    )
                LearningService().record_case(
                    db,
                    company_id=job.company_id,
                    summary=f"{_customer_label(order)} confirmado con {len(order.lines or [])} lineas.",
                    resolved_action="pedido_confirmado",
                    resolution_json=json.dumps({"order_id": order.id, "customer": _customer_label(order), "lines": len(order.lines or [])}, ensure_ascii=False),
                    customer_id=order.validated_customer_id or order.customer_id,
                    order_id=order.id,
                )
                db.commit()
                processed += 1
            elif action == "export":
                export = ExportService().generate_csv(db, order)
                ftp_settings = get_or_create_settings(db, FTPSettings, job.company_id)
                ok = FTPService().send(export) if ftp_settings.host else False
                order.status = "pedido_exportado" if ok else "error_exportacion"
                if ok:
                    order.exported_at = _now()
                    export.status = "sent"
                else:
                    export.status = "error"
                db.commit()
                processed += 1
            elif action in {"delete", "discard"}:
                order.status = "descartado"
                db.commit()
                processed += 1
            elif action == "mark_no_order":
                order.status = "no_pedido"
                if order.email:
                    order.email.detected_type = "no_pedido"
                    order.email.status = "no_pedido"
                db.commit()
                processed += 1
            elif action == "change_state" and target_state:
                order.status = target_state
                db.commit()
                processed += 1
            elif action == "recalculate":
                order.score = ScoringService().score_order(db, order)
                order.status = ScoringService().status_for_score(db, job.company_id, order.score)
                db.commit()
                processed += 1
            else:
                skipped += 1
            update_job_progress(db, job, int(((processed + skipped) / total) * 100))
        elif kind == "email":
            email = db.get(Email, item_id)
            if not email or email.company_id != job.company_id:
                skipped += 1
                update_job_progress(db, job, int(((processed + skipped) / total) * 100))
                continue
            if action in {"process", "reprocess"}:
                processor.process_email(db, email)
                processed += 1
            elif action in {"mark_no_order", "resolve"}:
                email.status = "no_pedido"
                email.agent_status = "processed_no_order"
                email.detected_type = "no_pedido"
                email.processing_error = None
                db.commit()
                processed += 1
            elif action in {"delete", "discard"}:
                email.status = "descartado"
                email.agent_status = "discarded"
                db.commit()
                processed += 1
            elif action == "change_state" and target_state:
                email.status = target_state
                db.commit()
                processed += 1
            else:
                skipped += 1
            update_job_progress(db, job, int(((processed + skipped) / total) * 100))
        else:
            skipped += 1
            update_job_progress(db, job, int(((processed + skipped) / total) * 100))
    return {"ok": True, "message": f"Accion masiva {action}: {processed} aplicadas, {skipped} omitidas.", "processed": processed, "skipped": skipped}


def _handle_tenant_jobs(master_db, tenant: MasterTenantDatabase, owner: str) -> None:
    if not tenant.database_url:
        return
    session_factory = tenant_db_session(tenant.database_url)
    db = session_factory()
    try:
        while True:
            job = claim_next_job(
                db,
                owner=owner,
                job_types={
                    "email_sync",
                    "process_recent_emails",
                    "backfill_imap",
                    "process_pending_emails",
                    "process_email",
                    "process_order",
                    "import_confirm",
                    "import_file",
                    "export_order",
                    "export_order_ftp",
                    "bulk_order_action",
                },
            )
            if not job:
                break
            try:
                result = _process_job(db, job)
                if isinstance(result, dict) and result.get("ok") is False:
                    raise RuntimeError(str(result.get("message") or "El job devolvio ok=false."))
                finish_job(db, job, result)
                log_action(db, company_id=job.company_id, user=None, action=f"job.{job.job_type}.success", entity_type="job", entity_id=job.id, message=result.get("message") or "Trabajo completado")
            except Exception as exc:  # noqa: BLE001
                logger.exception("Job fallido company=%s job=%s", job.company_id, job.job_type)
                should_retry = job.retry_count < max(1, job.max_retries) and _is_retryable_exception(exc)
                failed_job = fail_job(db, job, str(exc), retry=should_retry, error_type=exc.__class__.__name__)
                action_suffix = "retrying" if failed_job.status == "retrying" else "failed"
                log_action(db, company_id=job.company_id, user=None, action=f"job.{job.job_type}.{action_suffix}", entity_type="job", entity_id=job.id, message=str(exc))
    finally:
        db.close()


def _worker_loop() -> None:
    settings = get_settings()
    poll_seconds = max(int(getattr(settings, "job_worker_poll_seconds", 10)), 5)
    while True:
        master_db = MasterSessionLocal()
        try:
            tenants = master_db.scalars(
                select(MasterTenantDatabase).where(
                    MasterTenantDatabase.is_active.is_(True),
                    MasterTenantDatabase.database_url.is_not(None),
                )
            ).all()
            for tenant in tenants:
                _handle_tenant_jobs(master_db, tenant, owner="job-worker")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Job worker error: %s", exc)
        finally:
            master_db.close()
        time.sleep(poll_seconds)


def start_job_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    threading.Thread(target=_worker_loop, name="anchi-job-worker", daemon=True).start()
