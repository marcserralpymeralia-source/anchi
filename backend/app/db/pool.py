"""Bounded SQLAlchemy pool options for long-lived runtime engines."""

from __future__ import annotations

from app.core.config import Settings, get_settings


def runtime_pool_options(database_url: str, settings: Settings | None = None) -> dict[str, int]:
    """Return conservative pool bounds; SQLite keeps its dialect defaults."""

    if database_url.startswith("sqlite"):
        return {"pool_pre_ping": True}
    runtime_settings = settings or get_settings()
    return {
        "pool_size": runtime_settings.database_pool_size,
        "max_overflow": runtime_settings.database_max_overflow,
        "pool_timeout": runtime_settings.database_pool_timeout_seconds,
        "pool_pre_ping": True,
    }
