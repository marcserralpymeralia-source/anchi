from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from app.core.storage import configured_storage_backend, resolve_temp_storage_dir


def _blob_token() -> str:
    token = os.getenv("BLOB_READ_WRITE_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Vercel Blob está seleccionado pero falta su credencial.")
    return token


def _blob_store_host() -> str:
    host = os.getenv("BLOB_STORE_HOST", "").strip().lower().rstrip(".")
    parsed = urlsplit(f"//{host}") if host else None
    if (
        not parsed
        or parsed.hostname != host
        or parsed.port is not None
        or not host.endswith(".private.blob.vercel-storage.com")
    ):
        raise RuntimeError("Vercel Blob requiere el hostname privado exacto del store.")
    return host


def save_attachment(
    *,
    filename: str,
    payload: bytes,
    content_type: str | None = None,
    company_id: int | None = None,
) -> str:
    safe_filename = Path(filename).name
    prefix = f"tenants/{int(company_id)}/attachments" if company_id is not None else "attachments"
    storage_name = f"{prefix}/{uuid4().hex}-{safe_filename}"

    backend = configured_storage_backend()
    if backend == "vercel_blob":
        token = _blob_token()
        store_host = _blob_store_host()
        from vercel.blob import BlobClient

        client = BlobClient()
        try:
            result = client.put(
                storage_name,
                payload,
                access="private",
                content_type=content_type or "application/octet-stream",
                overwrite=False,
                token=token,
            )
            object_url = getattr(result, "url", None)
            if not object_url:
                raise RuntimeError("Vercel Blob no devolvió referencia para el objeto.")
            try:
                object_host = (urlsplit(object_url).hostname or "").lower().rstrip(".")
            except ValueError:
                object_host = ""
            if object_host != store_host:
                try:
                    client.delete(object_url, token=token)
                except Exception:  # noqa: BLE001
                    pass
                raise RuntimeError("Vercel Blob devolvió un objeto fuera del store configurado.")
            return object_url
        finally:
            client.close()

    if backend != "filesystem":
        raise RuntimeError("El backend de almacenamiento duradero no está configurado correctamente.")

    root = resolve_temp_storage_dir("attachments")
    root.mkdir(parents=True, exist_ok=True)

    path = root / storage_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return str(path)


def read_attachment(storage_ref: str, *, company_id: int | None = None) -> bytes:
    if storage_ref.startswith(("https://", "http://")):
        if configured_storage_backend() != "vercel_blob":
            raise RuntimeError("No se puede leer una referencia Blob sin seleccionar explícitamente ese backend.")
        parsed = urlsplit(storage_ref)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        store_host = _blob_store_host()
        if (
            parsed.scheme != "https"
            or hostname != store_host
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeError("La referencia no pertenece a un Blob privado autorizado.")
        if company_id is None or int(company_id) <= 0:
            raise RuntimeError("La lectura Blob requiere el tenant autorizado.")
        object_path = parsed.path.lstrip("/")
        expected_prefix = f"tenants/{int(company_id)}/attachments/"
        if not object_path.startswith(expected_prefix):
            raise RuntimeError("La referencia Blob no pertenece al tenant solicitado.")
        token = _blob_token()
        from vercel.blob import BlobClient

        client = BlobClient()
        try:
            result = client.get(storage_ref, access="private", token=token)
            if result is None:
                raise FileNotFoundError("El objeto Blob solicitado no existe.")
            return result.content
        finally:
            client.close()

    if configured_storage_backend() != "filesystem":
        raise RuntimeError("No se permite fallback a filesystem para el backend seleccionado.")
    return Path(storage_ref).read_bytes()
