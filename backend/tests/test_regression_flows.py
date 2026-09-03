from __future__ import annotations

import json
import gc
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker, selectinload
from sqlalchemy.pool import NullPool

import os
import sys

os.environ.setdefault("APP_ENV", "development")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.services import AgentProcessingService, MockAgentService  # noqa: E402
from app.channels.service import get_or_create_channel  # noqa: E402
from app.core.encryption import encrypt_secret  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import AuditLog, BackgroundJob, Company, Conversation, Customer, CustomerContact, CustomerContactPoint, Email, ExportFile, FTPSettings, InboundMessage, LLMSettings, Order, OrderLine, Product, ProductAlias, ScoringSettings  # noqa: E402
from app.jobs.service import enqueue_job, retry_job  # noqa: E402
from app.master.database import MasterBase  # noqa: E402
from app.master.models import MasterCompany, MasterTenantDatabase  # noqa: E402
from app.orders.routes import confirm_order  # noqa: E402
from app.orders.service import learn_customer_email_from_confirmed_order  # noqa: E402
from app.messages.service import upsert_inbound_message  # noqa: E402
from app.tenancy.database import get_tenant_engine  # noqa: E402
from app.tenancy.migrations import upgrade_tenant_schema  # noqa: E402
from app.workers.jobs_worker import run_worker_cycle  # noqa: E402


class RegressionFlowsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        self.master_path = base / "master.sqlite"
        self.tenant_path = base / "tenant.sqlite"
        self.master_engine = create_engine(
            f"sqlite:///{self.master_path.as_posix()}",
            connect_args={"check_same_thread": False},
            poolclass=NullPool,
        )
        self.tenant_engine = create_engine(
            f"sqlite:///{self.tenant_path.as_posix()}",
            connect_args={"check_same_thread": False},
            poolclass=NullPool,
        )
        MasterBase.metadata.create_all(self.master_engine)
        Base.metadata.create_all(self.tenant_engine)
        self.MasterSession = sessionmaker(bind=self.master_engine, autoflush=False, autocommit=False)
        self.TenantSession = sessionmaker(bind=self.tenant_engine, autoflush=False, autocommit=False)

    def tearDown(self):
        self.master_engine.dispose()
        self.tenant_engine.dispose()
        get_tenant_engine.cache_clear()
        gc.collect()
        self.tempdir.cleanup()

    def _seed_master(self):
        db = self.MasterSession()
        db.add(MasterCompany(id=1, name="Demo", slug="demo", active=True))
        db.add(MasterTenantDatabase(company_id=1, database_key="demo", database_url=f"sqlite:///{self.tenant_path.as_posix()}", is_active=True, health_status="ok"))
        db.commit()
        db.close()

    def _seed_tenant_settings(self):
        db = self.TenantSession()
        db.add(LLMSettings(company_id=1, provider="openai", api_key_encrypted=encrypt_secret("test-token")))
        db.add(
            ScoringSettings(
                company_id=1,
                safe_threshold=80,
                review_threshold=60,
                doubtful_threshold=40,
                blocked_threshold=39,
                block_without_customer=True,
                block_without_reference=True,
                block_without_quantity=True,
                block_below_threshold=True,
            )
        )
        db.add(FTPSettings(company_id=1, host="ftp.example.com", port=22, username="demo", destination_path="/exports"))
        db.commit()
        db.close()

    def _seed_customer_and_product(self):
        db = self.TenantSession()
        customer = Customer(
            company_id=1,
            code="C001",
            fiscal_name="Cliente Demo SL",
            commercial_name="Cliente Demo",
            primary_email="cliente@example.com",
            phone="600000000",
            status="active",
        )
        product = Product(
            company_id=1,
            reference="P-100",
            alternative_code="ALT-100",
            name="Producto Demo",
            description="Producto de prueba",
            sale_unit="uds",
            sale_price=10.0,
            status="active",
        )
        db.add_all(
            [
                customer,
                product,
                CustomerContactPoint(company_id=1, customer_id=1, type="email", value="cliente@example.com", is_primary=True, active=True),
                ProductAlias(company_id=1, product_id=1, alias="Producto demo"),
            ]
        )
        db.commit()
        db.close()

    def _upgrade_tenant_schema(self):
        upgrade_tenant_schema(self.tenant_engine, company_id=1, application_version="1.2.3")

    def _seed_user(self):
        return SimpleNamespace(id=1, company_id=1, role=SimpleNamespace(name="Administrador"))

    def test_ambiguous_partial_product_reference_is_not_auto_matched(self):
        self._seed_master()
        self._seed_tenant_settings()
        self._seed_customer_and_product()
        self._upgrade_tenant_schema()

        db = self.TenantSession()

        db.add_all(
            [
                Product(
                    company_id=1,
                    reference="GTAKEOUT66OZ",
                    name="Take Away rectangular cartón 1900 ml",
                    sale_unit="uds",
                    sale_price=10.0,
                    status="active",
                ),
                Product(
                    company_id=1,
                    reference="GTAKEOUT66XL",
                    name="Envase especial cartón XL",
                    sale_unit="uds",
                    sale_price=11.0,
                    status="active",
                ),
            ]
        )
        db.commit()

        email = Email(
            company_id=1,
            external_id="mail-ambiguous-product-1",
            sender="cliente@example.com",
            subject="Pedido",
            body="Pedido GTAKEOUT66",
        )
        db.add(email)
        db.commit()

        channel = get_or_create_channel(db, 1, "email")
        channel.is_active = True
        db.commit()

        classification = json.dumps(
            {
                "tipo_correo": "pedido",
                "confianza": 0.96,
                "motivo": "Pedido claro",
            },
            ensure_ascii=False,
        )

        extraction = json.dumps(
            {
                "cliente": {
                    "nombre_detectado": "Cliente Demo SL",
                    "codigo_cliente_detectado": "C001",
                },
                "pedido": {
                    "lineas": [
                        {
                            "texto_original": "GTAKEOUT66",
                            "referencia_detectada": "GTAKEOUT66",
                            "producto_detectado": "Envase",
                            "cantidad": 1,
                            "unidad": "P",
                            "confianza_extraccion": 0.92,
                        }
                    ]
                },
            },
            ensure_ascii=False,
        )

        with patch(
            "app.agent.platform.classify_sample",
            return_value={"ok": True, "content": classification},
        ), patch(
            "app.agent.platform.extract_sample",
            return_value={"ok": True, "content": extraction},
        ):
            result = AgentProcessingService().process_email(db, email)

        self.assertTrue(result["ok"])

        order = db.scalar(
            select(Order)
            .where(Order.id == result["order_id"])
            .options(selectinload(Order.lines))
        )

        self.assertIsNotNone(order)
        self.assertEqual(len(order.lines), 1)
        self.assertIsNone(order.lines[0].validated_product_id)
        self.assertEqual(order.lines[0].validation_status, "pending")

        db.close()


    def test_unique_partial_product_reference_is_matched(self):
        self._seed_master()
        self._seed_tenant_settings()
        self._seed_customer_and_product()
        self._upgrade_tenant_schema()

        db = self.TenantSession()

        db.add(
            Product(
                company_id=1,
                reference="GTAKEOUT66OZ",
                name="Take Away rectangular cartón 1900 ml",
                description="Envase take away",
                sale_unit="uds",
                sale_price=10.0,
                status="active",
            )
        )
        db.commit()

        email = Email(
            company_id=1,
            external_id="mail-partial-product-1",
            sender="cliente@example.com",
            subject="Pedido",
            body="Pedido GTAKEOUT66",
        )
        db.add(email)
        db.commit()

        channel = get_or_create_channel(db, 1, "email")
        channel.is_active = True
        db.commit()

        classification = json.dumps(
            {
                "tipo_correo": "pedido",
                "confianza": 0.96,
                "motivo": "Pedido claro",
            },
            ensure_ascii=False,
        )

        extraction = json.dumps(
            {
                "cliente": {
                    "nombre_detectado": "Cliente Demo SL",
                    "codigo_cliente_detectado": "C001",
                },
                "pedido": {
                    "lineas": [
                        {
                            "texto_original": "GTAKEOUT66 Envase Carton Take Away 1900ml",
                            "referencia_detectada": "GTAKEOUT66",
                            "producto_detectado": "Envase Carton Take Away 1900ml 212x162x65 mm 200Ud",
                            "cantidad": 1,
                            "unidad": "P",
                            "confianza_extraccion": 0.92,
                        }
                    ]
                },
            },
            ensure_ascii=False,
        )

        with patch(
            "app.agent.platform.classify_sample",
            return_value={"ok": True, "content": classification},
        ), patch(
            "app.agent.platform.extract_sample",
            return_value={"ok": True, "content": extraction},
        ):
            result = AgentProcessingService().process_email(db, email)

        self.assertTrue(result["ok"])

        order = db.scalar(
            select(Order)
            .where(Order.id == result["order_id"])
            .options(selectinload(Order.lines))
        )

        self.assertIsNotNone(order)
        self.assertEqual(len(order.lines), 1)
        self.assertIsNotNone(order.lines[0].validated_product_id)

        product = db.get(Product, order.lines[0].validated_product_id)

        self.assertEqual(product.reference, "GTAKEOUT66OZ")
        self.assertGreaterEqual(order.lines[0].line_score, 90)

        db.close()


    def test_sender_display_name_matches_customer_primary_email(self):
        self._seed_master()
        self._seed_tenant_settings()
        self._upgrade_tenant_schema()

        db = self.TenantSession()
        db.add(
            Customer(
                company_id=1,
                code="PESAFRI",
                fiscal_name="PESAFRI, S.L.",
                primary_email="administracion@pesafri.com",
                status="active",
            )
        )
        db.commit()

        from app.agent.platform import CustomerMatchingService

        customer, method, score = CustomerMatchingService().match(
            db,
            1,
            sender='"Administración" <administracion@pesafri.com>',
        )

        self.assertIsNotNone(customer)
        self.assertEqual(customer.code, "PESAFRI")
        self.assertEqual(method, "email_principal")
        self.assertEqual(score, 0.99)

        db.close()

    def test_sender_exact_customer_contact_email_is_matched(self):
        self._seed_master()
        self._seed_tenant_settings()
        self._upgrade_tenant_schema()

        db = self.TenantSession()
        customer = Customer(
            company_id=1,
            code="PESAFRI",
            fiscal_name="PESAFRI, S.L.",
            status="active",
        )
        db.add(customer)
        db.flush()
        db.add(
            CustomerContact(
                company_id=1,
                customer_id=customer.id,
                contact_type="email",
                name="Administración",
                email="administracion@pesafri.com",
                is_primary=True,
            )
        )
        db.commit()

        from app.agent.platform import CustomerMatchingService

        matched, method, score = CustomerMatchingService().match(
            db,
            1,
            sender='"Administración" <administracion@pesafri.com>',
        )

        self.assertIsNotNone(matched)
        self.assertEqual(matched.id, customer.id)
        self.assertEqual(method, "email_contacto")
        self.assertEqual(score, 0.99)

        db.close()

    def test_tenant_detected_name_is_excluded_and_sender_customer_wins(self):
        self._seed_master()
        self._seed_tenant_settings()
        self._seed_customer_and_product()
        self._upgrade_tenant_schema()

        db = self.TenantSession()

        company = Company(
            id=1,
            name="GEMAVI",
            legal_name="Comercial Gemavi Import, S.L.",
            email="pedidos@gemavi.es",
        )
        db.add(company)
        db.flush()

        customer = Customer(
            company_id=1,
            code="PESAFRI",
            fiscal_name="PESAFRI, S.L.",
            primary_email="administracion@pesafri.com",
            status="active",
        )
        db.add(customer)
        db.commit()

        email = Email(
            company_id=1,
            external_id="mail-tenant-exclusion-1",
            sender='"Administración" <administracion@pesafri.com>',
            subject="Pedido N26/000180",
            body="Pedido realizado a su empresa Comercial Gemavi Import, S.L.",
        )
        db.add(email)
        db.commit()

        channel = get_or_create_channel(db, 1, "email")
        channel.is_active = True
        db.commit()

        classification = json.dumps(
            {
                "tipo_correo": "pedido",
                "confianza": 0.96,
                "motivo": "Pedido claro",
            },
            ensure_ascii=False,
        )

        extraction = json.dumps(
            {
                "cliente": {
                    "nombre_detectado": "Comercial Gemavi Import, S.L.",
                },
                "pedido": {
                    "lineas": [
                        {
                            "texto_original": "1 unidad de P-100",
                            "referencia_detectada": "P-100",
                            "producto_detectado": "Producto Demo",
                            "cantidad": 1,
                            "unidad": "uds",
                            "confianza_extraccion": 0.95,
                        }
                    ]
                },
            },
            ensure_ascii=False,
        )

        with patch(
            "app.agent.platform.classify_sample",
            return_value={"ok": True, "content": classification},
        ), patch(
            "app.agent.platform.extract_sample",
            return_value={"ok": True, "content": extraction},
        ):
            result = AgentProcessingService().process_email(db, email)

        self.assertTrue(result["ok"])

        order = db.scalar(
            select(Order).where(Order.id == result["order_id"])
        )

        self.assertIsNotNone(order)
        self.assertEqual(
            order.customer_detected_name,
            "Comercial Gemavi Import, S.L.",
        )
        self.assertEqual(order.customer_id, customer.id)
        self.assertEqual(order.validated_customer_id, customer.id)
        self.assertEqual(order.customer_identification_method, "exact_email")
        self.assertGreaterEqual(order.customer_score, 99)
        self.assertIn(
            "receptor del pedido",
            (order.review_reasons or "").lower(),
        )

        db.close()

    def test_ambiguous_sender_domain_does_not_auto_match_customer(self):
        self._seed_master()
        self._seed_tenant_settings()
        self._upgrade_tenant_schema()

        db = self.TenantSession()

        customer_one = Customer(
            company_id=1,
            code="C001",
            fiscal_name="Cliente Uno, S.L.",
            status="active",
        )
        customer_two = Customer(
            company_id=1,
            code="C002",
            fiscal_name="Cliente Dos, S.L.",
            status="active",
        )
        db.add_all([customer_one, customer_two])
        db.flush()

        db.add_all(
            [
                CustomerContact(
                    company_id=1,
                    customer_id=customer_one.id,
                    contact_type="email",
                    email="pedidos@grupo.com",
                ),
                CustomerContact(
                    company_id=1,
                    customer_id=customer_two.id,
                    contact_type="email",
                    email="administracion@grupo.com",
                ),
            ]
        )
        db.commit()

        from app.agent.platform import CustomerMatchingService

        matched, method, score = CustomerMatchingService().match(
            db,
            1,
            sender="compras@grupo.com",
        )

        self.assertIsNone(matched)
        self.assertEqual(method, "sin_identificar")
        self.assertEqual(score, 0.0)

        db.close()

    def test_confirmed_order_learns_sender_email_for_customer(self):
        self._upgrade_tenant_schema()
        db = self.TenantSession()

        customer = Customer(
            company_id=1,
            code="PESAFRI",
            fiscal_name="PESAFRI, S.L.",
            status="active",
        )
        db.add(customer)
        db.flush()

        email = Email(
            company_id=1,
            external_id="learn-email-1",
            sender='"Administración" <administracion@pesafri.com>',
            subject="Pedido",
            body="Pedido de prueba",
        )
        db.add(email)
        db.flush()

        order = Order(
            company_id=1,
            email_id=email.id,
            customer_id=customer.id,
            validated_customer_id=customer.id,
        )
        db.add(order)
        db.flush()

        result = learn_customer_email_from_confirmed_order(
            db,
            order=order,
            company_id=1,
        )
        db.commit()

        self.assertEqual(result, "created")

        points = db.scalars(
            select(CustomerContactPoint).where(
                CustomerContactPoint.company_id == 1,
                CustomerContactPoint.type == "email",
                CustomerContactPoint.value == "administracion@pesafri.com",
            )
        ).all()

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].customer_id, customer.id)
        self.assertEqual(points[0].confidence, 1.0)
        self.assertEqual(points[0].source, "validated_order")
        self.assertTrue(points[0].active)
        self.assertIsNotNone(points[0].first_seen_at)
        self.assertIsNotNone(points[0].last_seen_at)

        db.close()

    def test_confirmed_order_reuses_existing_sender_email_learning(self):
        self._upgrade_tenant_schema()
        db = self.TenantSession()

        customer = Customer(
            company_id=1,
            code="PESAFRI",
            fiscal_name="PESAFRI, S.L.",
            status="active",
        )
        db.add(customer)
        db.flush()

        email = Email(
            company_id=1,
            external_id="learn-email-2",
            sender="administracion@pesafri.com",
            subject="Pedido",
            body="Pedido de prueba",
        )
        db.add(email)
        db.flush()

        point = CustomerContactPoint(
            company_id=1,
            customer_id=customer.id,
            type="email",
            value="administracion@pesafri.com",
            active=True,
            confidence=0.65,
            source="manual",
            first_seen_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_seen_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        db.add(point)
        db.flush()
        previous_last_seen = point.last_seen_at

        order = Order(
            company_id=1,
            email_id=email.id,
            customer_id=customer.id,
            validated_customer_id=customer.id,
        )
        db.add(order)
        db.flush()

        result = learn_customer_email_from_confirmed_order(
            db,
            order=order,
            company_id=1,
        )
        db.commit()

        self.assertEqual(result, "updated")

        points = db.scalars(
            select(CustomerContactPoint).where(
                CustomerContactPoint.company_id == 1,
                CustomerContactPoint.type == "email",
                CustomerContactPoint.value == "administracion@pesafri.com",
            )
        ).all()

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].customer_id, customer.id)
        self.assertEqual(points[0].confidence, 1.0)
        self.assertEqual(points[0].source, "validated_order")
        current_last_seen = points[0].last_seen_at
        if current_last_seen.tzinfo is None:
            current_last_seen = current_last_seen.replace(tzinfo=timezone.utc)
        if previous_last_seen.tzinfo is None:
            previous_last_seen = previous_last_seen.replace(tzinfo=timezone.utc)
        self.assertGreater(current_last_seen, previous_last_seen)

        db.close()

    def test_confirmed_order_does_not_overwrite_email_assigned_to_other_customer(self):
        self._upgrade_tenant_schema()
        db = self.TenantSession()

        customer_one = Customer(
            company_id=1,
            code="C001",
            fiscal_name="Cliente Uno, S.L.",
            status="active",
        )
        customer_two = Customer(
            company_id=1,
            code="C002",
            fiscal_name="Cliente Dos, S.L.",
            status="active",
        )
        db.add_all([customer_one, customer_two])
        db.flush()

        db.add(
            CustomerContactPoint(
                company_id=1,
                customer_id=customer_one.id,
                type="email",
                value="administracion@pesafri.com",
                active=True,
                confidence=1.0,
                source="validated_order",
            )
        )

        email = Email(
            company_id=1,
            external_id="learn-email-conflict",
            sender="administracion@pesafri.com",
            subject="Pedido",
            body="Pedido de prueba",
        )
        db.add(email)
        db.flush()

        order = Order(
            company_id=1,
            email_id=email.id,
            customer_id=customer_two.id,
            validated_customer_id=customer_two.id,
        )
        db.add(order)
        db.flush()

        result = learn_customer_email_from_confirmed_order(
            db,
            order=order,
            company_id=1,
        )
        db.commit()

        self.assertEqual(result, "conflict")

        points = db.scalars(
            select(CustomerContactPoint).where(
                CustomerContactPoint.company_id == 1,
                CustomerContactPoint.type == "email",
                CustomerContactPoint.value == "administracion@pesafri.com",
            )
        ).all()

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].customer_id, customer_one.id)

        db.close()

    def test_confirm_order_learns_sender_email_automatically(self):
        self._seed_master()
        self._seed_tenant_settings()
        self._seed_customer_and_product()
        self._upgrade_tenant_schema()

        db = self.TenantSession()

        customer = db.scalar(
            select(Customer).where(
                Customer.company_id == 1,
                Customer.code == "C001",
            )
        )
        self.assertIsNotNone(customer)

        email = Email(
            company_id=1,
            external_id="confirm-learn-email-1",
            sender='"Compras Cliente" <nuevo-contacto@cliente-demo.com>',
            subject="Pedido",
            body="Pedido de prueba",
        )
        db.add(email)
        db.flush()

        order = Order(
            company_id=1,
            email_id=email.id,
            customer_id=customer.id,
            validated_customer_id=customer.id,
            status="pedido_pendiente_revision",
            score=95,
        )
        db.add(order)
        db.flush()

        db.add(
            OrderLine(
                company_id=1,
                order_id=order.id,
                product_id=1,
                validated_product_id=1,
                original_text="1 unidad P-100",
                detected_reference="P-100",
                detected_product="Producto Demo",
                quantity=1,
                unit="uds",
                extraction_confidence=0.95,
                line_score=95,
                validation_status="validated",
            )
        )
        db.commit()

        user = self._seed_user()
        confirm_order(order.id, db=db, user=user)

        db.refresh(order)
        self.assertEqual(order.status, "pedido_confirmado")

        points = db.scalars(
            select(CustomerContactPoint).where(
                CustomerContactPoint.company_id == 1,
                CustomerContactPoint.customer_id == customer.id,
                CustomerContactPoint.type == "email",
                CustomerContactPoint.value == "nuevo-contacto@cliente-demo.com",
            )
        ).all()

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].confidence, 1.0)
        self.assertEqual(points[0].source, "validated_order")
        self.assertTrue(points[0].active)

        db.close()

    def test_weak_fuzzy_customer_is_not_auto_validated(self):
        self._seed_master()
        self._seed_tenant_settings()
        self._seed_customer_and_product()
        self._upgrade_tenant_schema()

        db = self.TenantSession()

        db.add(
            Customer(
                company_id=1,
                code="1045",
                fiscal_name="COMERCIAL RYALIMP, S.L.",
                commercial_name="COMERCIAL RYALIMP",
                primary_email="ryalimp@example.com",
                status="active",
            )
        )
        db.commit()

        email = Email(
            company_id=1,
            external_id="mail-fuzzy-customer-1",
            sender="pedidos@unknown-example.com",
            subject="Pedido",
            body="Pedido de Comercial Gemavi Import, S.L.",
        )
        db.add(email)
        db.commit()

        channel = get_or_create_channel(db, 1, "email")
        channel.is_active = True
        db.commit()

        classification = json.dumps(
            {
                "tipo_correo": "pedido",
                "confianza": 0.96,
                "motivo": "Pedido claro",
            },
            ensure_ascii=False,
        )

        extraction = json.dumps(
            {
                "cliente": {
                    "nombre_detectado": "Comercial Gemavi Import, S.L.",
                },
                "pedido": {
                    "lineas": [
                        {
                            "texto_original": "1 unidad de P-100",
                            "referencia_detectada": "P-100",
                            "producto_detectado": "Producto Demo",
                            "cantidad": 1,
                            "unidad": "uds",
                            "confianza_extraccion": 0.95,
                        }
                    ]
                },
            },
            ensure_ascii=False,
        )

        with patch(
            "app.agent.platform.classify_sample",
            return_value={"ok": True, "content": classification},
        ), patch(
            "app.agent.platform.extract_sample",
            return_value={"ok": True, "content": extraction},
        ):
            result = AgentProcessingService().process_email(db, email)

        self.assertTrue(result["ok"])

        order = db.scalar(
            select(Order).where(Order.id == result["order_id"])
        )

        self.assertIsNotNone(order)
        self.assertEqual(
            order.customer_detected_name,
            "Comercial Gemavi Import, S.L.",
        )
        self.assertIsNone(order.customer_id)
        self.assertIsNone(order.validated_customer_id)
        self.assertIn("cliente no identificado", (order.review_reasons or "").lower())
        self.assertIn("requiere validacion humana", (order.review_reasons or "").lower())
        self.assertIn("fuzzy_name", (order.review_reasons or "").lower())

        db.close()


    def test_mock_order_uses_tenant_company_name(self):
        db = self.TenantSession()
        try:
            db.add(Company(id=1, name="Tenant Prueba SL"))
            db.commit()

            MockAgentService().create_mock_order(db, 1)

            email = db.scalar(
                select(Email).where(
                    Email.company_id == 1,
                    Email.external_id.like("mock-1-%"),
                )
            )

            self.assertIsNotNone(email)
            self.assertIn("Tenant Prueba SL", email.extracted_text or "")
            self.assertNotIn("Anchi Demo", email.extracted_text or "")
            self.assertNotEqual(email.sender, "compras@anchi-demo.local")
        finally:
            db.close()

    def test_external_export_failure_does_not_mark_order_as_exported(self):
        self._seed_master()
        self._seed_tenant_settings()
        self._seed_customer_and_product()
        self._upgrade_tenant_schema()

        db = self.TenantSession()

        order = Order(
            company_id=1,
            customer_id=1,
            validated_customer_id=1,
            status="pedido_confirmado",
            score=95,
        )
        db.add(order)
        db.flush()

        db.add(
            OrderLine(
                company_id=1,
                order_id=order.id,
                product_id=1,
                validated_product_id=1,
                quantity=1,
                unit="uds",
                validation_status="validated",
            )
        )
        db.commit()

        user = self._seed_user()

        enqueue_job(
            db,
            company_id=1,
            job_type="export_order_ftp",
            payload={"order_id": order.id},
            created_by_user_id=user.id,
        )

        job = db.scalar(
            select(BackgroundJob)
            .where(
                BackgroundJob.company_id == 1,
                BackgroundJob.job_type == "export_order_ftp",
            )
            .order_by(BackgroundJob.id.desc())
        )

        with patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession), patch(
            "app.workers.jobs_worker.FTPService.send",
            side_effect=TimeoutError("connection refused"),
        ):
            run_worker_cycle()

        db.refresh(job)
        db.refresh(order)

        export = db.scalar(
            select(ExportFile)
            .where(
                ExportFile.company_id == 1,
                ExportFile.order_id == order.id,
            )
            .order_by(ExportFile.id.desc())
        )

        self.assertIsNotNone(export)
        self.assertNotEqual(order.status, "pedido_exportado")
        self.assertIsNone(order.exported_at)
        self.assertEqual(job.status, "retrying")

        db.close()


    def test_unconfirmed_order_cannot_be_sent_to_external_export(self):
        self._seed_master()
        self._seed_tenant_settings()
        self._seed_customer_and_product()
        self._upgrade_tenant_schema()

        db = self.TenantSession()

        order = Order(
            company_id=1,
            customer_id=1,
            validated_customer_id=1,
            status="pedido_pendiente_revision",
            score=95,
        )
        db.add(order)
        db.flush()

        db.add(
            OrderLine(
                company_id=1,
                order_id=order.id,
                product_id=1,
                validated_product_id=1,
                quantity=1,
                unit="uds",
                validation_status="validated",
            )
        )
        db.commit()

        user = self._seed_user()

        enqueue_job(
            db,
            company_id=1,
            job_type="export_order_ftp",
            payload={"order_id": order.id},
            created_by_user_id=user.id,
        )

        job = db.scalar(
            select(BackgroundJob)
            .where(
                BackgroundJob.company_id == 1,
                BackgroundJob.job_type == "export_order_ftp",
            )
            .order_by(BackgroundJob.id.desc())
        )

        with patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession), patch(
            "app.workers.jobs_worker.FTPService.send",
            return_value=True,
        ) as send_mock:
            run_worker_cycle()

        db.refresh(job)
        db.refresh(order)

        self.assertEqual(job.status, "failed")
        self.assertEqual(order.status, "pedido_pendiente_revision")
        send_mock.assert_not_called()

        db.close()


    def test_email_to_review_confirm_export_and_audit_flow(self):
        self._seed_master()
        self._seed_tenant_settings()
        self._seed_customer_and_product()
        self._upgrade_tenant_schema()
        db = self.TenantSession()
        email = Email(company_id=1, external_id="mail-e2e-1", sender="cliente@example.com", subject="Pedido demo", body="Necesitamos 5 unidades de P-100.")
        db.add(email)
        db.commit()

        channel = get_or_create_channel(db, 1, "email")
        channel.is_active = True
        db.commit()

        classification = json.dumps({"tipo_correo": "pedido", "confianza": 0.96, "motivo": "Pedido claro"}, ensure_ascii=False)
        extraction = json.dumps(
            {
                "cliente": {"nombre_detectado": "Cliente Demo SL", "codigo_cliente_detectado": "C001"},
                "pedido": {
                    "fecha_pedido": "2026-07-16",
                    "observaciones": "Pedido de ejemplo",
                    "lineas": [
                        {
                            "texto_original": "5 unidades de P-100",
                            "referencia_detectada": "P-100",
                            "producto_detectado": "Producto Demo",
                            "cantidad": 5,
                            "unidad": "uds",
                            "confianza_extraccion": 0.95,
                        }
                    ],
                },
            },
            ensure_ascii=False,
        )

        with patch("app.agent.platform.classify_sample", return_value={"ok": True, "content": classification}), patch(
            "app.agent.platform.extract_sample", return_value={"ok": True, "content": extraction}
        ):
            result = AgentProcessingService().process_email(db, email)

        self.assertTrue(result["ok"])
        self.assertIn("review_id", result)
        inbound = db.scalar(select(InboundMessage).where(InboundMessage.company_id == 1, InboundMessage.source_external_id == "mail-e2e-1"))
        self.assertIsNotNone(inbound)
        self.assertIsNotNone(inbound.conversation_id)
        conversation = db.get(Conversation, inbound.conversation_id)
        self.assertIsNotNone(conversation)
        order = db.scalar(select(Order).where(Order.id == result["order_id"]).options(selectinload(Order.lines)))
        self.assertIsNotNone(order.validated_customer_id)
        self.assertEqual(len(order.lines or []), 1)
        self.assertEqual(order.lines[0].validated_product_id, 1)

        user = self._seed_user()
        confirm_order(order.id, db=db, user=user)
        db.refresh(order)
        self.assertEqual(order.status, "pedido_confirmado")

        enqueue_job(
            db,
            company_id=1,
            job_type="export_order",
            payload={"order_id": order.id},
            created_by_user_id=user.id,
        )
        generate_job = db.scalar(
            select(BackgroundJob)
            .where(
                BackgroundJob.company_id == 1,
                BackgroundJob.job_type == "export_order",
            )
            .order_by(BackgroundJob.id.desc())
        )
        self.assertIsNotNone(generate_job)

        with patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession):
            generate_summary = run_worker_cycle()

        self.assertEqual(generate_summary["tenants"], 1)

        db.refresh(order)
        self.assertEqual(order.status, "pedido_confirmado")

        export = db.scalar(
            select(ExportFile)
            .where(
                ExportFile.company_id == 1,
                ExportFile.order_id == order.id,
            )
            .order_by(ExportFile.id.desc())
        )
        self.assertIsNotNone(export)
        self.assertEqual(export.status, "generated")
        self.assertEqual(
            db.scalar(select(func.count()).select_from(ExportFile)) or 0,
            1,
        )

        enqueue_job(
            db,
            company_id=1,
            job_type="export_order_ftp",
            payload={"order_id": order.id},
            created_by_user_id=user.id,
        )
        send_job = db.scalar(
            select(BackgroundJob)
            .where(
                BackgroundJob.company_id == 1,
                BackgroundJob.job_type == "export_order_ftp",
            )
            .order_by(BackgroundJob.id.desc())
        )
        self.assertIsNotNone(send_job)

        with patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession), patch(
            "app.workers.jobs_worker.FTPService.send",
            return_value=True,
        ):
            send_summary = run_worker_cycle()

        self.assertEqual(send_summary["tenants"], 1)

        db.refresh(order)
        db.refresh(export)

        self.assertEqual(order.status, "pedido_exportado")
        self.assertEqual(export.status, "sent")
        self.assertEqual(
            db.scalar(select(func.count()).select_from(ExportFile)) or 0,
            1,
        )

        actions = {
            row.action
            for row in db.scalars(
                select(AuditLog).where(AuditLog.company_id == 1)
            ).all()
        }
        self.assertIn("agent.order_created", actions)
        self.assertIn("order.confirm", actions)
        self.assertIn("job.export_order.success", actions)
        self.assertIn("job.export_order_ftp.success", actions)

        enqueue_job(
            db,
            company_id=1,
            job_type="export_order_ftp",
            payload={"order_id": order.id},
            created_by_user_id=user.id,
        )
        duplicate_send_job = db.scalar(
            select(BackgroundJob)
            .where(
                BackgroundJob.company_id == 1,
                BackgroundJob.job_type == "export_order_ftp",
            )
            .order_by(BackgroundJob.id.desc())
        )
        with patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession), patch(
            "app.workers.jobs_worker.FTPService.send",
            return_value=True,
        ) as duplicate_send_mock:
            duplicate_summary = run_worker_cycle()

        self.assertEqual(duplicate_summary["tenants"], 1)
        db.refresh(duplicate_send_job)
        self.assertEqual(duplicate_send_job.status, "success")
        duplicate_send_mock.assert_not_called()

        db.close()


    def test_force_reprocess_email_updates_same_order(self):
        self._seed_master()
        self._seed_tenant_settings()
        self._seed_customer_and_product()
        self._upgrade_tenant_schema()

        db = self.TenantSession()

        email = Email(
            company_id=1,
            external_id="mail-force-reprocess-1",
            sender="cliente@example.com",
            subject="Pedido para reprocesar",
            body="Necesitamos 5 unidades de P-100.",
        )
        db.add(email)
        db.commit()

        channel = get_or_create_channel(db, 1, "email")
        channel.is_active = True
        db.commit()

        classification = json.dumps(
            {
                "tipo_correo": "pedido",
                "confianza": 0.96,
                "motivo": "Pedido claro",
            },
            ensure_ascii=False,
        )

        first_extraction = json.dumps(
            {
                "cliente": {
                    "nombre_detectado": "Cliente Demo SL",
                    "codigo_cliente_detectado": "C001",
                },
                "pedido": {
                    "fecha_pedido": "2026-07-16",
                    "observaciones": "Primera extraccion",
                    "lineas": [
                        {
                            "texto_original": "5 unidades de P-100",
                            "referencia_detectada": "P-100",
                            "producto_detectado": "Producto Demo",
                            "cantidad": 5,
                            "unidad": "uds",
                            "confianza_extraccion": 0.95,
                        }
                    ],
                },
            },
            ensure_ascii=False,
        )

        with patch(
            "app.agent.platform.classify_sample",
            return_value={"ok": True, "content": classification},
        ), patch(
            "app.agent.platform.extract_sample",
            return_value={"ok": True, "content": first_extraction},
        ):
            first = AgentProcessingService().process_email(db, email)

        self.assertTrue(first["ok"])
        original_order_id = first["order_id"]

        second_extraction = json.dumps(
            {
                "cliente": {
                    "nombre_detectado": "Cliente Demo SL",
                    "codigo_cliente_detectado": "C001",
                },
                "pedido": {
                    "fecha_pedido": "2026-07-16",
                    "observaciones": "Pedido reprocesado",
                    "lineas": [
                        {
                            "texto_original": "7 unidades de P-100",
                            "referencia_detectada": "P-100",
                            "producto_detectado": "Producto Demo",
                            "cantidad": 7,
                            "unidad": "uds",
                            "confianza_extraccion": 0.97,
                        }
                    ],
                },
            },
            ensure_ascii=False,
        )

        with patch(
            "app.agent.platform.classify_sample",
            return_value={"ok": True, "content": classification},
        ), patch(
            "app.agent.platform.extract_sample",
            return_value={"ok": True, "content": second_extraction},
        ):
            second = AgentProcessingService().process_email(
                db,
                email,
                force_order=True,
            )

        self.assertTrue(second["ok"])
        self.assertEqual(second["order_id"], original_order_id)

        self.assertEqual(
            db.scalar(select(func.count()).select_from(Order)) or 0,
            1,
        )

        self.assertEqual(
            db.scalar(select(func.count()).select_from(InboundMessage)) or 0,
            1,
        )

        db.expire_all()

        order = db.scalar(
            select(Order)
            .where(Order.id == original_order_id)
            .options(selectinload(Order.lines))
        )

        self.assertIsNotNone(order)
        self.assertEqual(order.id, original_order_id)
        self.assertEqual(order.notes, "Pedido reprocesado")
        self.assertEqual(len(order.lines or []), 1)
        self.assertEqual(order.lines[0].quantity, 7)
        self.assertEqual(order.lines[0].detected_reference, "P-100")

        inbound = db.scalar(
            select(InboundMessage).where(
                InboundMessage.company_id == 1,
                InboundMessage.source_external_id == "mail-force-reprocess-1",
            )
        )

        self.assertEqual(inbound.order_id, original_order_id)

        db.close()

    def test_force_reprocess_email_can_be_reapplied_without_duplicate_order(self):
        self._seed_master()
        self._seed_tenant_settings()
        self._seed_customer_and_product()
        self._upgrade_tenant_schema()

        db = self.TenantSession()

        email = Email(
            company_id=1,
            external_id="mail-force-reprocess-2",
            sender="cliente@example.com",
            subject="Pedido para reprocesar varias veces",
            body="Necesitamos 5 unidades de P-100.",
        )
        db.add(email)
        db.commit()

        channel = get_or_create_channel(db, 1, "email")
        channel.is_active = True
        db.commit()

        classification = json.dumps(
            {
                "tipo_correo": "pedido",
                "confianza": 0.96,
                "motivo": "Pedido claro",
            },
            ensure_ascii=False,
        )

        first_extraction = json.dumps(
            {
                "cliente": {
                    "nombre_detectado": "Cliente Demo SL",
                    "codigo_cliente_detectado": "C001",
                },
                "pedido": {
                    "fecha_pedido": "2026-07-16",
                    "observaciones": "Primera extraccion",
                    "lineas": [
                        {
                            "texto_original": "5 unidades de P-100",
                            "referencia_detectada": "P-100",
                            "producto_detectado": "Producto Demo",
                            "cantidad": 5,
                            "unidad": "uds",
                            "confianza_extraccion": 0.95,
                        }
                    ],
                },
            },
            ensure_ascii=False,
        )

        second_extraction = json.dumps(
            {
                "cliente": {
                    "nombre_detectado": "Cliente Demo SL",
                    "codigo_cliente_detectado": "C001",
                },
                "pedido": {
                    "fecha_pedido": "2026-07-16",
                    "observaciones": "Segundo reproceso",
                    "lineas": [
                        {
                            "texto_original": "7 unidades de P-100",
                            "referencia_detectada": "P-100",
                            "producto_detectado": "Producto Demo",
                            "cantidad": 7,
                            "unidad": "uds",
                            "confianza_extraccion": 0.97,
                        }
                    ],
                },
            },
            ensure_ascii=False,
        )

        third_extraction = json.dumps(
            {
                "cliente": {
                    "nombre_detectado": "Cliente Demo SL",
                    "codigo_cliente_detectado": "C001",
                },
                "pedido": {
                    "fecha_pedido": "2026-07-16",
                    "observaciones": "Tercer reproceso",
                    "lineas": [
                        {
                            "texto_original": "9 unidades de P-100",
                            "referencia_detectada": "P-100",
                            "producto_detectado": "Producto Demo",
                            "cantidad": 9,
                            "unidad": "uds",
                            "confianza_extraccion": 0.98,
                        }
                    ],
                },
            },
            ensure_ascii=False,
        )

        with patch(
            "app.agent.platform.classify_sample",
            return_value={"ok": True, "content": classification},
        ), patch(
            "app.agent.platform.extract_sample",
            side_effect=[
                {"ok": True, "content": first_extraction},
                {"ok": True, "content": second_extraction},
                {"ok": True, "content": third_extraction},
            ],
        ):
            first = AgentProcessingService().process_email(db, email)
            second = AgentProcessingService().process_email(db, email, force_order=True)
            third = AgentProcessingService().process_email(db, email, force_order=True)

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertTrue(third["ok"])
        self.assertEqual(first["order_id"], second["order_id"])
        self.assertEqual(first["order_id"], third["order_id"])
        self.assertEqual(db.scalar(select(func.count()).select_from(Order)) or 0, 1)
        self.assertEqual(db.scalar(select(func.count()).select_from(InboundMessage)) or 0, 1)

        order = db.scalar(
            select(Order)
            .where(Order.id == first["order_id"])
            .options(selectinload(Order.lines))
        )
        self.assertIsNotNone(order)
        self.assertEqual(order.notes, "Tercer reproceso")
        self.assertEqual(len(order.lines or []), 1)
        self.assertEqual(order.lines[0].quantity, 9)
        self.assertEqual(order.lines[0].detected_reference, "P-100")

        inbound = db.scalar(
            select(InboundMessage).where(
                InboundMessage.company_id == 1,
                InboundMessage.source_external_id == "mail-force-reprocess-2",
            )
        )
        self.assertEqual(inbound.order_id, first["order_id"])

        db.close()

    def test_duplicate_mail_retry_and_export_retry_path(self):
        self._seed_master()
        self._seed_tenant_settings()
        self._seed_customer_and_product()
        self._upgrade_tenant_schema()
        db = self.TenantSession()
        first_message, _ = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="email",
            provider="imap",
            external_id="mail-dup-1",
            sender="cliente@example.com",
            recipients=["pedidos@example.com"],
            subject="Pedido duplicado",
            text_content="Pedido duplicado",
            external_thread_id="thread-dup",
            received_at=datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc),
            metadata={"message_id": "mail-dup-1"},
            content_type="email",
        )
        duplicate_message, _ = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="email",
            provider="imap",
            external_id="mail-dup-1",
            sender="cliente@example.com",
            recipients=["pedidos@example.com"],
            subject="Pedido duplicado",
            text_content="Pedido duplicado otra vez",
            external_thread_id="thread-dup",
            received_at=datetime(2026, 7, 16, 10, 1, tzinfo=timezone.utc),
            metadata={"message_id": "mail-dup-1"},
            content_type="email",
        )
        db.commit()
        self.assertEqual(first_message.id, duplicate_message.id)
        self.assertEqual(db.scalar(select(func.count()).select_from(InboundMessage)) or 0, 1)
        self.assertEqual(db.scalar(select(func.count()).select_from(Conversation)) or 0, 1)

        email = Email(company_id=1, external_id="mail-dup-1", sender="cliente@example.com", subject="Pedido duplicado", body="Pedido de 5 unidades de P-100")
        db.add(email)
        db.commit()

        with patch("app.agent.platform.classify_sample", return_value={"ok": True, "content": "no-json"}), patch(
            "app.agent.platform.extract_sample", return_value={"ok": True, "content": "no-json"}
        ):
            failed = AgentProcessingService().process_email(db, email)
        self.assertFalse(failed["ok"])
        self.assertEqual(db.get(Email, email.id).status, "error_processing")

        classification = json.dumps({"tipo_correo": "pedido", "confianza": 0.95, "motivo": "Pedido claro"}, ensure_ascii=False)
        extraction = json.dumps(
            {
                "cliente": {"nombre_detectado": "Cliente Demo SL", "codigo_cliente_detectado": "C001"},
                "pedido": {
                    "fecha_pedido": "2026-07-16",
                    "lineas": [
                        {
                            "texto_original": "5 unidades de P-100",
                            "referencia_detectada": "P-100",
                            "producto_detectado": "Producto Demo",
                            "cantidad": 5,
                            "unidad": "uds",
                            "confianza_extraccion": 0.93,
                        }
                    ],
                },
            },
            ensure_ascii=False,
        )

        with patch("app.agent.platform.classify_sample", return_value={"ok": True, "content": classification}), patch(
            "app.agent.platform.extract_sample", return_value={"ok": True, "content": extraction}
        ):
            retried = AgentProcessingService().process_email(db, email)

        self.assertTrue(retried["ok"])
        order = db.scalar(select(Order).where(Order.id == retried["order_id"]).options(selectinload(Order.lines)))
        user = self._seed_user()
        confirm_order(order.id, db=db, user=user)
        enqueue_job(db, company_id=1, job_type="export_order_ftp", payload={"order_id": order.id}, created_by_user_id=user.id)
        job = db.scalar(select(BackgroundJob).where(BackgroundJob.company_id == 1, BackgroundJob.job_type == "export_order_ftp").order_by(BackgroundJob.id.desc()))
        self.assertIsNotNone(job)

        with patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession), patch(
            "app.workers.jobs_worker.FTPService.send", side_effect=[TimeoutError("connection refused"), True]
        ):
            first_cycle = run_worker_cycle()

        self.assertEqual(first_cycle["tenants"], 1)
        db.refresh(job)
        self.assertEqual(job.status, "retrying")
        retried_job = retry_job(db, 1, job.id)
        self.assertIsNotNone(retried_job)

        with patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession), patch(
            "app.workers.jobs_worker.FTPService.send", return_value=True
        ):
            second_cycle = run_worker_cycle()

        self.assertEqual(second_cycle["tenants"], 1)
        db.refresh(order)
        self.assertEqual(order.status, "pedido_exportado")
        self.assertEqual(db.scalar(select(func.count()).select_from(ExportFile)) or 0, 1)
        db.close()


if __name__ == "__main__":
    unittest.main()
