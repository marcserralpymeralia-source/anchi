from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4

from app.core.storage import resolve_temp_storage_dir


def _is_vercel_runtime() -> bool:
    return os.getenv("VERCEL") == "1" or bool(os.getenv("VERCEL_ENV"))


def _use_vercel_blob() -> bool:
    return bool(
        _is_vercel_runtime()
        and os.getenv("BLOB_READ_WRITE_TOKEN")
    )


def _normalize_store_id(value: str) -> str:
    normalized = value.strip()
    if normalized.lower().startswith("store_"):
        normalized = normalized[6:]
    return normalized


def _uat_store_id() -> str | None:
    value = _normalize_store_id(os.getenv("UAT_STORE_ID", ""))
    return value or None


def _use_oidc_blob() -> bool:
    return bool(_is_vercel_runtime() and _uat_store_id())


def _uat_blob_hostname(store_id: str) -> str:
    return f"{store_id.lower()}.private.blob.vercel-storage.com"


def _get_oidc_token() -> str:
    try:
        from vercel.oidc import get_vercel_oidc_token
    except ImportError as exc:
        raise RuntimeError("Vercel OIDC support is not installed.") from exc

    token = get_vercel_oidc_token()
    if not token:
        raise RuntimeError("Vercel OIDC token is not available for Blob storage.")
    return token


def _put_oidc_blob(
    *,
    storage_name: str,
    payload: bytes,
    content_type: str,
    store_id: str,
) -> str:
    query = urllib.parse.urlencode({"pathname": storage_name})
    token = _get_oidc_token()
    if not token:
        raise RuntimeError("Vercel OIDC token is not available for Blob storage.")
    request = urllib.request.Request(
        f"https://vercel.com/api/blob/?{query}",
        data=payload,
        method="PUT",
        headers={
            "authorization": f"Bearer {token}",
            "x-vercel-blob-store-id": store_id,
            "x-api-version": "12",
            "x-api-blob-request-id": uuid4().hex,
            "x-api-blob-request-attempt": "0",
            "x-vercel-blob-access": "private",
            "x-content-type": content_type,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    url = result.get("url")
    if not isinstance(url, str) or not url:
        raise RuntimeError("Vercel Blob did not return an attachment URL.")
    return url


def _read_oidc_blob(storage_ref: str) -> bytes:
    store_id = _uat_store_id()
    parsed = urllib.parse.urlparse(storage_ref)
    if not store_id or parsed.scheme != "https" or parsed.hostname != _uat_blob_hostname(store_id):
        raise RuntimeError("Attachment URL is outside the configured UAT Blob store.")

    request = urllib.request.Request(
        storage_ref,
        method="GET",
        headers={"authorization": f"Bearer {_get_oidc_token()}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def _delete_oidc_blob(storage_ref: str) -> None:
    store_id = _uat_store_id()
    parsed = urllib.parse.urlparse(storage_ref)
    if not store_id or parsed.scheme != "https" or parsed.hostname != _uat_blob_hostname(store_id):
        raise RuntimeError("Attachment URL is outside the configured UAT Blob store.")

    payload = json.dumps({"urls": [storage_ref]}).encode("utf-8")
    token = _get_oidc_token()
    request = urllib.request.Request(
        "https://vercel.com/api/blob/delete",
        data=payload,
        method="POST",
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "x-vercel-blob-store-id": store_id,
            "x-api-version": "12",
            "x-api-blob-request-id": uuid4().hex,
            "x-api-blob-request-attempt": "0",
        },
    )
    with urllib.request.urlopen(request, timeout=30):
        return None


def save_attachment(
    *,
    filename: str,
    payload: bytes,
    content_type: str | None = None,
) -> str:
    safe_filename = Path(filename).name
    storage_name = f"attachments/{uuid4().hex}-{safe_filename}"

    if _use_oidc_blob():
        return _put_oidc_blob(
            storage_name=storage_name,
            payload=payload,
            content_type=content_type or "application/octet-stream",
            store_id=_uat_store_id(),
        )

    if _use_vercel_blob():
        from vercel.blob import BlobClient

        client = BlobClient()
        try:
            result = client.put(
                storage_name,
                payload,
                access="private",
                content_type=content_type or "application/octet-stream",
                overwrite=False,
            )
            return result.url
        finally:
            client.close()

    if _is_vercel_runtime():
        raise RuntimeError("Persistent attachment storage is not configured for Vercel.")

    root = resolve_temp_storage_dir("attachments")
    root.mkdir(parents=True, exist_ok=True)

    path = root / storage_name.replace("attachments/", "", 1)
    path.write_bytes(payload)
    return str(path)


def read_attachment(storage_ref: str) -> bytes:
    if storage_ref.startswith(("https://", "http://")):
        if _use_oidc_blob():
            return _read_oidc_blob(storage_ref)

        from vercel.blob import BlobClient

        client = BlobClient()
        try:
            result = client.get(storage_ref, access="private")
            return result.content
        finally:
            client.close()

    return Path(storage_ref).read_bytes()
