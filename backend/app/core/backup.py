from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_path(database_url: str) -> Path:
    url = make_url(database_url)
    if url.drivername not in {"sqlite", "sqlite+pysqlite"} or not url.database or url.database == ":memory:":
        raise ValueError("El backup SQLite requiere una URL sqlite con fichero.")
    return Path(url.database).expanduser()


def _sqlite_schema_metadata(path: Path) -> dict[str, Any]:
    with sqlite3.connect(path) as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        migration = None
        if "schema_migrations" in tables:
            migration = db.execute(
                "SELECT version, checksum, status FROM schema_migrations ORDER BY id DESC LIMIT 1"
            ).fetchone()
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError("La base SQLite de origen no supera integrity_check.")
    return {
        "table_count": len(tables),
        "schema_version": migration[0] if migration else None,
        "schema_checksum": migration[1] if migration else None,
        "schema_status": migration[2] if migration else None,
    }


def _write_metadata(artifact: Path, metadata: dict[str, Any]) -> Path:
    metadata_path = Path(f"{artifact}.json")
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return metadata_path


def backup_sqlite(
    source_url: str,
    destination: str | Path,
    *,
    environment: str,
    database_identity: str,
) -> dict[str, Any]:
    source = _sqlite_path(source_url)
    artifact = Path(destination).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"No existe la base SQLite de origen: {source}")
    artifact.parent.mkdir(parents=True, exist_ok=True)
    if artifact.resolve() == source.resolve():
        raise ValueError("El backup debe escribirse en un artefacto distinto del origen.")
    if artifact.exists():
        artifact.unlink()
    with sqlite3.connect(source) as source_db, sqlite3.connect(artifact) as backup_db:
        source_db.backup(backup_db)
        integrity = backup_db.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError("El backup SQLite no supera integrity_check.")
    metadata = {
        "created_at": _now(),
        "environment": environment,
        "database_identity": database_identity,
        "engine": "sqlite",
        "artifact": artifact.name,
        "sha256": _sha256(artifact),
        **_sqlite_schema_metadata(artifact),
    }
    metadata["metadata_file"] = _write_metadata(artifact, metadata).name
    return metadata


def restore_sqlite(source: str | Path, destination_url: str) -> dict[str, Any]:
    source_path = Path(source).expanduser()
    destination = _sqlite_path(destination_url)
    if not source_path.is_file():
        raise FileNotFoundError(f"No existe el artefacto SQLite: {source_path}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    with sqlite3.connect(source_path) as source_db, sqlite3.connect(destination) as destination_db:
        source_db.backup(destination_db)
        integrity = destination_db.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError("La base SQLite restaurada no supera integrity_check.")
    return {
        "restored_at": _now(),
        "engine": "sqlite",
        "destination": destination.name,
        "sha256": _sha256(destination),
        **_sqlite_schema_metadata(destination),
    }


def _postgres_args(database_url: str, *, command: str, output: Path | None = None) -> tuple[list[str], dict[str, str]]:
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql") or not url.database:
        raise ValueError("Se requiere una URL PostgreSQL válida.")
    args = [command]
    if command == "pg_dump":
        args.extend(["--format=custom", "--no-owner", "--file", str(output)])
    else:
        args.extend(["--exit-on-error", "--no-owner", "--dbname"])
    if url.host:
        args.extend(["--host", url.host])
    if url.port:
        args.extend(["--port", str(url.port)])
    if url.username:
        args.extend(["--username", url.username])
    if command == "pg_dump":
        args.append(url.database)
    else:
        args.append(url.database)
    env = os.environ.copy()
    if url.password:
        env["PGPASSWORD"] = url.password
    if url.query.get("sslmode"):
        env["PGSSLMODE"] = str(url.query["sslmode"])
    return args, env


def pg_dump(database_url: str, destination: str | Path, *, environment: str, database_identity: str) -> dict[str, Any]:
    artifact = Path(destination).expanduser()
    artifact.parent.mkdir(parents=True, exist_ok=True)
    args, env = _postgres_args(database_url, command="pg_dump", output=artifact)
    subprocess.run(args, env=env, check=True, capture_output=True, text=True)
    database_target = make_url(database_url)
    metadata = {
        "created_at": _now(),
        "environment": environment,
        "database_identity": database_identity,
        "engine": "postgresql",
        "artifact": artifact.name,
        "sha256": _sha256(artifact),
        "source_database_target": {"host": database_target.host, "port": database_target.port, "database": database_target.database},
    }
    metadata["metadata_file"] = _write_metadata(artifact, metadata).name
    return metadata


def pg_restore(source: str | Path, database_url: str, *, target_identity: str) -> dict[str, Any]:
    artifact = Path(source).expanduser()
    if not artifact.is_file():
        raise FileNotFoundError(f"No existe el artefacto PostgreSQL: {artifact}")
    metadata_path = Path(f"{artifact}.json")
    if not metadata_path.is_file():
        raise ValueError("Falta metadata sidecar para verificar el backup PostgreSQL.")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("sha256") != _sha256(artifact):
        raise ValueError("El checksum del backup PostgreSQL no coincide con su metadata.")
    target_identity = str(target_identity or "").strip()
    if not target_identity or target_identity == str(metadata.get("database_identity") or ""):
        raise ValueError("Restore PostgreSQL requiere una identidad destino distinta de la fuente.")
    source_target = metadata.get("source_database_target")
    target = make_url(database_url)
    if isinstance(source_target, dict) and source_target == {"host": target.host, "port": target.port, "database": target.database}:
        raise ValueError("Restore PostgreSQL no puede usar la misma base que el backup de origen.")
    args, env = _postgres_args(database_url, command="pg_restore")
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            has_user_relations = bool(connection.execute(text(
                "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%')"
            )).scalar())
            has_custom_schemas = bool(connection.execute(text(
                "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname NOT IN "
                "('pg_catalog', 'information_schema', 'public') AND nspname NOT LIKE 'pg_toast%')"
            )).scalar())
            has_public_routines = bool(connection.execute(text(
                "SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'public')"
            )).scalar())
            has_public_types = bool(connection.execute(text(
                "SELECT EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE n.nspname = 'public' AND t.typrelid = 0)"
            )).scalar())
            if has_user_relations or has_custom_schemas or has_public_routines or has_public_types:
                raise ValueError("La base destino PostgreSQL no está vacía; restore abortado.")
        subprocess.run(args + ["--single-transaction", str(artifact)], env=env, check=True, capture_output=True, text=True)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    finally:
        engine.dispose()
    return {
        "restored_at": _now(),
        "engine": "postgresql",
        "artifact": artifact.name,
        "sha256": _sha256(artifact),
        "source_identity": metadata.get("database_identity"),
        "target_identity": target_identity,
        "post_restore_validation": "SELECT 1 passed",
    }


def backup_database(database_url: str, destination: str | Path, *, environment: str, database_identity: str) -> dict[str, Any]:
    if database_url.startswith("sqlite"):
        return backup_sqlite(database_url, destination, environment=environment, database_identity=database_identity)
    if database_url.startswith("postgresql"):
        return pg_dump(database_url, destination, environment=environment, database_identity=database_identity)
    raise ValueError("Motor de base de datos no soportado para backup.")


def verify_sqlite_backup(artifact: str | Path) -> dict[str, Any]:
    path = Path(artifact).expanduser()
    metadata_path = Path(f"{path}.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    actual_sha256 = _sha256(path)
    if metadata.get("sha256") and metadata["sha256"] != actual_sha256:
        raise RuntimeError("El checksum del backup no coincide con su metadata.")
    return {"ok": True, "sha256": actual_sha256, **_sqlite_schema_metadata(path)}
