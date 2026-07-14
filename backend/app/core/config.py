import base64
import hashlib
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


DEV_SECRET_KEY = base64.urlsafe_b64encode(hashlib.sha256(b"order-agent-dev-secret-key").digest()).decode()


class Settings(BaseSettings):
    app_name: str = "Anchi"
    app_slug: str = "anchi"
    database_url: str = "sqlite:///./anchi_demo.db"
    master_database_url: str = "sqlite:///./master.db"
    app_secret_key: str = DEV_SECRET_KEY
    tenant_db_encryption_key: str = ""
    auth_secret: str = DEV_SECRET_KEY
    cron_secret: str = ""
    enable_legacy_sync: bool = False
    branding_cache_ttl_seconds: int = 30
    email_worker_poll_seconds: int = 15
    job_worker_poll_seconds: int = 10
    app_url: str = "http://127.0.0.1:8000"
    session_cookie: str = "anchi_session"
    environment: str = "development"
    default_company_name: str = "Anchi Demo"
    default_admin_email: str = "admin@anchi.local"
    default_admin_password: str = "admin123"
    seed_demo_data: bool = True
    branding_app_name: str = "Anchi"
    branding_primary_claim: str = "Gestion inteligente de pedidos"
    branding_secondary_claim: str = ""
    branding_logo_url: str = ""
    branding_dark_logo_url: str = ""
    branding_favicon_url: str = ""
    email_signature_text: str = "Equipo de pedidos"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
