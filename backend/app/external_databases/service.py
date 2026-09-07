from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.engine import Engine, URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.encryption import decrypt_secret
from app.db.models import ExternalDatabaseConnection, ExternalDatabaseMapping, ProxyConnection
from app.master_data.service import upsert_customer, upsert_product

logger = logging.getLogger(__name__)

SUPPORTED_DATABASE_TYPES = {
    "postgresql": {"label": "PostgreSQL", "driver": "postgresql+psycopg", "default_port": 5432},
    "mysql": {"label": "MySQL / MariaDB", "driver": "mysql+pymysql", "default_port": 3306},
    "sqlite": {"label": "SQLite (solo demo)", "driver": "sqlite+pysqlite", "default_port": 0},
}
SSL_MODES = {"require", "verify-full", "disable"}
ENTITY_FIELDS = {
    "customers": (
        ("code", "Código", True),
        ("fiscal_name", "Razón social", True),
        ("commercial_name", "Nombre comercial", False),
        ("primary_email", "Email", False),
        ("phone", "Teléfono", False),
        ("tax_id", "CIF / NIF", False),
        ("address", "Dirección", False),
        ("city", "Ciudad", False),
        ("province", "Provincia", False),
        ("country", "País", False),
        ("category", "Categoría", False),
        ("status", "Estado", False),
    ),
    "products": (
        ("reference", "Referencia", True),
        ("name", "Nombre", True),
        ("alternative_code", "Código alternativo", False),
        ("description", "Descripción", False),
        ("brand", "Marca", False),
        ("usual_supplier", "Proveedor habitual", False),
        ("family", "Familia", False),
        ("subfamily", "Subfamilia", False),
        ("format", "Formato", False),
        ("sale_unit", "Unidad de venta", False),
        ("ean", "EAN", False),
        ("sale_price", "Precio", False),
        ("discount_percent", "Descuento %", False),
        ("status", "Estado", False),
    ),
}
MAX_TABLES = 200
MAX_COLUMNS = 100
MAX_PREVIEW_ROWS = 50
MAX_SYNC_ROWS = 5000
_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9][A-Za-z0-9_.:-]*$")


class ExternalDatabaseError(ValueError):
    """Expected, user-actionable connector error without leaking credentials."""


def database_type_options() -> list[dict[str, Any]]:
    return [{"value": key, "label": value["label"], "default_port": value["default_port"]} for key, value in SUPPORTED_DATABASE_TYPES.items()]


def entity_field_options() -> dict[str, list[dict[str, Any]]]:
    return {
        entity_type: [{"key": key, "label": label, "required": required} for key, label, required in fields]
        for entity_type, fields in ENTITY_FIELDS.items()
    }


def _validate_host(host: str, database_type: str) -> str:
    value = (host or "").strip()
    if database_type == "sqlite":
        return value or ":memory:"
    if not value or "/" in value or "\\" in value or any(character.isspace() for character in value) or not _HOST_RE.fullmatch(value):
        raise ExternalDatabaseError("Introduce un host o una IP válida, sin protocolo ni ruta.")
    return value


def normalize_connection_values(data: dict[str, Any], existing: ExternalDatabaseConnection | None = None) -> dict[str, Any]:
    name = str(data.get("name") or "").strip()
    if not name or len(name) > 120:
        raise ExternalDatabaseError("El nombre de la conexión es obligatorio y no puede superar 120 caracteres.")
    database_type = str(data.get("database_type") or "postgresql").strip().lower()
    if database_type not in SUPPORTED_DATABASE_TYPES:
        raise ExternalDatabaseError("El tipo de base de datos no está soportado.")
    host = _validate_host(str(data.get("host") or (existing.host if existing else "")), database_type)
    try:
        port = int(data.get("port") or (existing.port if existing else SUPPORTED_DATABASE_TYPES[database_type]["default_port"]))
    except (TypeError, ValueError) as exc:
        raise ExternalDatabaseError("El puerto debe ser un número válido.") from exc
    if database_type != "sqlite" and not 1 <= port <= 65535:
        raise ExternalDatabaseError("El puerto debe estar entre 1 y 65535.")
    database_name = str(data.get("database_name") or (existing.database_name if existing else "")).strip()
    if not database_name or len(database_name) > 255:
        raise ExternalDatabaseError("El nombre de la base de datos es obligatorio.")
    schema_name = str(data.get("schema_name") or (existing.schema_name if existing else "public")).strip() or "public"
    if len(schema_name) > 120 or any(character.isspace() for character in schema_name):
        raise ExternalDatabaseError("El esquema indicado no es válido.")
    username = str(data.get("username") or (existing.username if existing else "")).strip()
    if database_type != "sqlite" and not username:
        raise ExternalDatabaseError("El usuario de la base de datos es obligatorio.")
    ssl_mode = str(data.get("ssl_mode") or (existing.ssl_mode if existing else "require")).strip().lower()
    if ssl_mode not in SSL_MODES:
        raise ExternalDatabaseError("El modo SSL indicado no es válido.")
    proxy_raw = str(data.get("proxy_connection_id") or "").strip()
    try:
        proxy_connection_id = int(proxy_raw) if proxy_raw else None
    except ValueError as exc:
        raise ExternalDatabaseError("El proxy seleccionado no es válido.") from exc
    return {
        "name": name,
        "database_type": database_type,
        "host": host,
        "port": port,
        "database_name": database_name,
        "schema_name": schema_name,
        "username": username,
        "ssl_mode": ssl_mode,
        "proxy_connection_id": proxy_connection_id,
        "enabled": str(data.get("enabled") or "").lower() in {"on", "true", "1"},
    }


def _safe_error_message(exc: Exception) -> str:
    """Return a useful connector error while stripping URLs and credentials."""

    message = str(exc).replace("\r", " ").replace("\n", " ").strip()
    message = re.sub(r"(://)([^/@\s]+)@", r"\1***@", message)
    message = re.sub(r"password[=:][^\s,;]+", "password=***", message, flags=re.IGNORECASE)
    return message[:400] or "Error no especificado del conector."


def _ensure_driver(database_type: str) -> None:
    if database_type == "mysql":
        try:
            import pymysql  # noqa: F401
        except ImportError as exc:
            raise ExternalDatabaseError("Falta la dependencia PyMySQL para conectar con MySQL/MariaDB.") from exc


def _engine_for(connection: ExternalDatabaseConnection, password: str | None) -> Engine:
    if connection.proxy_connection_id:
        raise ExternalDatabaseError("El gateway proxy seleccionado todavía solo expone su endpoint de salud; el túnel de base de datos aún no está habilitado.")
    database_type = (connection.database_type or "").lower()
    driver_config = SUPPORTED_DATABASE_TYPES.get(database_type)
    if not driver_config:
        raise ExternalDatabaseError("El tipo de base de datos no está soportado.")
    _ensure_driver(database_type)
    if database_type == "sqlite":
        database = connection.database_name if connection.database_name == ":memory:" else connection.database_name
        return create_engine(
            URL.create(drivername=driver_config["driver"], database=database),
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )
    if not password:
        raise ExternalDatabaseError("La contraseña de la conexión no está configurada.")
    query = {"sslmode": connection.ssl_mode} if database_type == "postgresql" and connection.ssl_mode else {}
    url = URL.create(
        drivername=driver_config["driver"],
        username=connection.username,
        password=password,
        host=connection.host,
        port=connection.port,
        database=connection.database_name,
        query=query,
    )
    connect_args: dict[str, Any] = {"connect_timeout": 5}
    return create_engine(url, connect_args=connect_args, pool_pre_ping=True, pool_size=1, max_overflow=0)


def _run_read_only_probe(engine: Engine) -> None:
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            if conn.dialect.name == "postgresql":
                conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            conn.execute(text("SELECT 1"))
        finally:
            transaction.rollback()


def _connection_password(connection: ExternalDatabaseConnection) -> str | None:
    return decrypt_secret(connection.password_encrypted)


def test_connection(connection: ExternalDatabaseConnection) -> tuple[bool, str]:
    engine: Engine | None = None
    try:
        engine = _engine_for(connection, _connection_password(connection))
        _run_read_only_probe(engine)
        return True, "Conexión correcta. Anchi solo ejecutará lecturas de consulta."
    except (ExternalDatabaseError, SQLAlchemyError, OSError) as exc:
        return False, _safe_error_message(exc)
    finally:
        if engine is not None:
            engine.dispose()


def scan_schema(connection: ExternalDatabaseConnection) -> dict[str, Any]:
    engine: Engine | None = None
    try:
        engine = _engine_for(connection, _connection_password(connection))
        _run_read_only_probe(engine)
        inspector = inspect(engine)
        schema = None if connection.database_type == "sqlite" else (connection.schema_name or "public")
        table_names = inspector.get_table_names(schema=schema)[:MAX_TABLES]
        tables: list[dict[str, Any]] = []
        for table_name in sorted(table_names, key=str.casefold):
            columns = inspector.get_columns(table_name, schema=schema)[:MAX_COLUMNS]
            tables.append(
                {
                    "schema": schema or "main",
                    "name": table_name,
                    "columns": [
                        {"name": column.get("name"), "type": str(column.get("type") or ""), "nullable": bool(column.get("nullable", True))}
                        for column in columns
                    ],
                }
            )
        return {"schema": schema or "main", "tables": tables, "truncated": len(table_names) >= MAX_TABLES}
    except (ExternalDatabaseError, SQLAlchemyError, OSError) as exc:
        raise ExternalDatabaseError(_safe_error_message(exc)) from exc
    finally:
        if engine is not None:
            engine.dispose()


def _mapping_dict(mapping: ExternalDatabaseMapping) -> dict[str, str]:
    try:
        payload = json.loads(mapping.field_map_json or "{}")
    except (TypeError, ValueError) as exc:
        raise ExternalDatabaseError("El mapeo guardado no es válido.") from exc
    if not isinstance(payload, dict):
        raise ExternalDatabaseError("El mapeo guardado no es válido.")
    return {str(key): str(value) for key, value in payload.items() if str(key).strip() and str(value).strip()}


def validate_mapping_payload(entity_type: str, field_map: dict[str, Any], schema_payload: dict[str, Any]) -> dict[str, str]:
    if entity_type not in ENTITY_FIELDS:
        raise ExternalDatabaseError("El tipo de datos a mapear no es válido.")
    available = {
        str(column["name"])
        for table in schema_payload.get("tables", [])
        for column in table.get("columns", [])
        if column.get("name") is not None
    }
    normalized = {str(key).strip(): str(value).strip() for key, value in field_map.items() if str(key).strip() and str(value).strip()}
    invalid = [source for source in normalized.values() if source not in available]
    if invalid:
        raise ExternalDatabaseError("El mapeo contiene columnas que ya no existen en el esquema escaneado.")
    missing = [key for key, _label, required in ENTITY_FIELDS[entity_type] if required and not normalized.get(key)]
    if missing:
        labels = {key: label for key, label, _required in ENTITY_FIELDS[entity_type]}
        raise ExternalDatabaseError("Faltan campos obligatorios: " + ", ".join(labels[key] for key in missing) + ".")
    return normalized


def _reflected_table(engine: Engine, mapping: ExternalDatabaseMapping) -> Table:
    schema = None if engine.dialect.name == "sqlite" else (mapping.table_schema or "public")
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names(schema=schema))
    if mapping.table_name not in table_names:
        raise ExternalDatabaseError("La tabla del mapeo ya no existe en la base de datos externa.")
    return Table(mapping.table_name, MetaData(), schema=schema, autoload_with=engine)


def read_mapping_rows(connection: ExternalDatabaseConnection, mapping: ExternalDatabaseMapping, limit: int) -> list[dict[str, Any]]:
    field_map = _mapping_dict(mapping)
    safe_limit = max(min(int(limit), MAX_SYNC_ROWS), 1)
    engine: Engine | None = None
    try:
        engine = _engine_for(connection, _connection_password(connection))
        table = _reflected_table(engine, mapping)
        selected = [table.c[source].label(target) for target, source in field_map.items() if source in table.c]
        if not selected:
            raise ExternalDatabaseError("El mapeo no contiene columnas utilizables.")
        statement = select(*selected).limit(safe_limit)
        with engine.connect() as conn:
            transaction = conn.begin()
            try:
                if conn.dialect.name == "postgresql":
                    conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                return [dict(row) for row in conn.execute(statement).mappings().all()]
            finally:
                transaction.rollback()
    except (ExternalDatabaseError, SQLAlchemyError, OSError) as exc:
        raise ExternalDatabaseError(_safe_error_message(exc)) from exc
    finally:
        if engine is not None:
            engine.dispose()


def preview_mapping(connection: ExternalDatabaseConnection, mapping: ExternalDatabaseMapping) -> dict[str, Any]:
    rows = read_mapping_rows(connection, mapping, MAX_PREVIEW_ROWS)
    return {"rows": rows, "count": len(rows), "limit": MAX_PREVIEW_ROWS}


def sync_mapping(db: Session, connection: ExternalDatabaseConnection, mapping: ExternalDatabaseMapping, company_id: int, actor_id: int | None) -> dict[str, Any]:
    rows = read_mapping_rows(connection, mapping, mapping.sync_limit or 500)
    field_map = _mapping_dict(mapping)
    counts = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}
    errors: list[str] = []
    for index, row in enumerate(rows, start=1):
        try:
            payload = {target: str(row.get(target) or "").strip() for target in field_map}
            if mapping.entity_type == "customers":
                outcome = upsert_customer(db, company_id=company_id, data=payload, source="external_database", actor_id=actor_id)
            else:
                outcome = upsert_product(db, company_id=company_id, data=payload, source="external_database", actor_id=actor_id)
            counts[outcome.action] = counts.get(outcome.action, 0) + 1
        except (SQLAlchemyError, ValueError, TypeError) as exc:
            db.rollback()
            counts["errors"] += 1
            if len(errors) < 10:
                errors.append(f"Fila {index}: {_safe_error_message(exc)}")
    db.commit()
    return {"rows_read": len(rows), **counts, "errors_detail": errors}


def connection_summary(connection: ExternalDatabaseConnection) -> dict[str, Any]:
    return {
        "id": connection.id,
        "name": connection.name,
        "database_type": connection.database_type,
        "host": connection.host,
        "port": connection.port,
        "database_name": connection.database_name,
        "schema_name": connection.schema_name,
        "username": connection.username,
        "password_configured": bool(connection.password_encrypted),
        "ssl_mode": connection.ssl_mode,
        "proxy_connection_id": connection.proxy_connection_id,
        "enabled": bool(connection.enabled),
        "read_only": True,
        "status": connection.status,
        "last_test_at": connection.last_test_at.isoformat() if connection.last_test_at else None,
        "last_test_ok": connection.last_test_ok,
        "last_test_message": connection.last_test_message,
        "last_scan_at": connection.last_scan_at.isoformat() if connection.last_scan_at else None,
        "last_scan_ok": connection.last_scan_ok,
        "last_scan_message": connection.last_scan_message,
    }
