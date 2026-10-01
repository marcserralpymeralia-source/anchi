from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4


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


def check_storage_readiness() -> dict[str, object]:
    """Probe the configured local storage without exposing its absolute path."""

    if os.getenv("VERCEL") == "1" or os.getenv("VERCEL_ENV"):
        if os.getenv("BLOB_READ_WRITE_TOKEN", "").strip():
            return {
                "ok": True,
                "backend": "vercel_blob",
                "probe": "configuration",
            }
        return {
            "ok": False,
            "backend": "vercel_blob",
            "probe": "configuration",
            "error_type": "missing_configuration",
        }

    probe_root = resolve_temp_storage_dir(".health")
    probe_path = probe_root / f"storage-{uuid4().hex}.probe"
    payload = uuid4().hex.encode("ascii")
    try:
        probe_root.mkdir(parents=True, exist_ok=True)
        probe_path.write_bytes(payload)
        if probe_path.read_bytes() != payload:
            raise OSError("storage probe read-back mismatch")
        return {
            "ok": True,
            "backend": "filesystem",
            "probe": "write_read_delete",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "backend": "filesystem",
            "probe": "write_read_delete",
            "error_type": exc.__class__.__name__,
            "error": str(exc)[:240],
        }
    finally:
        try:
            probe_path.unlink(missing_ok=True)
        except OSError:
            pass
