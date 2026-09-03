from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import cycle
from pathlib import Path
from typing import Iterator

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from unittest.mock import patch

from app.core.config import get_settings
from app.core.encryption import encrypt_secret
from app.core.security import hash_password
from app.db.database import Base
from app.db.models import (
    Alert,
    AuditLog,
    BackgroundJob,
    Company,
    Customer,
    CustomerAlias,
    CustomerContactPoint,
    CustomerDomain,
    Email,
    EmailAttachment,
    EmailSettings,
    ImportJob,
    InputChannel,
    JobAttempt,
    LLMSettings,
    Order,
    OrderLine,
    Product,
    ProductAlias,
    Role,
    User,
    utcnow,
)
from app.demo_seed import _ensure_order_bundle, _now, reset_demo_company
from app.master.database import MasterBase
from app.master.migrations import upgrade_master_schema
from app.master.models import CompanyMembership, EmailSyncState, MasterCompany, MasterTenantDatabase, MasterUser
from app.master.service import slugify
from app.migrations.helpers import ensure_columns
from app.tenancy.database import ensure_tenant_schema, get_tenant_engine


@dataclass(slots=True)
class ScenarioPlan:
    name: str
    extra_customers: int
    extra_products: int
    extra_orders: int
    extra_jobs: int
    extra_alerts: int
    extra_logs: int
    extra_imports: int


@dataclass(slots=True)
class PerformanceFixture:
    scenario: str
    tempdir: tempfile.TemporaryDirectory[str]
    master_path: Path
    tenant_path: Path
    master_database_url: str
    tenant_database_url: str
    company_id: int
    company_slug: str
    admin_email: str
    admin_password: str
    order_ids: list[int]
    counts: dict[str, int]

    def cleanup(self) -> None:
        # A fixture can be used without temporary_performance_environment (for
        # example by tests that only inspect its database). Dispose the cached
        # tenant engine here as well, before removing the SQLite files. This
        # keeps cleanup deterministic and prevents sqlite3 ResourceWarnings.
        try:
            from app.tenancy.database import clear_tenant_engine_cache, clear_tenant_schema_cache

            clear_tenant_engine_cache()
            clear_tenant_schema_cache()
        except (ImportError, OSError):
            pass
        try:
            self.tempdir.cleanup()
        except (PermissionError, OSError):
            pass
        finally:
            import gc

            gc.collect()


SCENARIO_PLANS: dict[str, ScenarioPlan] = {
    "small": ScenarioPlan("small", extra_customers=0, extra_products=0, extra_orders=0, extra_jobs=0, extra_alerts=0, extra_logs=0, extra_imports=0),
    "medium": ScenarioPlan("medium", extra_customers=140, extra_products=360, extra_orders=120, extra_jobs=45, extra_alerts=30, extra_logs=90, extra_imports=20),
    "large": ScenarioPlan("large", extra_customers=360, extra_products=960, extra_orders=280, extra_jobs=90, extra_alerts=60, extra_logs=180, extra_imports=50),
}


def _connect_args(database_url: str) -> dict[str, object]:
    return {"check_same_thread": False} if database_url.startswith("sqlite") else {}


def _session_factory(database_url: str) -> sessionmaker[Session]:
    engine = create_engine(database_url, connect_args=_connect_args(database_url))
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _setup_master(master_db: Session, tenant_database_url: str) -> tuple[MasterCompany, MasterUser]:
    company = MasterCompany(id=1, name="Anchi Demo", slug="demo", legal_name="Anchi Demo", active=True, default_language="es", default_timezone="Europe/Madrid")
    user = MasterUser(id=1, email="admin@anchi.local", full_name="Administrador demo", password_hash=hash_password("admin123"), is_active=True)
    membership = CompanyMembership(id=1, user_id=1, company_id=1, role_key="Superadmin", is_active=True, is_owner=True)
    tenant_db = MasterTenantDatabase(company_id=1, database_key="demo", database_url=tenant_database_url, database_type="sqlite", is_active=True, health_status="ok", provisioned_at=utcnow())
    sync_state = EmailSyncState(company_id=1, channel_key="email", enabled=True, frequency_seconds=60, status="idle")
    master_db.add_all([company, user, membership, tenant_db, sync_state])
    master_db.commit()
    return company, user


def _seed_extra_customers(db: Session, company_id: int, count: int) -> None:
    for index in range(1, count + 1):
        code = f"PX{index:05d}"
        customer = Customer(
            company_id=company_id,
            code=code,
            fiscal_name=f"Cliente Rendimiento {index:05d} SL",
            commercial_name=f"Rendimiento {index:05d}",
            primary_email=f"rendimiento-{index:05d}@example.com",
            delegation="Demo",
            phone=f"900{index:06d}"[-9:],
            city="Madrid" if index % 3 else "Barcelona",
            province="Madrid" if index % 3 else "Barcelona",
            assigned_salesperson=f"Comercial {index % 8 + 1}",
            accounting_code=f"43{index:05d}",
            category="Demo",
            notes="Cliente sintético para medición de rendimiento.",
        )
        db.add(customer)
        if index % 10 == 0:
            db.flush()
            db.add(CustomerAlias(company_id=company_id, customer_id=customer.id, alias=f"Rendimiento {index:05d}"))
            db.add(CustomerDomain(company_id=company_id, customer_id=customer.id, domain=f"rendimiento-{index:05d}.example"))
            db.add(
                CustomerContactPoint(
                    company_id=company_id,
                    customer_id=customer.id,
                    type="email",
                    value=f"rendimiento-{index:05d}@example.com",
                    label="principal",
                    contact_name="Compras",
                    contact_role="Compras",
                    is_primary=True,
                    confidence=0.9,
                    source="synthetic",
                    first_seen_at=_now(index % 7),
                    last_seen_at=_now(0),
                )
            )
    db.flush()


def _seed_extra_products(db: Session, company_id: int, count: int) -> None:
    for index in range(1, count + 1):
        product = Product(
            company_id=company_id,
            reference=f"PRD-{index:05d}",
            alternative_code=f"ALT-{index:05d}" if index % 5 == 0 else None,
            name=f"Producto Rendimiento {index:05d}",
            description=f"Producto sintético {index:05d} para baseline.",
            brand="Anchi",
            usual_supplier="Proveedor Demo",
            family="Demo" if index % 2 else "Envases",
            subfamily="General" if index % 3 else "Especial",
            format="STD",
            sale_unit="unidades",
            sale_price=round(5.0 + (index % 50) * 0.25, 2),
            discount_percent=float(index % 10),
            size_group="STD",
            colors="Mixto",
            entry_date="2026-07-01",
            obsolete=False,
            article_type="Venta",
            description_cont=f"Descripcion extendida del producto sintético {index:05d}.",
            warehouse_location_code=f"RACK-{index % 12:02d}",
            replenishment_warehouse="WH-1",
            ean=f"84{index:011d}"[:13],
            status="active",
        )
        db.add(product)
        if index % 12 == 0:
            db.flush()
            db.add(ProductAlias(company_id=company_id, product_id=product.id, alias=f"Producto Rendimiento {index:05d}"))
    db.flush()


def _seed_extra_imports(db: Session, company_id: int, user_id: int, count: int) -> None:
    for index in range(1, count + 1):
        db.add(
            ImportJob(
                company_id=company_id,
                entity_type="customers" if index % 2 else "products",
                filename=f"perf_import_{index:05d}.csv",
                status="completed",
                rows_total=100 + index,
                rows_created=95 + index,
                rows_updated=index % 13,
                rows_ignored=index % 7,
                errors=None,
                mapping_used='{"baseline":true}',
                user_id=user_id,
            )
        )


def _seed_extra_jobs(db: Session, company_id: int, user_id: int, count: int) -> None:
    statuses = ("queued", "running", "retrying", "success", "failed")
    job_types = ("process_email", "import_file", "export_order", "bulk_order_action")
    for index in range(1, count + 1):
        status = statuses[index % len(statuses)]
        job = BackgroundJob(
            company_id=company_id,
            job_type=job_types[index % len(job_types)],
            dedupe_key=f"perf-{index:05d}",
            status=status,
            payload_json=f'{{"scenario":"performance","index":{index}}}',
            result_json='{"ok":true}' if status == "success" else None,
            error_message="Simulated failure for baseline" if status == "failed" else None,
            created_by_user_id=user_id,
            progress=(index * 7) % 100,
            attempt_count=1 if status != "queued" else 0,
            retry_count=1 if status in {"retrying", "failed"} else 0,
            max_retries=3,
            lock_owner=f"worker-{index % 3}" if status in {"running", "retrying"} else None,
            lock_until=utcnow() if status in {"running", "retrying"} else None,
            next_retry_at=utcnow() if status == "retrying" else None,
            last_error_at=utcnow() if status == "failed" else None,
            last_error_type="RuntimeError" if status == "failed" else None,
            last_heartbeat_at=utcnow() if status in {"running", "retrying"} else None,
        )
        db.add(job)
        if status in {"running", "retrying", "failed"}:
            db.flush()
            db.add(
                JobAttempt(
                    company_id=company_id,
                    job_id=job.id,
                    attempt_number=1,
                    worker_id=job.lock_owner or f"worker-{index % 3}",
                    status="running" if status == "running" else "failed_permanent",
                    started_at=utcnow(),
                    finished_at=utcnow() if status != "running" else None,
                    duration_seconds=5 + index % 11,
                    error_type="RuntimeError" if status == "failed" else None,
                    error_message="Simulated failure for baseline" if status == "failed" else None,
                    next_retry_at=utcnow() if status == "retrying" else None,
                )
            )


def _seed_extra_alerts(db: Session, company_id: int, count: int) -> None:
    severities = ("low", "medium", "high")
    for index in range(1, count + 1):
        db.add(
            Alert(
                company_id=company_id,
                alert_type="performance_review",
                severity=severities[index % len(severities)],
                status="open",
                title=f"Alerta sintética {index:05d}",
                message="Alerta sintética para medir el peso del listado operativo.",
                payload_json='{"baseline":true}',
            )
        )


def _seed_extra_logs(db: Session, company_id: int, user_id: int, count: int) -> None:
    for index in range(1, count + 1):
        db.add(
            AuditLog(
                company_id=company_id,
                user_id=user_id,
                action="performance.seed",
                entity_type="baseline",
                entity_id=index,
                message=f"{{\"message\":\"Evento sintético {index:05d}\",\"context\":{{\"request_id\":\"perf-{index:05d}\"}},\"metadata\":{{\"scenario\":\"baseline\"}}}}",
            )
        )


def _seed_extra_orders(db: Session, company_id: int, admin_email: str, count: int) -> list[int]:
    orders = db.scalars(select(Order).where(Order.company_id == company_id).order_by(Order.id)).all()
    customers = db.scalars(select(Customer).where(Customer.company_id == company_id).order_by(Customer.id)).all()
    products = db.scalars(select(Product).where(Product.company_id == company_id).order_by(Product.id)).all()
    channels = db.scalars(select(InputChannel).where(InputChannel.company_id == company_id, InputChannel.key == "email")).all()
    channel = channels[0] if channels else db.scalar(select(InputChannel).where(InputChannel.company_id == company_id).order_by(InputChannel.id))
    if not channel:
        raise RuntimeError("No se pudo localizar el canal email de la base de rendimiento")

    statuses = ("pedido_pendiente_revision", "dudoso", "pedido_confirmado", "pedido_exportado", "no_importable")
    scores = (94, 78, 66, 88, 44)
    order_ids: list[int] = [order.id for order in orders]
    customer_cycle = cycle(customers)
    product_cycle = cycle(products)
    for index in range(1, count + 1):
        customer = next(customer_cycle)
        product_a = next(product_cycle)
        product_b = next(product_cycle)
        status = statuses[index % len(statuses)]
        score = scores[index % len(scores)]
        has_pdf = index % 3 == 0
        order = _ensure_order_bundle(
            db,
            company_id=company_id,
            channel=channel,
            customer=customer,
            detected_name=customer.commercial_name or customer.fiscal_name,
            sender=customer.primary_email or f"{slugify(customer.fiscal_name)}@example.com",
            subject=f"Pedido rendimiento {index:05d}",
            body="Pedido sintético generado para medición objetiva.",
            email_external_id=f"perf-order-{index:05d}",
            order_status=status,
            score=score,
            created_days_ago=index % 11,
            lines=[
                {
                    "reference": product_a.reference,
                    "name": product_a.name,
                    "product_id": product_a.id,
                    "quantity": 10 + (index % 25),
                    "unit": product_a.sale_unit or "unidades",
                    "original_text": f"{10 + (index % 25)} {product_a.name}",
                    "confidence": 0.9,
                },
                {
                    "reference": product_b.reference if index % 4 else None,
                    "name": product_b.name if index % 4 else "Linea sintetica sin referencia",
                    "product_id": product_b.id if index % 4 else None,
                    "quantity": 1 + (index % 3),
                    "unit": product_b.sale_unit or "unidades",
                    "original_text": f"{1 + (index % 3)} {product_b.name}",
                    "confidence": 0.72 if index % 4 else 0.52,
                    "doubt_reason": None if index % 4 else "Referencia incompleta",
                },
            ],
            has_pdf=has_pdf,
            pdf_text=f"{customer.fiscal_name} solicita {product_a.name} y {product_b.name}.",
            order_note="Pedido sintético de baseline de rendimiento.",
            export_filename=f"PERF_{index:05d}.csv" if status == "pedido_exportado" else None,
        )
        order_ids.append(order.id)
    return order_ids


def build_performance_fixture(scenario: str, base_dir: Path | None = None) -> PerformanceFixture:
    if scenario not in SCENARIO_PLANS:
        raise ValueError(f"Escenario no soportado: {scenario}")
    plan = SCENARIO_PLANS[scenario]
    tempdir = tempfile.TemporaryDirectory()
    root = Path(base_dir) if base_dir is not None else Path(tempdir.name)
    root.mkdir(parents=True, exist_ok=True)
    master_path = root / "master.sqlite"
    tenant_path = root / "tenant.sqlite"
    master_database_url = f"sqlite:///{master_path.as_posix()}"
    tenant_database_url = f"sqlite:///{tenant_path.as_posix()}"

    master_engine = create_engine(master_database_url, connect_args=_connect_args(master_database_url))
    tenant_engine = create_engine(tenant_database_url, connect_args=_connect_args(tenant_database_url))
    schema_engine = None
    MasterBase.metadata.create_all(master_engine)
    Base.metadata.create_all(tenant_engine)
    schema_engine = get_tenant_engine(tenant_database_url)
    ensure_tenant_schema(tenant_database_url)

    MasterSession = sessionmaker(bind=master_engine, autoflush=False, autocommit=False)
    TenantSession = sessionmaker(bind=tenant_engine, autoflush=False, autocommit=False)

    master_db = MasterSession()
    tenant_db = TenantSession()
    try:
        company, user = _setup_master(master_db, tenant_database_url)
        reset_demo_company(tenant_db)

        company_row = tenant_db.get(Company, company.id)
        company_row.country = "España"

        llm = tenant_db.scalar(
            select(LLMSettings).where(LLMSettings.company_id == company.id)
        )
        llm.provider = "openai"

        with patch.dict(
            os.environ,
            {"ENCRYPTION_KEY": "CKHCB4gFGn7kJVxowWH2pEdPucfPaZugSsMgoJU6eNE="},
        ):
            get_settings.cache_clear()
            llm.api_key_encrypted = encrypt_secret("performance-test-key")

        get_settings.cache_clear()
        _seed_extra_customers(tenant_db, company.id, plan.extra_customers)
        _seed_extra_products(tenant_db, company.id, plan.extra_products)
        _seed_extra_imports(tenant_db, company.id, user.id, plan.extra_imports)
        _seed_extra_jobs(tenant_db, company.id, user.id, plan.extra_jobs)
        _seed_extra_alerts(tenant_db, company.id, plan.extra_alerts)
        _seed_extra_logs(tenant_db, company.id, user.id, plan.extra_logs)
        order_ids = _seed_extra_orders(tenant_db, company.id, user.email, plan.extra_orders)
        tenant_db.commit()

        counts = {
            "customers": tenant_db.scalar(select(func.count()).select_from(Customer).where(Customer.company_id == company.id)) or 0,
            "products": tenant_db.scalar(select(func.count()).select_from(Product).where(Product.company_id == company.id)) or 0,
            "orders": tenant_db.scalar(select(func.count()).select_from(Order).where(Order.company_id == company.id)) or 0,
            "jobs": tenant_db.scalar(select(func.count()).select_from(BackgroundJob).where(BackgroundJob.company_id == company.id)) or 0,
            "alerts": tenant_db.scalar(select(func.count()).select_from(Alert).where(Alert.company_id == company.id)) or 0,
            "imports": tenant_db.scalar(select(func.count()).select_from(ImportJob).where(ImportJob.company_id == company.id)) or 0,
            "logs": tenant_db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.company_id == company.id)) or 0,
            "emails": tenant_db.scalar(select(func.count()).select_from(Email).where(Email.company_id == company.id)) or 0,
            "attachments": tenant_db.scalar(select(func.count()).select_from(EmailAttachment).where(EmailAttachment.company_id == company.id)) or 0,
        }
    finally:
        tenant_db.close()
        master_db.close()
        master_engine.dispose()
        tenant_engine.dispose()
        if schema_engine is not None:
            schema_engine.dispose()
        get_tenant_engine.cache_clear()

    return PerformanceFixture(
        scenario=plan.name,
        tempdir=tempdir,
        master_path=master_path,
        tenant_path=tenant_path,
        master_database_url=master_database_url,
        tenant_database_url=tenant_database_url,
        company_id=1,
        company_slug="demo",
        admin_email="admin@anchi.local",
        admin_password="admin123",
        order_ids=order_ids,
        counts=counts,
    )


@contextmanager
def temporary_performance_environment(fixture: PerformanceFixture) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in (
        "APP_ENV",
        "MASTER_DATABASE_URL",
        "DATABASE_URL",
        "SECRET_KEY",
        "ENCRYPTION_KEY",
        "DEFAULT_COMPANY_NAME",
        "DEFAULT_ADMIN_EMAIL",
        "DEFAULT_ADMIN_PASSWORD",
        "PERFORMANCE_PROFILING_ENABLED",
        "ENABLE_PERFORMANCE_PROFILING",
        "ENABLE_DEMO_BOOTSTRAP",
    )}
    overrides = {
        "APP_ENV": "test",
        "MASTER_DATABASE_URL": fixture.master_database_url,
        "DATABASE_URL": fixture.tenant_database_url,
        "SECRET_KEY": "performance-baseline-secret-key-000000000000",
        "ENCRYPTION_KEY": "CKHCB4gFGn7kJVxowWH2pEdPucfPaZugSsMgoJU6eNE=",
        "DEFAULT_COMPANY_NAME": "Anchi Demo",
        "DEFAULT_ADMIN_EMAIL": fixture.admin_email,
        "DEFAULT_ADMIN_PASSWORD": fixture.admin_password,
        "PERFORMANCE_PROFILING_ENABLED": "true",
        "ENABLE_PERFORMANCE_PROFILING": "true",
        "ENABLE_DEMO_BOOTSTRAP": "false",
    }
    os.environ.update(overrides)
    get_settings.cache_clear()
    import importlib

    master_database_module = importlib.import_module("app.master.database")
    operational_database_module = importlib.import_module("app.db.database")
    lifespan_module = importlib.import_module("app.core.lifespan")
    middleware_module = importlib.import_module("app.core.middleware")
    tenancy_database_module = importlib.import_module("app.tenancy.database")
    jobs_worker_module = importlib.import_module("app.workers.jobs_worker")
    previous_master_engine = master_database_module.engine
    previous_master_sessionlocal = master_database_module.MasterSessionLocal
    previous_operational_engine = operational_database_module.engine
    previous_operational_sessionlocal = operational_database_module.SessionLocal
    previous_lifespan_master_sessionlocal = lifespan_module.MasterSessionLocal
    previous_middleware_master_sessionlocal = middleware_module.MasterSessionLocal
    previous_jobs_worker_master_sessionlocal = jobs_worker_module.MasterSessionLocal

    master_engine = create_engine(fixture.master_database_url, connect_args=_connect_args(fixture.master_database_url))
    tenant_engine = create_engine(fixture.tenant_database_url, connect_args=_connect_args(fixture.tenant_database_url))
    master_database_module.engine = master_engine
    master_database_module.MasterSessionLocal = sessionmaker(bind=master_engine, autoflush=False, autocommit=False)
    lifespan_module.MasterSessionLocal = master_database_module.MasterSessionLocal
    middleware_module.MasterSessionLocal = master_database_module.MasterSessionLocal
    jobs_worker_module.MasterSessionLocal = master_database_module.MasterSessionLocal
    operational_database_module.engine = tenant_engine
    operational_database_module.SessionLocal = sessionmaker(bind=tenant_engine, autoflush=False, autocommit=False)
    operational_database_module.ensure_schema_for_engine(tenant_engine)
    upgrade_master_schema(master_engine, baseline=True)
    ensure_columns(
        master_engine,
        "email_sync_state",
        {
            "source_provider": "VARCHAR(50)",
            "source_host": "VARCHAR(255)",
            "source_username": "VARCHAR(255)",
            "source_connected_email": "VARCHAR(255)",
        },
        dry_run=False,
    )
    tenancy_database_module.get_tenant_engine.cache_clear()
    try:
        yield
    finally:
        master_database_module.engine = previous_master_engine
        master_database_module.MasterSessionLocal = previous_master_sessionlocal
        operational_database_module.engine = previous_operational_engine
        operational_database_module.SessionLocal = previous_operational_sessionlocal
        lifespan_module.MasterSessionLocal = previous_lifespan_master_sessionlocal
        middleware_module.MasterSessionLocal = previous_middleware_master_sessionlocal
        jobs_worker_module.MasterSessionLocal = previous_jobs_worker_master_sessionlocal
        master_engine.dispose()
        tenant_engine.dispose()
        cached_tenant_engine = tenancy_database_module.get_tenant_engine(fixture.tenant_database_url)
        cached_tenant_engine.dispose()
        tenancy_database_module.get_tenant_engine.cache_clear()
        tenancy_database_module.clear_tenant_schema_cache()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()


@contextmanager
def performance_test_client(fixture: PerformanceFixture) -> Iterator[TestClient]:
    with temporary_performance_environment(fixture):
        from app.core.app_factory import create_app
        from app.core import lifespan as lifespan_module

        app = create_app()
        with patch.object(lifespan_module, "start_email_sync_worker", lambda: None), patch.object(
            lifespan_module,
            "start_job_worker",
            lambda: None,
        ):
            with TestClient(app, raise_server_exceptions=False) as client:
                login_response = client.post(
                    "/login",
                    data={"email": fixture.admin_email, "password": fixture.admin_password},
                    follow_redirects=False,
                )
                if login_response.status_code not in {302, 303}:
                    raise RuntimeError(f"No se pudo iniciar sesion para la base de rendimiento: {login_response.status_code}")
                yield client
