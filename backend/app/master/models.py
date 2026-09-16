from datetime import datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.core.encryption import decrypt_secret, encrypt_secret
from app.master.database import MasterBase


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EncryptedDatabaseURL(TypeDecorator):
    """Encrypt tenant database URLs at rest while accepting legacy plaintext."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001
        if not value or str(value).startswith("enc:"):
            return value
        encrypted = encrypt_secret(str(value))
        return f"enc:{encrypted}" if encrypted else value

    def process_result_value(self, value, dialect):  # noqa: ANN001
        if not value or not str(value).startswith("enc:"):
            return value
        return decrypt_secret(str(value)[4:])


class MasterCompany(MasterBase):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    legal_name: Mapped[str | None] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    provisioning_status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    suspended_reason: Mapped[str | None] = mapped_column(Text)
    default_language: Mapped[str] = mapped_column(String(20), default="es")
    default_timezone: Mapped[str] = mapped_column(String(80), default="Europe/Madrid")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    memberships: Mapped[list["CompanyMembership"]] = relationship(back_populates="company")
    tenant_databases: Mapped[list["MasterTenantDatabase"]] = relationship(back_populates="company")


class MasterUser(MasterBase):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    platform_role_key: Mapped[str | None] = mapped_column(String(40), index=True)
    email_normalized: Mapped[str | None] = mapped_column(String(255), index=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    password_version: Mapped[int] = mapped_column(Integer, default=1)
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    memberships: Mapped[list["CompanyMembership"]] = relationship(back_populates="user")


class CompanyMembership(MasterBase):
    __tablename__ = "memberships"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    role_key: Mapped[str] = mapped_column(String(80), default="Administrador")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[MasterUser] = relationship(back_populates="memberships")
    company: Mapped[MasterCompany] = relationship(back_populates="memberships")

    __table_args__ = (UniqueConstraint("user_id", "company_id"),)


class MasterTenantDatabase(MasterBase):
    __tablename__ = "tenant_databases"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    database_key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    database_url: Mapped[str] = mapped_column(EncryptedDatabaseURL())
    database_type: Mapped[str] = mapped_column(String(30), default="sqlite")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    provisioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    health_status: Mapped[str] = mapped_column(String(30), default="unknown")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    company: Mapped[MasterCompany] = relationship(back_populates="tenant_databases")


class MasterWhatsAppEndpoint(MasterBase):
    """Control-plane index for routing Meta webhooks to one tenant."""

    __tablename__ = "whatsapp_endpoints"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    phone_number_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    business_account_id: Mapped[str | None] = mapped_column(String(80), index=True)
    display_phone_number: Mapped[str | None] = mapped_column(String(80))
    verified_name: Mapped[str | None] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    company: Mapped[MasterCompany] = relationship()


class EmailSyncState(MasterBase):
    __tablename__ = "email_sync_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    channel_key: Mapped[str] = mapped_column(String(80), default="email", index=True)
    mailbox: Mapped[str | None] = mapped_column(String(255))
    uidvalidity: Mapped[str | None] = mapped_column(String(120))
    source_provider: Mapped[str | None] = mapped_column(String(50))
    source_host: Mapped[str | None] = mapped_column(String(255))
    source_username: Mapped[str | None] = mapped_column(String(255))
    source_connected_email: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    frequency_seconds: Mapped[int] = mapped_column(Integer, default=60)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_type: Mapped[str | None] = mapped_column(String(120))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(50), default="idle")
    sync_status: Mapped[str] = mapped_column(String(50), default="idle")
    listener_status: Mapped[str] = mapped_column(String(50), default="inactive")
    listener_owner: Mapped[str | None] = mapped_column(String(120))
    listener_last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    listener_last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    listener_last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    listener_last_error_message: Mapped[str | None] = mapped_column(Text)
    lock_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    lock_owner: Mapped[str | None] = mapped_column(String(120))
    last_seen_uid: Mapped[str | None] = mapped_column(String(120))
    last_checkpoint_uid: Mapped[str | None] = mapped_column(String(120))
    backfill_status: Mapped[str] = mapped_column(String(50), default="idle")
    backfill_total: Mapped[int] = mapped_column(Integer, default=0)
    backfill_processed: Mapped[int] = mapped_column(Integer, default=0)
    backfill_created: Mapped[int] = mapped_column(Integer, default=0)
    backfill_duplicates: Mapped[int] = mapped_column(Integer, default=0)
    backfill_errors: Mapped[int] = mapped_column(Integer, default=0)
    backfill_last_uid: Mapped[str | None] = mapped_column(String(120))
    backfill_checkpoint_json: Mapped[str | None] = mapped_column(Text)
    backfill_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backfill_last_checkpoint_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backfill_paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backfill_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backfill_cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    company: Mapped[MasterCompany] = relationship()

    __table_args__ = (UniqueConstraint("company_id", "channel_key"),)


class MasterSchemaMigration(MasterBase):
    __tablename__ = "schema_migrations"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(80), default="0")
    name: Mapped[str] = mapped_column(String(180), default="unregistered")
    checksum: Mapped[str | None] = mapped_column(String(120))
    execution_ms: Mapped[int] = mapped_column(Integer, default=0)
    application_version: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(30), default="missing")
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserInvitation(MasterBase):
    """One-time invitation metadata; token material is stored hashed by the service."""

    __tablename__ = "user_invitations"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    role_key: Mapped[str] = mapped_column(String(80), default="Operador")
    token_hash: Mapped[str] = mapped_column(String(255), unique=True)
    invited_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PasswordResetToken(MasterBase):
    """One-time password reset token; only its digest is persisted."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MasterUserSession(MasterBase):
    """Server-side revocation record for a browser session.

    The browser only receives the random identifier. The remaining context
    is persisted here so the master plane can revoke sessions instantly and
    enforce expiry/version changes across replicas without exposing tenant
    identifiers in a signed cookie.
    """

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), index=True)
    membership_id: Mapped[int | None] = mapped_column(ForeignKey("memberships.id"), index=True)
    session_data_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    user_agent: Mapped[str | None] = mapped_column(String(500))
    ip_address: Mapped[str | None] = mapped_column(String(80))


class MasterRateLimitBucket(MasterBase):
    """Persistent throttling bucket shared by all web replicas."""

    __tablename__ = "rate_limit_buckets"

    id: Mapped[int] = mapped_column(primary_key=True)
    bucket_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlatformAuditLog(MasterBase):
    """Metadata-only audit trail for control-plane actions."""

    __tablename__ = "platform_audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), index=True)
    action: Mapped[str] = mapped_column(String(120), index=True)
    target_type: Mapped[str | None] = mapped_column(String(80))
    target_id: Mapped[str | None] = mapped_column(String(120), index=True)
    outcome: Mapped[str] = mapped_column(String(30), default="success")
    metadata_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class TenantProvisioningRun(MasterBase):
    """Lifecycle state for company provisioning and recovery operations."""

    __tablename__ = "tenant_provisioning_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    operation: Mapped[str] = mapped_column(String(40), default="create")
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    requested_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    current_step: Mapped[str | None] = mapped_column(String(80))
    correlation_id: Mapped[str | None] = mapped_column(String(120), index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TenantUsageDaily(MasterBase):
    """Pre-aggregated usage counters for the platform dashboard."""

    __tablename__ = "tenant_usage_daily"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    usage_date: Mapped[datetime] = mapped_column(Date, index=True)
    orders_total: Mapped[int] = mapped_column(Integer, default=0)
    messages_total: Mapped[int] = mapped_column(Integer, default=0)
    email_messages_total: Mapped[int] = mapped_column(Integer, default=0)
    whatsapp_messages_total: Mapped[int] = mapped_column(Integer, default=0)
    active_users_total: Mapped[int] = mapped_column(Integer, default=0)
    storage_bytes: Mapped[int] = mapped_column(Integer, default=0)
    requests_total: Mapped[int] = mapped_column(Integer, default=0)
    avg_request_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("company_id", "usage_date"),)


class TenantHealthSnapshot(MasterBase):
    """Latest point-in-time health and usage sample for one tenant."""

    __tablename__ = "tenant_health_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="unknown", index=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    schema_version: Mapped[str | None] = mapped_column(String(80))
    orders_total: Mapped[int] = mapped_column(Integer, default=0)
    messages_total: Mapped[int] = mapped_column(Integer, default=0)
    email_messages_total: Mapped[int] = mapped_column(Integer, default=0)
    whatsapp_messages_total: Mapped[int] = mapped_column(Integer, default=0)
    active_users_total: Mapped[int] = mapped_column(Integer, default=0)
    pending_jobs_total: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(120))
    error_message: Mapped[str | None] = mapped_column(Text)

    company: Mapped[MasterCompany] = relationship()
