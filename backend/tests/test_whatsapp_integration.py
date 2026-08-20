from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.agent.platform import UnifiedOrderPipelineService
from app.core.encryption import encrypt_secret
from app.db.database import Base
from app.db.models import BackgroundJob, ChannelSetting, Conversation, Customer, CustomerContactPoint, InboundMessage, InputChannel, LLMSettings, Order, Product, ProductAlias, ScoringSettings
from app.jobs.service import enqueue_job
from app.master.database import MasterBase
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser
from app.master.service import TenantRole, TenantUser
from app.messages.service import upsert_inbound_message
from app.tenancy.migrations import upgrade_tenant_schema
from app.whatsapp.service import enqueue_whatsapp_processing, parse_payload_events, persist_event, redact_whatsapp_config, resolve_company_from_slug, verify_signature, verify_webhook_token, whatsapp_config
from app.workers.jobs_worker import run_worker_cycle


class WhatsAppIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        self.master_path = base / "master.sqlite"
        self.tenant_path = base / "tenant.sqlite"
        self.master_engine = create_engine(f"sqlite:///{self.master_path.as_posix()}", connect_args={"check_same_thread": False}, poolclass=NullPool)
        self.tenant_engine = create_engine(f"sqlite:///{self.tenant_path.as_posix()}", connect_args={"check_same_thread": False}, poolclass=NullPool)
        MasterBase.metadata.create_all(self.master_engine)
        Base.metadata.create_all(self.tenant_engine)
        self.MasterSession = sessionmaker(bind=self.master_engine, autoflush=False, autocommit=False)
        self.TenantSession = sessionmaker(bind=self.tenant_engine, autoflush=False, autocommit=False)
        self._seed_master()
        self._seed_tenant()
        self._seed_whatsapp_config()
        self._seed_agent_data()

    def tearDown(self):
        self.master_engine.dispose()
        self.tenant_engine.dispose()
        self.tempdir.cleanup()

    def _seed_master(self):
        db = self.MasterSession()
        company = MasterCompany(id=1, name="WhatsApp Demo", slug="whatsapp-demo", legal_name="WhatsApp Demo SL", active=True)
        user = MasterUser(id=1, email="admin@anchi.local", full_name="Admin", password_hash="hash", is_active=True)
        membership = CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=True)
        tenant = MasterTenantDatabase(company_id=1, database_key="whatsapp-demo", database_url=f"sqlite:///{self.tenant_path.as_posix()}", is_active=True, health_status="ok")
        db.add_all([company, user, membership, tenant])
        db.commit()
        db.close()

    def _seed_tenant(self):
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
        db.commit()
        upgrade_tenant_schema(self.tenant_engine, company_id=1, application_version="1.2.3")
        db.close()

    def _seed_whatsapp_config(self):
        db = self.TenantSession()
        channel = db.scalar(select(InputChannel).where(InputChannel.company_id == 1, InputChannel.key == "whatsapp"))
        if not channel:
            channel = InputChannel(company_id=1, key="whatsapp", name="WhatsApp", channel_type="message", is_active=True, is_default=False, supports_text=True, supports_attachments=True, supports_audio=True, supports_documents=True, supports_images=True)
            db.add(channel)
            db.flush()
        for key, value in {
            "enabled": "true",
            "provider": "meta",
            "phone_number_id": "pn-123",
            "business_account_id": "ba-123",
            "access_token": "token-123",
            "verify_token": "verify-123",
            "app_secret": "secret-123",
            "webhook_enabled": "true",
            "bot_enabled": "true",
            "default_language": "es",
            "timezone": "Europe/Madrid",
        }.items():
            db.add(ChannelSetting(company_id=1, channel_id=channel.id, key=key, value=value, value_type="string", is_secret=key in {"access_token", "verify_token", "app_secret"}))
        db.commit()
        db.close()

    def _seed_agent_data(self):
        db = self.TenantSession()
        customer = Customer(company_id=1, code="C001", fiscal_name="Cliente WhatsApp SL", commercial_name="Cliente WhatsApp", primary_email="whatsapp@example.com", phone="+34600000000", status="active")
        product = Product(company_id=1, reference="P-100", alternative_code="ALT-100", name="Producto WhatsApp", sale_unit="uds", sale_price=11.0, status="active")
        db.add_all(
            [
                customer,
                product,
                CustomerContactPoint(company_id=1, customer_id=1, type="whatsapp", value="+34600000000", is_primary=True, active=True),
                ProductAlias(company_id=1, product_id=1, alias="Producto whatsapp"),
            ]
        )
        db.commit()
        db.close()

    def test_verification_and_signature(self):
        db = self.TenantSession()
        config = whatsapp_config(db, 1)
        db.close()
        self.assertTrue(verify_webhook_token(config, "verify-123"))
        self.assertFalse(verify_webhook_token(config, "wrong"))
        body = b'{"entry":[]}'
        signature = "sha256=" + hmac.new(b"secret-123", body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_signature("secret-123", body, signature))
        self.assertFalse(verify_signature("secret-123", body, "sha256=bad"))
        redacted = redact_whatsapp_config(config)
        self.assertEqual(redacted["access_token"], "••••••••")
        self.assertEqual(redacted["verify_token"], "••••••••")
        self.assertEqual(redacted["app_secret"], "••••••••")

    def test_text_message_dedupes_and_queues_job(self):
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "pn-123", "business_account_id": "ba-123"},
                                "messages": [
                                    {
                                        "id": "wa-msg-1",
                                        "from": "+34600000000",
                                        "type": "text",
                                        "text": {"body": "Necesitamos 5 unidades de P-100"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        events = parse_payload_events(payload)
        self.assertEqual(len(events), 1)
        db = self.TenantSession()
        message = persist_event(db, 1, events[0])
        enqueue_whatsapp_processing(db, 1, message.id)
        duplicate = persist_event(db, 1, events[0])
        db.commit()
        self.assertEqual(message.id, duplicate.id)
        self.assertEqual(db.scalar(select(func.count()).select_from(InboundMessage)) or 0, 1)
        self.assertEqual(db.scalar(select(func.count()).select_from(BackgroundJob)) or 0, 1)
        db.close()

    def test_worker_processes_whatsapp_into_order(self):
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "pn-123", "business_account_id": "ba-123"},
                                "messages": [
                                    {
                                        "id": "wa-msg-2",
                                        "from": "+34600000000",
                                        "type": "text",
                                        "text": {"body": "Pedido de 5 unidades de P-100 para Cliente WhatsApp SL"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        events = parse_payload_events(payload)
        db = self.TenantSession()
        message = persist_event(db, 1, events[0])
        enqueue_whatsapp_processing(db, 1, message.id)
        db.commit()
        self.assertEqual(db.scalar(select(func.count()).select_from(BackgroundJob)) or 0, 1)
        db.close()

        classification = json.dumps({"tipo_correo": "pedido", "confianza": 0.97, "motivo": "Pedido claro"}, ensure_ascii=False)
        extraction = json.dumps(
            {
                "cliente": {"nombre_detectado": "Cliente WhatsApp SL", "codigo_cliente_detectado": "C001"},
                "pedido": {
                    "fecha_pedido": "2026-07-16",
                    "lineas": [
                        {
                            "texto_original": "5 unidades de P-100",
                            "referencia_detectada": "P-100",
                            "producto_detectado": "Producto WhatsApp",
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
        ), patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession):
            summary = run_worker_cycle()

        db = self.TenantSession()
        self.assertEqual(summary["processed"], 1)
        order = db.scalar(select(Order).where(Order.company_id == 1))
        self.assertIsNotNone(order)
        self.assertEqual(order.customer_id, 1)
        self.assertEqual(order.lines[0].validated_product_id, 1)
        conversation = db.scalar(select(Conversation).where(Conversation.company_id == 1))
        self.assertIsNotNone(conversation)
        db.close()


    def test_reprocessing_same_whatsapp_message_does_not_duplicate_order(self):
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {
                                    "phone_number_id": "pn-123",
                                    "business_account_id": "ba-123",
                                },
                                "messages": [
                                    {
                                        "id": "wa-idempotency-1",
                                        "from": "+34600000000",
                                        "type": "text",
                                        "text": {
                                            "body": "Pedido de 5 unidades de P-100 para Cliente WhatsApp SL"
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }

        events = parse_payload_events(payload)
        db = self.TenantSession()
        message = persist_event(db, 1, events[0])
        db.commit()

        classification = json.dumps(
            {
                "tipo_correo": "pedido",
                "confianza": 0.97,
                "motivo": "Pedido claro",
            },
            ensure_ascii=False,
        )
        extraction = json.dumps(
            {
                "cliente": {
                    "nombre_detectado": "Cliente WhatsApp SL",
                    "codigo_cliente_detectado": "C001",
                },
                "pedido": {
                    "fecha_pedido": "2026-07-16",
                    "lineas": [
                        {
                            "texto_original": "5 unidades de P-100",
                            "referencia_detectada": "P-100",
                            "producto_detectado": "Producto WhatsApp",
                            "cantidad": 5,
                            "unidad": "uds",
                            "confianza_extraccion": 0.95,
                        }
                    ],
                },
            },
            ensure_ascii=False,
        )

        pipeline = UnifiedOrderPipelineService()

        with patch(
            "app.agent.platform.classify_sample",
            return_value={"ok": True, "content": classification},
        ), patch(
            "app.agent.platform.extract_sample",
            return_value={"ok": True, "content": extraction},
        ):
            first = pipeline.process_inbound_message(db, message)
            second = pipeline.process_inbound_message(db, message)

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(first["order_id"], second["order_id"])

        self.assertEqual(
            db.scalar(select(func.count()).select_from(InboundMessage)) or 0,
            1,
        )
        self.assertEqual(
            db.scalar(select(func.count()).select_from(Order)) or 0,
            1,
        )

        db.close()


    def test_webhook_tenant_resolution_by_slug(self):
        master_db = self.MasterSession()
        company, tenant = resolve_company_from_slug(master_db, "whatsapp-demo")
        self.assertIsNotNone(company)
        self.assertIsNotNone(tenant)
        self.assertEqual(company.id, 1)
        master_db.close()
