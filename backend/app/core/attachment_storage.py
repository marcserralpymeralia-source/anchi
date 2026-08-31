from __future__ import annotations

import os
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


def save_attachment(
    *,
    filename: str,
    payload: bytes,
    content_type: str | None = None,
) -> str:
    safe_filename = Path(filename).name
    storage_name = f"attachments/{uuid4().hex}-{safe_filename}"

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
        from vercel.blob import BlobClient

        client = BlobClient()
        try:
            result = client.get(storage_ref, access="private")
            return result.content
        finally:
            client.close()

    return Path(storage_ref).read_bytes()
