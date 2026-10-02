from __future__ import annotations

import os
import hashlib
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4


_PROBE_TTL_SECONDS = 60.0
_probe_lock = threading.Lock()
_probe_cache: dict[str, object] | None = None
_probe_cache_until = 0.0
_probe_cache_key: str | None = None


def configured_storage_backend() -> str:
    configured = os.getenv("ATTACHMENT_STORAGE_BACKEND", "").strip().lower()
    if configured in {"filesystem", "vercel_blob"}:
        return configured
    if configured:
        return "invalid"
    if os.getenv("APP_ENV", "development").strip().lower() in {"development", "test"}:
        return "filesystem"
    return "unconfigured"


def resolve_temp_storage_root() -> Path:
    configured = os.getenv("TEMP_STORAGE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    if os.getenv("VERCEL") == "1":
        return Path("/tmp/anchi")
    return Path(__file__).resolve().parents[1] / "storage"


def resolve_temp_storage_dir(*parts: str) -> Path:
    return resolve_temp_storage_root().joinpath(*parts)


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _probe_blob() -> dict[str, object]:
    token = os.getenv("BLOB_READ_WRITE_TOKEN", "").strip()
    store_host = os.getenv("BLOB_STORE_HOST", "").strip().lower().rstrip(".")
    if not token or not store_host:
        return {"ok": False, "backend": "vercel_blob", "probe": "write_read_delete", "error_type": "missing_configuration"}
    path = f"anchi-readiness/{uuid4().hex}.probe"
    client = None
    object_url = None
    error_type = None
    try:
        from vercel.blob import BlobClient

        client = BlobClient()
        payload = uuid4().hex.encode("ascii")
        uploaded = client.put(path, payload, access="private", content_type="application/octet-stream", overwrite=False, token=token)
        url = getattr(uploaded, "url", None)
        if not url:
            raise RuntimeError("Blob probe upload returned no object reference")
        object_url = url
        if (urlsplit(url).hostname or "").lower().rstrip(".") != store_host:
            raise RuntimeError("Blob probe returned an object outside the configured store")
        downloaded = client.get(url, access="private", token=token)
        if downloaded is None or downloaded.content != payload:
            raise RuntimeError("Blob probe read-back mismatch")
    except Exception as exc:  # noqa: BLE001
        error_type = exc.__class__.__name__
    finally:
        if client is not None:
            if object_url:
                try:
                    client.delete(object_url, token=token)
                except Exception as exc:  # noqa: BLE001
                    error_type = error_type or exc.__class__.__name__
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
    return {
        "ok": error_type is None,
        "backend": "vercel_blob",
        "probe": "write_read_delete",
        **({"error_type": error_type} if error_type else {}),
    }


def check_storage_readiness(*, force: bool = False) -> dict[str, object]:
    """Bound the real provider probe so routine readiness polling is not a write loop."""
    global _probe_cache, _probe_cache_until, _probe_cache_key
    backend = configured_storage_backend()
    if backend == "vercel_blob":
        token = os.getenv("BLOB_READ_WRITE_TOKEN", "").strip()
        store_host = os.getenv("BLOB_STORE_HOST", "").strip().lower().rstrip(".")
        cache_key = f"{backend}:{store_host}:{hashlib.sha256(token.encode('utf-8')).hexdigest()}"
        with _probe_lock:
            now = time.monotonic()
            if not force and _probe_cache is not None and cache_key == _probe_cache_key and now < _probe_cache_until:
                return dict(_probe_cache)
            result = _probe_blob()
            _probe_cache = dict(result)
            _probe_cache_until = now + _PROBE_TTL_SECONDS
            _probe_cache_key = cache_key
            return result
    if backend != "filesystem":
        return {"ok": False, "backend": backend, "probe": "configuration", "error_type": "invalid_or_missing_configuration"}

    probe_root = resolve_temp_storage_dir(".health")
    probe_path = probe_root / f"storage-{uuid4().hex}.probe"
    payload = uuid4().hex.encode("ascii")
    try:
        probe_root.mkdir(parents=True, exist_ok=True)
        probe_path.write_bytes(payload)
        if probe_path.read_bytes() != payload:
            raise OSError("storage probe read-back mismatch")
        return {"ok": True, "backend": "filesystem", "probe": "write_read_delete"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "backend": "filesystem", "probe": "write_read_delete", "error_type": exc.__class__.__name__}
    finally:
        try:
            probe_path.unlink(missing_ok=True)
        except OSError:
            pass
