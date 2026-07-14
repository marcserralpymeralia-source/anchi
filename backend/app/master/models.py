from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.master.database import MasterBase


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MasterCompany(MasterBase):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    legal_name: Mapped[str | None] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
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
    database_url: Mapped[str] = mapped_column(Text)
    database_type: Mapped[str] = mapped_column(String(30), default="sqlite")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    provisioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    health_status: Mapped[str] = mapped_column(String(30), default="unknown")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    company: Mapped[MasterCompany] = relationship(back_populates="tenant_databases")


class EmailSyncState(MasterBase):
    __tablename__ = "email_sync_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    channel_key: Mapped[str] = mapped_column(String(80), default="email", index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    frequency_seconds: Mapped[int] = mapped_column(Integer, default=60)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(50), default="idle")
    lock_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    lock_owner: Mapped[str | None] = mapped_column(String(120))
    last_seen_uid: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    company: Mapped[MasterCompany] = relationship()

    __table_args__ = (UniqueConstraint("company_id", "channel_key"),)
