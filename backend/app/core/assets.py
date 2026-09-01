from __future__ import annotations

from pathlib import Path
from urllib.parse import quote


STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


def versioned_asset_url(path: str) -> str:
    """Return a cache-busting URL for a bundled static asset."""
    relative_path = Path(path)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("Static asset path must stay inside the static directory")

    static_root = STATIC_DIR.resolve()
    asset_path = (static_root / relative_path).resolve()
    if static_root not in asset_path.parents:
        raise ValueError("Static asset path must stay inside the static directory")

    stat = asset_path.stat()
    version = f"{stat.st_mtime_ns:x}-{stat.st_size:x}"
    encoded_path = quote(relative_path.as_posix(), safe="/")
    return f"/static/{encoded_path}?v={version}"
