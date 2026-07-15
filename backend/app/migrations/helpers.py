from __future__ import annotations

from hashlib import sha256

from sqlalchemy import inspect, text


def checksum_text(*parts: str) -> str:
    digest = sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def table_exists(engine, table_name: str) -> bool:  # noqa: ANN001
    return table_name in inspect(engine).get_table_names()


def existing_columns(engine, table_name: str) -> set[str]:  # noqa: ANN001
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def ensure_columns(engine, table_name: str, columns: dict[str, str], *, dry_run: bool = False) -> list[str]:  # noqa: ANN001
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return []
    current_columns = {column["name"] for column in inspector.get_columns(table_name)}
    actions: list[str] = []
    with engine.begin() as conn:
        for column_name, column_sql in columns.items():
            if column_name in current_columns:
                continue
            statement = f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}"
            actions.append(statement)
            if not dry_run:
                conn.execute(text(statement))
    return actions


def ensure_unique_index(engine, table_name: str, index_name: str, columns: tuple[str, ...], *, dry_run: bool = False) -> list[str]:  # noqa: ANN001
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return []
    existing_indexes = {index["name"] for index in inspector.get_indexes(table_name)}
    if index_name in existing_indexes:
        return []
    statement = f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} ON {table_name} ({', '.join(columns)})"
    if not dry_run:
        with engine.begin() as conn:
            conn.execute(text(statement))
    return [statement]
