from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.core.attachment_storage import (
    _delete_oidc_blob,
    _put_oidc_blob,
    _read_oidc_blob,
    _uat_blob_hostname,
    _uat_store_id,
)
from app.cron.routes import _cron_authorized

router = APIRouter()


def _safe_blob_error(error: BaseException) -> tuple[str, str]:
    if isinstance(error, urllib.error.HTTPError):
        try:
            payload = json.loads(error.read(512).decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            payload = {}
        details = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(details, dict):
            code = details.get("code")
            message = details.get("message")
            if isinstance(code, str) and isinstance(message, str):
                return code, message[:240]
        return f"http_{error.code}", "Blob request rejected."
    return type(error).__name__, "Blob health check failed."


@router.post("/internal/uat/blob-health")
def blob_health(request: Request):
    if os.getenv("VERCEL_ENV", "").strip().lower() != "preview":
        raise HTTPException(status_code=404, detail="Not found")

    store_id = _uat_store_id()
    if not store_id:
        raise HTTPException(status_code=404, detail="UAT Blob is not configured")
    _cron_authorized(request)

    content = b"gemavi-uat-blob-health"
    storage_name = f"uat-healthcheck/{uuid4().hex}.txt"
    storage_ref: str | None = None
    deleted = False
    try:
        storage_ref = _put_oidc_blob(
            storage_name=storage_name,
            payload=content,
            content_type="text/plain",
            store_id=store_id,
        )
        parsed = urllib.parse.urlparse(storage_ref)
        if parsed.scheme != "https" or parsed.hostname != _uat_blob_hostname(store_id):
            raise RuntimeError("Vercel Blob returned an unexpected store hostname.")
        read_content = _read_oidc_blob(storage_ref)
        if read_content != content:
            raise RuntimeError("Vercel Blob returned unexpected content.")
        _delete_oidc_blob(storage_ref)
        deleted = True
        return {
            "ok": True,
            "store_id": f"store_{store_id}",
            "write": True,
            "read": True,
            "delete": True,
        }
    except Exception as error:  # noqa: BLE001
        if storage_ref and not deleted:
            try:
                _delete_oidc_blob(storage_ref)
            except Exception:  # noqa: BLE001
                pass
        code, message = _safe_blob_error(error)
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "store_id": f"store_{store_id}",
                "write": False,
                "read": False,
                "delete": deleted,
                "error_code": code,
                "message": message,
            },
        )
