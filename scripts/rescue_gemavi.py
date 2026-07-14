#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

try:
    from app.core.security import hash_password
except Exception:  # pragma: no cover
    DEFAULT_HASH = "$pbkdf2-sha256$29000$G4PQ.r93bg2htHYuxbhXig$2hylimy/uOuC3HExciv6ChEpRDZwy/Oixg2B5x83AYA"

    def hash_password(password: str) -> str:
        if password == "GemaviDemo2026!":
            return DEFAULT_HASH
        raise RuntimeError("No se pudo importar app.core.security.hash_password.")


ALIASES = {"gemavi", "gemavi demo", "gemavi-demo", "gemavi_demo"}
ADMIN_EMAIL = "admin@gemavi.local"
ADMIN_PASSWORD = "GemaviDemo2026!"
ADMIN_ROLE = "Administrador"


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def count_rows(conn: sqlite3.Connection, table: str, company_id: int | None = None) -> int:
    if not table_exists(conn, table):
        return 0
    if company_id is None:
        return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
    if "company_id" not in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}:  # pragma: no cover
        return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE company_id=?", (company_id,)).fetchone()["n"])


def find_company(conn: sqlite3.Connection) -> sqlite3.Row | None:
    placeholders = ",".join("?" for _ in ALIASES)
    return conn.execute(
        f"""
        SELECT *
        FROM companies
        WHERE lower(name) IN ({placeholders})
           OR lower(coalesce(legal_name, '')) IN ({placeholders})
        ORDER BY id
        LIMIT 1
        """,
        tuple(ALIASES) + tuple(ALIASES),
    ).fetchone()


def get_or_create_company(conn: sqlite3.Connection) -> sqlite3.Row:
    company = find_company(conn)
    if company:
        conn.execute(
            """
            UPDATE companies
               SET active = 1,
                   legal_name = COALESCE(NULLIF(legal_name, ''), name),
                   email = COALESCE(NULLIF(email, ''), ?)
             WHERE id = ?
            """,
            (ADMIN_EMAIL, company["id"]),
        )
        return conn.execute("SELECT * FROM companies WHERE id = ?", (company["id"],)).fetchone()

    conn.execute(
        """
        INSERT INTO companies (name, legal_name, active, plan, email, currency, language, default_language, timezone, date_format, decimal_separator)
        VALUES (?, ?, 1, 'demo', ?, 'EUR', 'es', 'es', 'Europe/Madrid', '%d/%m/%Y', ',')
        """,
        ("GEMAVI", "GEMAVI", ADMIN_EMAIL),
    )
    return conn.execute("SELECT * FROM companies WHERE id = last_insert_rowid()").fetchone()


def get_or_create_admin_role(conn: sqlite3.Connection, company_id: int) -> sqlite3.Row:
    role = conn.execute(
        "SELECT * FROM roles WHERE company_id = ? AND name = ?",
        (company_id, ADMIN_ROLE),
    ).fetchone()
    if role:
        return role
    conn.execute(
        "INSERT INTO roles (company_id, name, permissions) VALUES (?, ?, ?)",
        (company_id, ADMIN_ROLE, ""),
    )
    return conn.execute("SELECT * FROM roles WHERE id = last_insert_rowid()").fetchone()


def ensure_admin_user(conn: sqlite3.Connection, company_id: int, role_id: int, password: str) -> None:
    password_hash = hash_password(password)
    user = conn.execute("SELECT * FROM users WHERE lower(email) = lower(?)", (ADMIN_EMAIL,)).fetchone()
    if user:
        conn.execute(
            """
            UPDATE users
               SET company_id = ?,
                   role_id = ?,
                   name = COALESCE(NULLIF(name, ''), 'Administrador'),
                   password_hash = ?,
                   is_active = 1
             WHERE id = ?
            """,
            (company_id, role_id, password_hash, user["id"]),
        )
        return
    conn.execute(
        "INSERT INTO users (company_id, role_id, email, name, password_hash, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (company_id, role_id, ADMIN_EMAIL, "Administrador", password_hash),
    )


def ensure_branding(conn: sqlite3.Connection, company_id: int) -> None:
    branding = conn.execute("SELECT * FROM branding_settings WHERE company_id = ?", (company_id,)).fetchone()
    if branding:
        conn.execute(
            """
            UPDATE branding_settings
               SET company_name = COALESCE(NULLIF(company_name, ''), 'GEMAVI'),
                   app_name = COALESCE(NULLIF(app_name, ''), 'Agente de Pedidos')
             WHERE id = ?
            """,
            (branding["id"],),
        )
        return
    conn.execute(
        """
        INSERT INTO branding_settings (company_id, app_name, company_name, primary_claim, secondary_claim, short_description, show_logo_sidebar, show_app_name_sidebar, show_claim_sidebar, show_claim_login, theme_json, microcopy_json)
        VALUES (?, 'Agente de Pedidos', 'GEMAVI', 'Gestion inteligente de pedidos', 'eco food packaging', 'Aplicacion para la revision, validacion y exportacion de pedidos recibidos por correo electronico.', 1, 1, 1, 1, '{}', '{}')
        """,
        (company_id,),
    )


def summary(conn: sqlite3.Connection, company_id: int) -> dict[str, int | bool]:
    email_settings = conn.execute("SELECT * FROM email_settings WHERE company_id = ?", (company_id,)).fetchone()
    llm_settings = conn.execute("SELECT * FROM llm_settings WHERE company_id = ?", (company_id,)).fetchone()
    return {
        "company_id": company_id,
        "users": count_rows(conn, "users", company_id),
        "products": count_rows(conn, "products", company_id),
        "customers": count_rows(conn, "customers", company_id),
        "orders": count_rows(conn, "orders", company_id),
        "imports": count_rows(conn, "imports", company_id),
        "inbound_messages": count_rows(conn, "inbound_messages", company_id),
        "email_config": bool(email_settings and (email_settings["imap_host"] or email_settings["connected_email"])),
        "openai_config": bool(llm_settings and llm_settings["api_key_encrypted"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Rescata o repara la organizacion Gemavi.")
    parser.add_argument("--db", default=str(ROOT / "backend" / "gemavi.db"), help="Ruta a la base SQLite de Gemavi.")
    parser.add_argument("--password", default=ADMIN_PASSWORD, help="Password temporal para admin@gemavi.local.")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        raise SystemExit(f"No existe la base de datos: {db_path}")
    admin_password = args.password

    conn = connect(db_path)
    try:
        company = get_or_create_company(conn)
        role = get_or_create_admin_role(conn, company["id"])
        ensure_admin_user(conn, company["id"], role["id"], admin_password)
        ensure_branding(conn, company["id"])
        conn.commit()

        report = summary(conn, company["id"])
        print("Gemavi encontrada:")
        print(f"company_id: {report['company_id']}")
        print(f"usuarios: {report['users']}")
        print(f"productos: {report['products']}")
        print(f"clientes: {report['customers']}")
        print(f"imap_config: {'si' if report['email_config'] else 'no'}")
        print(f"openai_config: {'si' if report['openai_config'] else 'no'}")
        print(f"pedidos: {report['orders']}")
        print(f"importaciones: {report['imports']}")
        print(f"inbound_messages: {report['inbound_messages']}")
        print()
        print(f"Usuario: {ADMIN_EMAIL}")
        print(f"Contraseña: {admin_password}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
