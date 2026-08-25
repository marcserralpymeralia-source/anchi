from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4


def _use_vercel_blob() -> bool:
    return bool(
        os.getenv("VERCEL")
        and (
            os.getenv("BLOB_READ_WRITE_TOKEN")
            or os.getenv("BLOB_STORE_ID")
        )
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

    root = Path("app/storage/attachments")
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
