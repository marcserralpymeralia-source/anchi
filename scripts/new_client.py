#!/usr/bin/env python3
import argparse
import base64
import os
import re
import secrets
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLIENTS_DIR = ROOT / "clients"


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    if not value:
        raise ValueError("El slug no puede quedar vacio.")
    return value


def fernet_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_env(args: argparse.Namespace) -> str:
    slug = slugify(args.slug or args.name)
    admin_password = args.admin_password or secrets.token_urlsafe(18)
    app_name = args.app_name or "Anchi"
    db_name = args.database_name or f"{slug}.db"
    is_production = args.environment == "production"
    allowed_hosts = "localhost,127.0.0.1,testserver" if not is_production else ""
    cors_origins = "http://localhost:8000,http://127.0.0.1:8000,http://localhost:8001,http://127.0.0.1:8001" if not is_production else ""
    return "\n".join(
        [
            "# Generado por scripts/new_client.py",
            "# No subir este archivo al repositorio.",
            "",
            f"APP_ENV={quote(args.environment)}",
            f"APP_NAME={quote(app_name)}",
            f"APP_SLUG={quote(slug)}",
            f"SECRET_KEY={quote(secrets.token_urlsafe(48))}",
            f"ENCRYPTION_KEY={quote(fernet_key())}",
            f"SESSION_COOKIE={quote(slug + '_session')}",
            f"SESSION_COOKIE_SECURE={quote('true' if is_production else 'false')}",
            f"SESSION_COOKIE_SAMESITE={quote('lax')}",
            "SESSION_MAX_AGE=604800",
            f"ALLOWED_HOSTS={quote(allowed_hosts)}",
            f"CORS_ALLOWED_ORIGINS={quote(cors_origins if not is_production else '')}",
            f"DATABASE_URL={quote('sqlite:///./' + db_name)}",
            "MASTER_DATABASE_URL=\"sqlite:///./master.db\"",
            "",
            f"DEFAULT_COMPANY_NAME={quote(args.name)}",
            f"DEFAULT_ADMIN_EMAIL={quote(args.admin_email)}",
            f"DEFAULT_ADMIN_PASSWORD={quote(admin_password)}",
            f"ENABLE_DEMO_BOOTSTRAP={quote('false' if is_production else 'true')}",
            f"DEBUG={quote('false' if is_production else 'true')}",
            "",
            f"BRANDING_APP_NAME={quote(app_name)}",
            f"BRANDING_PRIMARY_CLAIM={quote(args.primary_claim)}",
            f"BRANDING_SECONDARY_CLAIM={quote(args.secondary_claim or '')}",
            f"BRANDING_LOGO_URL={quote(args.logo_url or '')}",
            f"BRANDING_DARK_LOGO_URL={quote(args.dark_logo_url or '')}",
            f"BRANDING_FAVICON_URL={quote(args.favicon_url or '')}",
            "",
            f"EMAIL_SIGNATURE_TEXT={quote(args.email_signature or 'Equipo de pedidos')}",
            "",
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Crea un entorno .env para un nuevo cliente.")
    parser.add_argument("--name", required=True, help="Nombre visible de la empresa cliente.")
    parser.add_argument("--slug", help="Identificador tecnico. Si se omite, se genera desde name.")
    parser.add_argument("--admin-email", required=True, help="Email del administrador inicial.")
    parser.add_argument("--admin-password", help="Password inicial. Si se omite, se genera una segura.")
    parser.add_argument("--app-name", help="Nombre visible de la app para este cliente.")
    parser.add_argument("--primary-claim", default="Gestion inteligente de pedidos")
    parser.add_argument("--secondary-claim", default="")
    parser.add_argument("--logo-url", default="")
    parser.add_argument("--dark-logo-url", default="")
    parser.add_argument("--favicon-url", default="")
    parser.add_argument("--email-signature", default="")
    parser.add_argument("--database-name", help="Nombre del archivo SQLite dentro de backend.")
    parser.add_argument("--environment", default="production", choices=["development", "production"])
    parser.add_argument("--force", action="store_true", help="Sobrescribe el .env del cliente si existe.")
    args = parser.parse_args()

    slug = slugify(args.slug or args.name)
    CLIENTS_DIR.mkdir(exist_ok=True)
    target = CLIENTS_DIR / f"{slug}.env"
    if target.exists() and not args.force:
        raise SystemExit(f"Ya existe {target}. Usa --force si quieres sobrescribirlo.")
    target.write_text(build_env(args), encoding="utf-8")
    print(f"Configuracion creada: {target}")
    print("")
    print("Siguiente paso:")
    print(f"  cp {target} backend/.env")
    print("  cd backend")
    print("  source .venv/bin/activate")
    print("  uvicorn app.main:app --host 127.0.0.1 --port 8000")


if __name__ == "__main__":
    main()
