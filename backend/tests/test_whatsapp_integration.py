from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from zipfile import ZipFile

import httpx
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from starlette.background import BackgroundTasks
from starlette.requests import Request

from app.agent.platform import UnifiedOrderPipelineService
from app.alerts.routes import alert_default_action
from app.core.attachment_extraction import extract_attachment_text
from app.core.encryption import decrypt_secret, encrypt_secret
from app.db.database import Base
from app.db.models import Alert, BackgroundJob, ChannelSetting, Conversation, Customer, CustomerContactPoint, InboundMessage, InputChannel, LLMSettings, MessageAttachment, Order, Product, ProductAlias, ScoringSettings, WhatsAppOrderSession
from app.jobs.service import enqueue_job
from app.master.database import MasterBase
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser, MasterWhatsAppEndpoint
from app.master.service import TenantRole, TenantUser
from app.messages.service import get_or_create_conversation, upsert_inbound_message
from app.tenancy.database import get_tenant_engine
from app.tenancy.migrations import upgrade_tenant_schema
from app.whatsapp.service import (
    WhatsAppEmbeddedSignupError,
    check_whatsapp_connection,
    _download_meta_binary,
    complete_embedded_signup,
    download_whatsapp_media,
    enqueue_whatsapp_processing,
    enqueue_whatsapp_media_download,
    parse_payload_events,
    persist_event,
    redact_whatsapp_config,
    record_manual_response,
    retry_failed_whatsapp_message,
    send_manual_response,
    resolve_company_from_slug,
    resolve_company_from_whatsapp_identifiers,
    verify_signature,
    verify_webhook_token,
    whatsapp_event_has_processable_content,
    whatsapp_event_requires_agent_turn,
    whatsapp_event_matches_config,
    whatsapp_ingress_is_ready,
    whatsapp_outbound_is_ready,
    whatsapp_event_requires_media_download,
    whatsapp_config,
    _whatsapp_media_ready_for_processing,
    mark_whatsapp_media_failed_after_retries,
    recover_stale_whatsapp_outbound_sends,
    _upsert_whatsapp_alert,
)
from app.whatsapp.routes import (
    MAX_WHATSAPP_WEBHOOK_BODY_BYTES,
    _process_whatsapp_jobs_in_background,
    _read_bounded_webhook_body,
    _receive_default_webhook_request,
    _receive_tenant_webhook_request,
)
from app.whatsapp.agent import WhatsAppAgentReplyDeliveryError
from app.whatsapp.conversation_semantics import WhatsAppAgentTurnResult
from app.whatsapp.conversation_orders import has_explicit_order_close
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
        get_tenant_engine(f"sqlite:///{self.tenant_path.as_posix()}").dispose()
        get_tenant_engine.cache_clear()
        self.master_engine.dispose()
        self.tenant_engine.dispose()
        self.tempdir.cleanup()

    def _seed_master(self):
        db = self.MasterSession()
        company = MasterCompany(id=1, name="WhatsApp Demo", slug="whatsapp-demo", legal_name="WhatsApp Demo SL", active=True)
        user = MasterUser(id=1, email="admin@anchi.local", full_name="Admin", password_hash="hash", is_active=True)
        membership = CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=True)
        tenant = MasterTenantDatabase(company_id=1, database_key="whatsapp-demo", database_url=f"sqlite:///{self.tenant_path.as_posix()}", is_active=True, health_status="ok")
        endpoint = MasterWhatsAppEndpoint(company_id=1, phone_number_id="pn-123", business_account_id="ba-123", active=True)
        db.add_all([company, user, membership, tenant, endpoint])
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
            "connection_status": "connected",
            "webhook_enabled": "true",
            "bot_debounce_seconds": "0",
            "default_language": "es",
            "timezone": "Europe/Madrid",
        }.items():
            db.add(ChannelSetting(company_id=1, channel_id=channel.id, key=key, value=value, value_type="string", is_secret=key in {"access_token", "verify_token"}))
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

    @staticmethod
    def _complete_agent_turn():
        return WhatsAppAgentTurnResult(
            intent="order",
            action="respond",
            draft={
                "customer_name": "Cliente WhatsApp SL",
                "lines": [
                    {
                        "product_query": "Producto WhatsApp",
                        "reference": "P-100",
                        "quantity": 5,
                        "unit": "uds",
                        "notes": "",
                    }
                ],
                "notes": "",
            },
            missing_or_uncertain=[],
            reply_needed=False,
            suggested_reply="",
            confidence=0.99,
            evidence_ids=["product:1"],
            close_requested=True,
        )

    def test_verification_and_signature(self):
        db = self.TenantSession()
        config = whatsapp_config(db, 1)
        channel = db.scalar(select(InputChannel).where(InputChannel.company_id == 1, InputChannel.key == "whatsapp"))
        self.assertTrue(channel.supports_documents)
        self.assertTrue(channel.supports_images)
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
        self.assertNotIn("app_secret", redacted)

    def test_connection_health_validates_the_configured_meta_assets(self):
        settings = SimpleNamespace(meta_graph_api_version="v24.0", meta_request_timeout_seconds=5)

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/ba-123"):
                return httpx.Response(200, json={"id": "ba-123", "name": "WhatsApp Demo", "business_verification_status": "verified"})
            if path.endswith("/ba-123/phone_numbers"):
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "pn-123",
                                "display_phone_number": "+1 555 991 7479",
                                "verified_name": "WhatsApp Demo",
                                "quality_rating": "GREEN",
                                "code_verification_status": "VERIFIED",
                            }
                        ]
                    },
                )
            if path.endswith("/pn-123"):
                return httpx.Response(200, json={"id": "pn-123", "status": "CONNECTED", "platform_type": "CLOUD_API", "name_status": "APPROVED"})
            return httpx.Response(404, json={"error": {"message": "unexpected test request"}})

        async def execute_check():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.TenantSession() as db:
                    return await check_whatsapp_connection(db, 1, client=client)

        with patch("app.whatsapp.service.get_settings", return_value=settings):
            result = asyncio.run(execute_check())

        self.assertTrue(result["ok"])
        self.assertEqual(result["meta_phone_number_id"], "pn-123")
        self.assertEqual(result["meta_display_phone_number"], "+1 555 991 7479")
        self.assertEqual(result["display_name_status"], "APPROVED")
        self.assertTrue(all(result["checks"].values()))

    def test_connection_health_flags_a_pending_display_name(self):
        settings = SimpleNamespace(meta_graph_api_version="v24.0", meta_request_timeout_seconds=5)

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/ba-123"):
                return httpx.Response(200, json={"id": "ba-123", "name": "WhatsApp Demo"})
            if path.endswith("/ba-123/phone_numbers"):
                return httpx.Response(200, json={"data": [{"id": "pn-123", "display_phone_number": "+1 555 991 7479"}]})
            if path.endswith("/pn-123"):
                return httpx.Response(200, json={"id": "pn-123", "status": "CONNECTED", "name_status": "PENDING_REVIEW"})
            return httpx.Response(404, json={"error": {"message": "unexpected test request"}})

        async def execute_check():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.TenantSession() as db:
                    return await check_whatsapp_connection(db, 1, client=client)

        with patch("app.whatsapp.service.get_settings", return_value=settings):
            result = asyncio.run(execute_check())

        self.assertFalse(result["ok"])
        self.assertEqual(result["display_name_status"], "PENDING_REVIEW")
        self.assertIn("display_name", result["failed_checks"])

    def test_parser_ignores_message_events_without_provider_ids(self):
        payload = {
            "entry": [
                {
                    "id": "ba-123",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {
                                    "phone_number_id": "pn-123",
                                    "business_account_id": "ba-123",
                                },
                                "messages": [
                                    {"from": "+34600000000", "type": "text", "text": {"body": "sin id"}},
                                    {"id": "wa-valid-id", "from": "+34600000000", "type": "text", "text": {"body": "válido"}},
                                ],
                                "statuses": [{"status": "delivered"}],
                                "message_echoes": [{"to": "+34600000000", "type": "text", "text": {"body": "sin id"}}],
                            },
                        }
                    ],
                }
            ]
        }

        events = parse_payload_events(payload)

        self.assertEqual([event["external_id"] for event in events], ["wa-valid-id"])

    def test_interactive_replies_are_normalized_into_agent_input(self):
        payload = {
            "entry": [
                {
                    "id": "ba-123",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": "pn-123"},
                                "messages": [
                                    {
                                        "id": "wa-list-reply-1",
                                        "from": "+34600000000",
                                        "type": "interactive",
                                        "interactive": {
                                            "type": "list_reply",
                                            "list_reply": {
                                                "id": "catalog-box-10",
                                                "title": "Caja ecológica",
                                                "description": "Caja de 10 unidades",
                                            },
                                        },
                                    },
                                    {
                                        "id": "wa-button-reply-1",
                                        "from": "+34600000000",
                                        "type": "button",
                                        "button": {"text": "Confirmar 3 cajas", "payload": "confirm_3"},
                                    },
                                ],
                            },
                        }
                    ],
                }
            ]
        }

        events = parse_payload_events(payload)

        self.assertEqual(len(events), 2)
        self.assertIn("Caja ecológica", events[0]["text_content"])
        self.assertIn("Caja de 10 unidades", events[0]["text_content"])
        self.assertTrue(whatsapp_event_requires_agent_turn(events[0]))
        self.assertIn("Confirmar 3 cajas", events[1]["text_content"])
        self.assertTrue(whatsapp_event_requires_agent_turn(events[1]))

    def test_product_enquiry_context_is_passed_to_the_agent(self):
        payload = {
            "entry": [
                {
                    "id": "ba-123",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": "pn-123"},
                                "messages": [
                                    {
                                        "id": "wa-product-enquiry-1",
                                        "from": "+34600000000",
                                        "type": "text",
                                        "text": {"body": "¿Tenéis disponibilidad de este producto?"},
                                        "context": {
                                            "from": "+34910000000",
                                            "id": "wa-product-card-1",
                                            "referred_product": {
                                                "catalog_id": "external-catalog",
                                                "product_retailer_id": "BOX-ECO-10",
                                            },
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                }
            ]
        }

        event = parse_payload_events(payload)[0]

        self.assertIn("¿Tenéis disponibilidad de este producto?", event["text_content"])
        self.assertIn('referencia "BOX-ECO-10"', event["text_content"])
        self.assertTrue(whatsapp_event_requires_agent_turn(event))

    def test_catalog_order_is_queued_as_agent_input_without_trusting_catalog_price(self):
        payload = {
            "entry": [
                {
                    "id": "ba-123",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": "pn-123"},
                                "messages": [
                                    {
                                        "id": "wa-catalog-order-1",
                                        "from": "+34600000000",
                                        "type": "order",
                                        "order": {
                                            "catalog_id": "external-catalog",
                                            "text": "Para el viernes, por favor",
                                            "product_items": [
                                                {
                                                    "product_retailer_id": "BOX-ECO-10",
                                                    "quantity": "3",
                                                    "item_price": "999.99",
                                                    "currency": "EUR",
                                                }
                                            ],
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                }
            ]
        }

        event = parse_payload_events(payload)[0]
        self.assertTrue(whatsapp_event_requires_agent_turn(event))
        self.assertIn('referencia de catálogo "BOX-ECO-10"', event["text_content"])
        self.assertIn("cantidad solicitada: 3", event["text_content"])
        self.assertIn("Para el viernes, por favor", event["text_content"])
        self.assertNotIn("999.99", event["text_content"])

        with self.TenantSession() as db:
            inbound = persist_event(db, 1, event)
            job = enqueue_whatsapp_processing(db, 1, inbound.id)
            duplicate = persist_event(db, 1, event)
            duplicate_job = enqueue_whatsapp_processing(db, 1, duplicate.id)
            self.assertEqual(inbound.id, duplicate.id)
            self.assertEqual(job.id, duplicate_job.id)
            self.assertEqual(
                db.scalar(
                    select(func.count())
                    .select_from(BackgroundJob)
                    .where(BackgroundJob.job_type == "process_inbound_message")
                ),
                1,
            )

    def test_parser_normalizes_meta_wa_id_for_customer_resolution_and_replies(self):
        payload = {
            "entry": [
                {
                    "id": "ba-123",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {
                                    "phone_number_id": "pn-123",
                                    "display_phone_number": "34910000000",
                                },
                                "contacts": [{"wa_id": "34600000000"}],
                                "messages": [
                                    {
                                        "id": "wa-no-plus-1",
                                        "from": "34600000000",
                                        "type": "text",
                                        "text": {"body": "Hola, envío pedido de 5 unidades de P-100."},
                                    }
                                ],
                                "message_echoes": [
                                    {
                                        "id": "wa-no-plus-echo-1",
                                        "to": "34600000000",
                                        "type": "text",
                                        "text": {"body": "Respuesta"},
                                    }
                                ],
                            },
                        }
                    ],
                }
            ]
        }

        events = parse_payload_events(payload)
        event = events[0]

        self.assertEqual(event["sender"], "+34600000000")
        self.assertEqual(event["external_thread_id"], "+34600000000")
        self.assertEqual(event["recipients"], ["+34910000000"])
        self.assertEqual(events[1]["recipients"], ["+34600000000"])

        db = self.TenantSession()
        message = persist_event(db, 1, event)
        self.assertEqual(message.sender, "+34600000000")

        classification = json.dumps(
            {"tipo_correo": "pedido", "confianza": 0.97, "motivo": "Pedido claro"},
            ensure_ascii=False,
        )
        extraction = json.dumps(
            {
                "cliente": {
                    "nombre_detectado": "Cliente WhatsApp SL",
                    "codigo_cliente_detectado": "C001",
                },
                "pedido": {
                    "lineas": [
                        {
                            "texto_original": "5 unidades de P-100",
                            "referencia_detectada": "P-100",
                            "producto_detectado": "Producto WhatsApp",
                            "cantidad": 5,
                            "unidad": "uds",
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
            result = UnifiedOrderPipelineService().process_inbound_message(db, message)

        self.assertTrue(result["ok"])
        order = db.get(Order, result["order_id"])
        self.assertIsNotNone(order)
        self.assertEqual(order.customer_id, 1)
        db.close()

    def test_pipeline_marks_meta_messages_as_whatsapp_source(self):
        db = self.TenantSession()
        message = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="whatsapp",
            provider="meta",
            external_id="wa-source-type-1",
            sender="+34600000000",
            recipients=["+34910000000"],
            text_content="Pedido de prueba",
            external_thread_id="+34600000000",
            content_type="text",
        )[0]

        self.assertEqual(UnifiedOrderPipelineService()._source_type_for_extraction(message), "whatsapp")
        db.close()

    def test_outbound_requires_an_active_connected_channel(self):
        db = self.TenantSession()
        channel = db.scalar(select(InputChannel).where(InputChannel.company_id == 1, InputChannel.key == "whatsapp"))

        self.assertTrue(whatsapp_outbound_is_ready(db, 1))
        channel.is_active = False
        db.commit()
        self.assertFalse(whatsapp_outbound_is_ready(db, 1))
        db.close()

    def test_live_webhook_requires_ready_tenant_and_exact_meta_identifiers(self):
        db = self.TenantSession()
        config = whatsapp_config(db, 1)
        self.assertTrue(whatsapp_ingress_is_ready(db, 1, config=config))
        self.assertTrue(
            whatsapp_event_matches_config(
                {"business_account_id": "ba-123", "phone_number_id": "pn-123"},
                config,
            )
        )
        self.assertFalse(
            whatsapp_event_matches_config(
                {"business_account_id": "ba-other", "phone_number_id": "pn-123"},
                config,
            )
        )
        channel = db.scalar(select(InputChannel).where(InputChannel.company_id == 1, InputChannel.key == "whatsapp"))
        channel.is_active = False
        db.flush()
        self.assertFalse(whatsapp_ingress_is_ready(db, 1, config=config))
        db.close()

    def test_webhook_body_reader_rejects_oversized_chunked_payload(self):
        payload = b"x" * (MAX_WHATSAPP_WEBHOOK_BODY_BYTES + 1)
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/webhooks/whatsapp",
            "raw_path": b"/webhooks/whatsapp",
            "query_string": b"",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 443),
        }

        async def receive():
            return {"type": "http.request", "body": payload, "more_body": False}

        body, observed_size = asyncio.run(
            _read_bounded_webhook_body(Request(scope, receive=receive))
        )

        self.assertIsNone(body)
        self.assertEqual(observed_size, MAX_WHATSAPP_WEBHOOK_BODY_BYTES + 1)

    def test_both_webhook_routes_reject_oversized_declared_payload(self):
        declared_size = MAX_WHATSAPP_WEBHOOK_BODY_BYTES + 1

        def make_request(path: str) -> Request:
            raw_path = path.encode("ascii")
            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "https",
                "path": path,
                "raw_path": raw_path,
                "query_string": b"",
                "headers": [
                    (b"host", b"testserver"),
                    (b"content-length", str(declared_size).encode("ascii")),
                ],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 443),
            }

            async def receive():
                raise AssertionError("An oversized declared body must be rejected before reading it")

            return Request(scope, receive=receive)

        master_db = self.MasterSession()
        try:
            default_response = asyncio.run(
                _receive_default_webhook_request(
                    make_request("/webhooks/whatsapp"),
                    None,
                    master_db,
                    BackgroundTasks(),
                )
            )
            tenant_response = asyncio.run(
                _receive_tenant_webhook_request(
                    "whatsapp-demo",
                    make_request("/webhooks/whatsapp/whatsapp-demo"),
                    None,
                    master_db,
                    BackgroundTasks(),
                )
            )
        finally:
            master_db.close()

        for response in (default_response, tenant_response):
            self.assertEqual(response.status_code, 413)
            self.assertEqual(json.loads(response.body)["message"], "payload too large")

    def test_account_update_without_phone_id_controls_channel_lifecycle(self):
        db = self.TenantSession()
        channel = db.scalar(select(InputChannel).where(InputChannel.company_id == 1, InputChannel.key == "whatsapp"))
        db.add(
            ChannelSetting(
                company_id=1,
                channel_id=channel.id,
                key="display_phone_number",
                value="+34 610 000 000",
                value_type="string",
            )
        )
        db.commit()
        config = whatsapp_config(db, 1)
        payload = {
            "entry": [
                {
                    "id": "ba-123",
                    "changes": [
                        {
                            "field": "account_update",
                            "value": {
                                "phone_number": "+34610000000",
                                "event": "PARTNER_REMOVED",
                            },
                        }
                    ],
                }
            ]
        }

        event = parse_payload_events(payload)[0]

        self.assertEqual(event["kind"], "account_update")
        self.assertIsNone(event["phone_number_id"])
        self.assertEqual(event["phone_number"], "+34610000000")
        self.assertTrue(whatsapp_event_matches_config(event, config))
        persist_event(db, 1, event)
        db.refresh(channel)
        self.assertFalse(channel.is_active)
        self.assertEqual(whatsapp_config(db, 1).connection_status, "disconnected")

        reconnect_payload = {
            "entry": [
                {
                    "id": "ba-123",
                    "changes": [
                        {
                            "field": "account_update",
                            "value": {
                                "phone_number": "+34610000000",
                                "event": "ACCOUNT_RECONNECTED",
                            },
                        }
                    ],
                }
            ]
        }
        persist_event(db, 1, parse_payload_events(reconnect_payload)[0])
        db.refresh(channel)
        self.assertTrue(channel.is_active)
        self.assertEqual(whatsapp_config(db, 1).connection_status, "connected")
        db.close()

    def test_text_message_dedupes_and_queues_job(self):
        payload = {
            "entry": [
                {
                    "id": "12345678901",
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
        self.assertEqual(events[0]["business_account_id"], "12345678901")
        db = self.TenantSession()
        message = persist_event(db, 1, events[0])
        enqueue_whatsapp_processing(db, 1, message.id)
        duplicate = persist_event(db, 1, events[0])
        db.commit()
        self.assertEqual(message.id, duplicate.id)
        self.assertEqual(db.scalar(select(func.count()).select_from(InboundMessage)) or 0, 1)
        self.assertEqual(db.scalar(select(func.count()).select_from(BackgroundJob)) or 0, 1)
        db.close()

    def test_first_live_inbound_enables_bot_but_history_and_duplicates_do_not(self):
        live_payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "pn-123"},
                                "messages": [
                                    {
                                        "id": "wa-first-live-chat-bot-default",
                                        "from": "+34600000002",
                                        "type": "text",
                                        "text": {"body": "Quiero hacer un pedido"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        live_event = parse_payload_events(live_payload)[0]
        with self.TenantSession() as db:
            live_message = persist_event(db, 1, live_event)
            live_conversation = db.get(Conversation, live_message.conversation_id)
            self.assertTrue(live_conversation.whatsapp_bot_enabled)

            live_conversation.whatsapp_bot_enabled = False
            db.commit()
            duplicate = persist_event(db, 1, live_event)
            db.refresh(live_conversation)
            self.assertEqual(duplicate.id, live_message.id)
            self.assertFalse(live_conversation.whatsapp_bot_enabled)

            followup_event = {
                **live_event,
                "external_id": "wa-first-live-chat-followup",
                "text_content": "Añado otra línea al pedido",
            }
            followup = persist_event(db, 1, followup_event)
            db.refresh(live_conversation)
            self.assertEqual(followup.conversation_id, live_conversation.id)
            self.assertFalse(live_conversation.whatsapp_bot_enabled)

            history_event = {
                "kind": "history_message",
                "external_id": "wa-imported-history-chat-bot-default",
                "sender": "+34600000003",
                "recipients": ["+34610000000"],
                "external_thread_id": "+34600000003",
                "direction": "inbound",
                "message_type": "text",
                "text_content": "Mensaje antiguo importado",
                "metadata": {"payload": {}},
            }
            history_message = persist_event(db, 1, history_event)
            history_conversation = db.get(Conversation, history_message.conversation_id)
            self.assertFalse(history_conversation.whatsapp_bot_enabled)

            echo_event = {
                "kind": "message_echo",
                "external_id": "wa-outbound-first-chat-bot-default",
                "sender": "+34610000000",
                "recipients": ["+34600000004"],
                "external_thread_id": "+34600000004",
                "direction": "outbound",
                "message_type": "text",
                "text_content": "Te escribimos desde la empresa",
                "metadata": {"payload": {}},
            }
            echo_message = persist_event(db, 1, echo_event)
            echo_conversation = db.get(Conversation, echo_message.conversation_id)
            self.assertFalse(echo_conversation.whatsapp_bot_enabled)


    def test_webhook_schedules_immediate_processing_for_durable_text_job(self):
        payload = {
            "entry": [
                {
                    "id": "ba-123",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": "pn-123", "business_account_id": "ba-123"},
                                "messages": [
                                    {
                                        "id": "wa-webhook-immediate-1",
                                        "from": "+34600000000",
                                        "type": "text",
                                        "text": {"body": "Quiero 4 cajas de Producto WhatsApp"},
                                    }
                                ],
                            },
                        }
                    ],
                }
            ]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        secret = "synthetic-webhook-secret"
        signature = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/webhooks/whatsapp/whatsapp-demo",
            "raw_path": b"/webhooks/whatsapp/whatsapp-demo",
            "query_string": b"",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 443),
        }

        async def receive():
            return {"type": "http.request", "body": raw_body, "more_body": False}

        request = Request(scope, receive=receive)
        tasks = BackgroundTasks()
        master_db = self.MasterSession()
        try:
            with patch("app.whatsapp.routes.get_settings") as settings_getter:
                settings_getter.return_value.meta_app_secret = secret
                result = asyncio.run(
                    _receive_tenant_webhook_request(
                        "whatsapp-demo",
                        request,
                        signature,
                        master_db,
                        tasks,
                    )
                )
        finally:
            master_db.close()

        self.assertEqual(result["stored"], [1])
        scheduled = [task for task in tasks.tasks if task.func is _process_whatsapp_jobs_in_background]
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0].args[0], 1)
        self.assertEqual(len(scheduled[0].args[1]), 1)
        scheduled_job_id = scheduled[0].args[1][0][0]
        db = self.TenantSession()
        queued = db.get(BackgroundJob, scheduled_job_id)
        self.assertEqual(queued.job_type, "process_inbound_message")
        self.assertEqual(queued.status, "queued")
        db.close()

    def test_default_webhook_also_schedules_immediate_processing(self):
        payload = {
            "entry": [
                {
                    "id": "ba-123",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {
                                    "phone_number_id": "pn-123",
                                    "business_account_id": "ba-123",
                                },
                                "messages": [
                                    {
                                        "id": "wa-default-immediate-1",
                                        "from": "+34600000000",
                                        "type": "text",
                                        "text": {"body": "Necesito 3 cajas de prueba"},
                                    },
                                    {
                                        "id": "wa-default-media-immediate-1",
                                        "from": "+34600000000",
                                        "type": "document",
                                        "document": {
                                            "id": "media-default-123",
                                            "filename": "pedido.pdf",
                                            "mime_type": "application/pdf",
                                        },
                                    },
                                ],
                            },
                        }
                    ],
                }
            ]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        secret = "synthetic-webhook-secret"
        signature = "sha256=" + hmac.new(
            secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/webhooks/whatsapp",
            "raw_path": b"/webhooks/whatsapp",
            "query_string": b"",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 443),
        }

        async def receive():
            return {"type": "http.request", "body": raw_body, "more_body": False}

        request = Request(scope, receive=receive)
        tasks = BackgroundTasks()
        master_db = self.MasterSession()
        try:
            with patch("app.whatsapp.routes.get_settings") as settings_getter:
                settings_getter.return_value.meta_app_secret = secret
                result = asyncio.run(
                    _receive_default_webhook_request(
                        request,
                        signature,
                        master_db,
                        tasks,
                    )
                )
        finally:
            master_db.close()

        self.assertEqual(result["stored"], [1, 2])
        scheduled = [
            task
            for task in tasks.tasks
            if task.func is _process_whatsapp_jobs_in_background
        ]
        self.assertEqual(len(scheduled), 2)
        scheduled_jobs = []
        for task in scheduled:
            self.assertEqual(task.args[0], 1)
            self.assertEqual(len(task.args[1]), 1)
            scheduled_jobs.append(task.args[1][0][0])
        with self.TenantSession() as db:
            queued = [db.get(BackgroundJob, job_id) for job_id in scheduled_jobs]
            self.assertEqual(
                {job.job_type for job in queued if job is not None},
                {"process_inbound_message", "download_whatsapp_media"},
            )
            self.assertTrue(all(job.status == "queued" for job in queued if job is not None))

    def test_media_webhook_runs_the_durable_download_job_immediately(self):
        payload = {
            "entry": [
                {
                    "id": "ba-123",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": "pn-123", "business_account_id": "ba-123"},
                                "messages": [
                                    {
                                        "id": "wa-webhook-media-immediate-1",
                                        "from": "+34600000000",
                                        "type": "document",
                                        "document": {
                                            "id": "media-123",
                                            "filename": "pedido.pdf",
                                            "mime_type": "application/pdf",
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                }
            ]
        }
        raw_body = json.dumps(payload).encode("utf-8")
        secret = "synthetic-webhook-secret"
        signature = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/webhooks/whatsapp/whatsapp-demo",
            "raw_path": b"/webhooks/whatsapp/whatsapp-demo",
            "query_string": b"",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 443),
        }

        async def receive():
            return {"type": "http.request", "body": raw_body, "more_body": False}

        request = Request(scope, receive=receive)
        tasks = BackgroundTasks()
        master_db = self.MasterSession()
        try:
            with patch("app.whatsapp.routes.get_settings") as settings_getter:
                settings_getter.return_value.meta_app_secret = secret
                result = asyncio.run(
                    _receive_tenant_webhook_request(
                        "whatsapp-demo",
                        request,
                        signature,
                        master_db,
                        tasks,
                    )
                )
        finally:
            master_db.close()

        self.assertEqual(result["stored"], [1])
        scheduled = [task for task in tasks.tasks if task.func is _process_whatsapp_jobs_in_background]
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0].args[0], 1)
        self.assertEqual(len(scheduled[0].args[1]), 1)
        scheduled_job_id = scheduled[0].args[1][0][0]
        with self.TenantSession() as db:
            queued = db.get(BackgroundJob, scheduled_job_id)
            self.assertIsNotNone(queued)
            self.assertEqual(queued.job_type, "download_whatsapp_media")
            self.assertEqual(queued.status, "queued")

    def test_immediate_job_runner_respects_short_debounce_and_bounds_waits(self):
        from datetime import timedelta

        due_at = datetime.now(timezone.utc) + timedelta(seconds=2)
        retry_due_at = datetime.now(timezone.utc) + timedelta(seconds=60)
        with patch("app.workers.jobs_worker.run_worker_cycle", return_value={"processed": 1}) as worker, patch(
            "app.whatsapp.routes.time.sleep"
        ) as sleep:
            _process_whatsapp_jobs_in_background(
                1,
                [(42, due_at), (43, retry_due_at)],
            )

        self.assertEqual(worker.call_count, 1)
        self.assertEqual(worker.call_args.kwargs, {"max_jobs": 1, "company_id": 1, "job_id": 42})
        self.assertGreater(sleep.call_args.args[0], 0)
        self.assertLessEqual(sleep.call_args.args[0], 2)

    def test_media_processing_can_be_enqueued_again_after_extraction_finishes(self):
        db = self.TenantSession()
        channel = db.scalar(
            select(InputChannel).where(
                InputChannel.company_id == 1,
                InputChannel.key == "whatsapp",
            )
        )
        message = InboundMessage(
            company_id=1,
            channel_id=channel.id,
            provider="meta",
            source_external_id="wa-media-state-retry-1",
            direction="inbound",
            status="received",
            original_content="Adjunto pedido",
            received_at=datetime.now(timezone.utc),
        )
        db.add(message)
        db.flush()
        attachment = MessageAttachment(
            company_id=1,
            inbound_message_id=message.id,
            filename="pedido.pdf",
            content_type="application/pdf",
            size_bytes=100,
            extraction_status="pending",
        )
        db.add(attachment)
        db.commit()

        pending_job = enqueue_whatsapp_processing(db, 1, message.id)
        attachment.extracted_text = "4 cajas de Caja eco"
        attachment.extraction_status = "extracted"
        db.commit()
        ready_job = enqueue_whatsapp_processing(db, 1, message.id)

        self.assertNotEqual(pending_job.id, ready_job.id)
        self.assertEqual(
            db.scalar(
                select(func.count())
                .select_from(BackgroundJob)
                .where(BackgroundJob.job_type == "process_inbound_message")
            ),
            2,
        )
        db.close()

    def test_media_download_refreshes_attachment_after_another_worker_finishes(self):
        payload = {
            "entry": [
                {
                    "id": "ba-123",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": "pn-123"},
                                "messages": [
                                    {
                                        "id": "wa-media-concurrent-1",
                                        "from": "+34600000000",
                                        "type": "document",
                                        "document": {
                                            "id": "media-concurrent-1",
                                            "filename": "pedido.pdf",
                                            "mime_type": "application/pdf",
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                }
            ]
        }
        first_db = self.TenantSession()
        message = persist_event(first_db, 1, parse_payload_events(payload)[0])
        stale_attachment = message.attachments[0]

        second_db = self.TenantSession()
        completed_attachment = second_db.get(MessageAttachment, stale_attachment.id)
        completed_attachment.storage_path = "already-downloaded/pedido.pdf"
        completed_attachment.extraction_status = "extracted"
        completed_attachment.extracted_text = "2 cajas del producto P-100"
        second_db.commit()
        second_db.close()

        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(500, json={"error": "no network request expected"})

        async def run_download():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await download_whatsapp_media(
                    first_db,
                    company_id=1,
                    inbound_message_id=message.id,
                    client=client,
                )

        result = asyncio.run(run_download())
        self.assertEqual(result["downloaded"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertTrue(result["ready_for_processing"])
        self.assertEqual(request_count, 0)
        first_db.close()

    def test_duplicate_insert_race_reuses_existing_message(self):
        payload = {
            "entry": [
                {
                    "id": "ba-123",
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "pn-123"},
                                "messages": [
                                    {
                                        "id": "wa-race-1",
                                        "from": "+34600000000",
                                        "type": "text",
                                        "text": {"body": "Pedido concurrente"},
                                    }
                                ],
                            }
                        }
                    ],
                }
            ]
        }
        event = parse_payload_events(payload)[0]
        first_db = self.TenantSession()
        first = persist_event(first_db, 1, event)
        first_id = first.id
        first_db.close()

        second_db = self.TenantSession()
        try:
            duplicate_error = IntegrityError("INSERT", {"source_external_id": "wa-race-1"}, Exception("duplicate"))
            with patch("app.whatsapp.service.persist_normalized_message", side_effect=duplicate_error):
                duplicate = persist_event(second_db, 1, event)

            self.assertEqual(first_id, duplicate.id)
            self.assertEqual(second_db.scalar(select(func.count()).select_from(InboundMessage)), 1)
        finally:
            second_db.close()

    def test_concurrent_new_messages_reuse_the_winning_conversation(self):
        first_db = self.TenantSession()
        channel = first_db.scalar(
            select(InputChannel).where(
                InputChannel.company_id == 1,
                InputChannel.key == "whatsapp",
            )
        )
        channel_id = channel.id
        thread_key = "+34600000077"
        second_db = self.TenantSession()
        winner_ids: list[int] = []
        original_flush = first_db.flush
        conversation_id = None
        bot_enabled = False

        def flush_with_competing_insert(*args, **kwargs):
            if not winner_ids:
                winner = Conversation(
                    company_id=1,
                    channel_id=channel.id,
                    provider="meta",
                    external_thread_id=thread_key,
                    status="open",
                    whatsapp_bot_enabled=True,
                )
                second_db.add(winner)
                second_db.commit()
                winner_ids.append(winner.id)
            return original_flush(*args, **kwargs)

        try:
            with patch.object(first_db, "flush", side_effect=flush_with_competing_insert):
                conversation = get_or_create_conversation(
                    first_db,
                    company_id=1,
                    channel_id=channel_id,
                    provider="meta",
                    external_thread_id=thread_key,
                    whatsapp_bot_enabled_on_create=True,
                )
            first_db.commit()
            conversation_id = conversation.id
            bot_enabled = conversation.whatsapp_bot_enabled
        finally:
            first_db.close()
            second_db.close()

        with self.TenantSession() as verify_db:
            conversations = verify_db.scalars(
                select(Conversation).where(
                    Conversation.company_id == 1,
                    Conversation.channel_id == channel_id,
                    Conversation.provider == "meta",
                    Conversation.external_thread_id == thread_key,
                )
            ).all()
        self.assertEqual(conversation_id, winner_ids[0])
        self.assertEqual(len(conversations), 1)
        self.assertTrue(bot_enabled)

    def test_repeated_media_webhook_does_not_duplicate_attachments(self):
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "pn-123", "business_account_id": "ba-123"},
                                "messages": [
                                    {
                                        "id": "wa-media-1",
                                        "from": "+34600000000",
                                        "type": "document",
                                        "document": {
                                            "id": "media-1",
                                            "filename": "pedido.pdf",
                                            "mime_type": "application/pdf",
                                            "file_size": 1024,
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        event = parse_payload_events(payload)[0]
        self.assertTrue(event["attachments"][0]["downloadable"])
        db = self.TenantSession()
        first = persist_event(db, 1, event)
        second = persist_event(db, 1, event)
        self.assertEqual(first.id, second.id)
        self.assertEqual(
            db.scalar(select(func.count()).select_from(MessageAttachment).where(MessageAttachment.inbound_message_id == first.id)),
            1,
        )
        db.close()

    def test_duplicate_message_does_not_regress_processed_state(self):
        payload = {
            "entry": [
                {
                    "id": "ba-123",
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "pn-123"},
                                "messages": [
                                    {
                                        "id": "wa-state-1",
                                        "from": "+34600000000",
                                        "type": "text",
                                        "text": {"body": "Pedido"},
                                    }
                                ],
                            }
                        }
                    ],
                }
            ]
        }
        event = parse_payload_events(payload)[0]
        db = self.TenantSession()
        message = persist_event(db, 1, event)
        message.status = "order_detected"
        message.processing_step = "completed"
        message.last_processed_at = datetime(2026, 8, 30, tzinfo=timezone.utc)
        db.commit()

        duplicate = persist_event(db, 1, event)

        self.assertEqual(duplicate.status, "order_detected")
        self.assertEqual(duplicate.processing_step, "completed")
        self.assertEqual(duplicate.last_processed_at.replace(tzinfo=None), datetime(2026, 8, 30))
        db.close()

    def test_media_policy_allows_images_and_rejects_unsupported_documents(self):
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "pn-123", "business_account_id": "ba-123"},
                                "messages": [
                                    {"id": "wa-image-1", "from": "+34600000000", "type": "image", "image": {"id": "image-1", "mime_type": "image/jpeg"}},
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        event = parse_payload_events(payload)[0]
        self.assertTrue(event["attachments"][0]["downloadable"])

        legacy_doc_payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "pn-123", "business_account_id": "ba-123"},
                                "messages": [
                                    {
                                        "id": "wa-legacy-doc-1",
                                        "from": "+34600000000",
                                        "type": "document",
                                        "document": {
                                            "id": "legacy-doc-1",
                                            "filename": "pedido.doc",
                                            "mime_type": "application/msword",
                                        },
                                    },
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        legacy_doc_event = parse_payload_events(legacy_doc_payload)[0]
        self.assertFalse(legacy_doc_event["attachments"][0]["downloadable"])
        self.assertTrue(whatsapp_event_requires_agent_turn(legacy_doc_event))
        db = self.TenantSession()
        persisted = persist_event(db, 1, legacy_doc_event)
        attachment = db.scalar(
            select(MessageAttachment).where(MessageAttachment.inbound_message_id == persisted.id)
        )
        self.assertEqual(attachment.extraction_status, "unsupported")
        self.assertIn("no se puede interpretar", attachment.extraction_error or "")
        db.close()

    def test_image_media_is_downloaded_and_queued_for_human_handoff(self):
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "pn-123", "business_account_id": "ba-123"},
                                "messages": [
                                    {
                                        "id": "wa-image-download-1",
                                        "from": "+34600000000",
                                        "type": "image",
                                        "image": {"id": "image-download-1", "mime_type": "image/jpeg", "file_size": 4},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        db = self.TenantSession()
        message = persist_event(db, 1, parse_payload_events(payload)[0])

        def handler(request):
            if request.url.host == "graph.facebook.com":
                return httpx.Response(
                    200,
                    json={
                        "url": "https://lookaside.fbsbx.com/image-download-1",
                        "mime_type": "image/jpeg",
                        "file_size": 4,
                    },
                )
            if request.url.host == "lookaside.fbsbx.com":
                return httpx.Response(200, content=b"jpeg")
            return httpx.Response(404, json={"error": {"message": "unexpected request"}})

        storage_root = Path(self.tempdir.name) / "image-storage"
        with patch.dict(os.environ, {"TEMP_STORAGE_DIR": str(storage_root)}):
            async def run_download():
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    return await download_whatsapp_media(db, company_id=1, inbound_message_id=message.id, client=client)

            result = asyncio.run(run_download())

        attachment = db.scalar(select(MessageAttachment).where(MessageAttachment.inbound_message_id == message.id))
        self.assertEqual(result["downloaded"], 1)
        self.assertTrue(result["ready_for_processing"])
        self.assertEqual(attachment.extraction_status, "stored")
        self.assertIsNone(attachment.extracted_text)
        self.assertTrue(Path(attachment.storage_path).is_file())
        db.close()

    def test_media_download_worker_enqueues_only_when_media_is_ready_for_agent(self):
        from app.workers.jobs_worker import _process_job

        job = SimpleNamespace(
            job_type="download_whatsapp_media",
            company_id=1,
            payload_json=json.dumps({"inbound_message_id": 44}),
            created_by_user_id=None,
        )
        db = self.TenantSession()
        with patch(
            "app.whatsapp.service.download_whatsapp_media",
            new=AsyncMock(return_value={"ok": True, "ready_for_processing": False}),
        ), patch("app.whatsapp.service.enqueue_whatsapp_processing") as enqueue_processing:
            result = _process_job(db, job)

        self.assertIsNone(result["processing_job_id"])
        self.assertIn("sigue pendiente", result["message"])
        enqueue_processing.assert_not_called()
        db.close()

        job.payload_json = json.dumps({"inbound_message_id": 45})
        db = self.TenantSession()
        with patch(
            "app.whatsapp.service.download_whatsapp_media",
            new=AsyncMock(return_value={"ok": True, "ready_for_processing": True}),
        ), patch(
            "app.whatsapp.service.enqueue_whatsapp_processing",
            return_value=SimpleNamespace(id=123),
        ) as enqueue_processing:
            result = _process_job(db, job)

        self.assertEqual(result["processing_job_id"], 123)
        enqueue_processing.assert_called_once_with(db, 1, 45, user_id=None)
        db.close()

    def test_unsupported_attachment_reaches_agent_handoff_after_download_worker(self):
        db = self.TenantSession()
        channel = db.scalar(select(InputChannel).where(InputChannel.company_id == 1, InputChannel.key == "whatsapp"))
        message = InboundMessage(
            company_id=1,
            channel_id=channel.id,
            provider="meta",
            source_external_id="wa-unsupported-ready-1",
            direction="inbound",
            status="received",
            original_content=None,
            received_at=datetime.now(timezone.utc),
        )
        db.add(message)
        db.flush()
        db.add(
            MessageAttachment(
                company_id=1,
                inbound_message_id=message.id,
                filename="pedido.doc",
                content_type="application/msword",
                size_bytes=10,
                extraction_status="unsupported",
            )
        )
        db.commit()

        from app.workers.jobs_worker import _process_job

        job = SimpleNamespace(
            job_type="download_whatsapp_media",
            company_id=1,
            payload_json=json.dumps({"inbound_message_id": message.id}),
            created_by_user_id=None,
        )
        with patch(
            "app.whatsapp.service.enqueue_whatsapp_processing",
            return_value=SimpleNamespace(id=123),
        ) as enqueue_processing:
            result = _process_job(db, job)

        self.assertTrue(result["ready_for_processing"])
        self.assertEqual(result["processing_job_id"], 123)
        enqueue_processing.assert_called_once_with(db, 1, message.id, user_id=None)
        db.close()

    def test_storage_error_waits_for_retries_then_becomes_handoff_ready(self):
        db = self.TenantSession()
        channel = db.scalar(select(InputChannel).where(InputChannel.company_id == 1, InputChannel.key == "whatsapp"))
        message = InboundMessage(
            company_id=1,
            channel_id=channel.id,
            provider="meta",
            source_external_id="wa-storage-error-retry-1",
            direction="inbound",
            status="received",
            original_content="Adjunto el pedido",
            received_at=datetime.now(timezone.utc),
        )
        db.add(message)
        db.flush()
        db.add(
            MessageAttachment(
                company_id=1,
                inbound_message_id=message.id,
                filename="pedido.pdf",
                content_type="application/pdf",
                extraction_status="storage_error",
            )
        )
        db.commit()

        self.assertFalse(_whatsapp_media_ready_for_processing(message))
        self.assertTrue(
            mark_whatsapp_media_failed_after_retries(
                db,
                company_id=1,
                inbound_message_id=message.id,
            )
        )
        attachment = db.scalar(select(MessageAttachment).where(MessageAttachment.inbound_message_id == message.id))
        self.assertEqual(attachment.extraction_status, "extraction_error")
        self.assertTrue(_whatsapp_media_ready_for_processing(message))
        db.close()

    def test_image_without_caption_is_stored_but_does_not_enter_order_pipeline(self):
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "pn-123", "business_account_id": "ba-123"},
                                "messages": [
                                    {
                                        "id": "wa-image-no-caption",
                                        "from": "+34600000000",
                                        "type": "image",
                                        "image": {"id": "image-no-caption", "mime_type": "image/jpeg"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }

        event = parse_payload_events(payload)[0]

        self.assertIsNone(event["text_content"])
        self.assertTrue(whatsapp_event_requires_media_download(event))
        self.assertFalse(whatsapp_event_has_processable_content(event))

    def test_image_with_caption_waits_for_media_before_text_pipeline(self):
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "pn-123", "business_account_id": "ba-123"},
                                "messages": [
                                    {
                                        "id": "wa-image-caption",
                                        "from": "+34600000000",
                                        "type": "image",
                                        "image": {"id": "image-caption", "mime_type": "image/jpeg", "caption": "Consulta sobre el pedido"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }

        event = parse_payload_events(payload)[0]

        self.assertTrue(whatsapp_event_requires_media_download(event))
        self.assertFalse(whatsapp_event_has_processable_content(event))

    def test_malformed_media_size_does_not_break_ingestion(self):
        payload = {
            "entry": [
                {
                    "id": "ba-123",
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "pn-123", "business_account_id": "ba-123"},
                                "messages": [
                                    {
                                        "id": "wa-media-size-1",
                                        "from": "+34600000000",
                                        "type": "document",
                                        "document": {
                                            "id": "media-size-1",
                                            "filename": "pedido.pdf",
                                            "mime_type": "application/pdf",
                                            "file_size": "not-a-number",
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                }
            ]
        }

        db = self.TenantSession()
        message = persist_event(db, 1, parse_payload_events(payload)[0])

        self.assertEqual(message.attachments[0].size_bytes, 0)
        db.close()

    def test_media_download_persists_file_with_meta_mock(self):
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "pn-123", "business_account_id": "ba-123"},
                                "messages": [
                                    {
                                        "id": "wa-media-download-1",
                                        "from": "+34600000000",
                                        "type": "document",
                                        "document": {"id": "media-download-1", "filename": "pedido.pdf", "mime_type": "application/pdf", "file_size": 12},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        db = self.TenantSession()
        message = persist_event(db, 1, parse_payload_events(payload)[0])

        def handler(request):
            if request.url.host == "graph.facebook.com" and request.url.path.endswith("/media-download-1"):
                return httpx.Response(200, json={"url": "https://lookaside.facebook.com/media-download-1", "mime_type": "application/pdf", "file_size": 12})
            if request.url.host == "lookaside.facebook.com":
                return httpx.Response(200, content=b"%PDF-1.4\nBT\n(Pedido 10 cajas) Tj\nET")
            return httpx.Response(404, json={"error": {"message": "unexpected request"}})

        storage_root = Path(self.tempdir.name) / "storage"
        with patch.dict(os.environ, {"TEMP_STORAGE_DIR": str(storage_root)}):
            async def run_download():
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    return await download_whatsapp_media(db, company_id=1, inbound_message_id=message.id, client=client)

            result = asyncio.run(run_download())
        attachment = db.scalar(select(MessageAttachment).where(MessageAttachment.inbound_message_id == message.id))
        self.assertEqual(result["downloaded"], 1)
        self.assertEqual(attachment.extraction_status, "extracted")
        self.assertIn("Pedido 10 cajas", attachment.extracted_text or "")
        self.assertTrue(result["ready_for_processing"])
        self.assertTrue(Path(attachment.storage_path).is_file())
        db.close()

    def test_media_download_rejects_mime_mismatch_before_fetching_content(self):
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "pn-123", "business_account_id": "ba-123"},
                                "messages": [
                                    {
                                        "id": "wa-media-mime-mismatch-1",
                                        "from": "+34600000000",
                                        "type": "document",
                                        "document": {
                                            "id": "media-mime-mismatch-1",
                                            "filename": "pedido.pdf",
                                            "mime_type": "application/pdf",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        db = self.TenantSession()
        message = persist_event(db, 1, parse_payload_events(payload)[0])
        content_requests = 0

        def handler(request):
            nonlocal content_requests
            if request.url.host == "graph.facebook.com" and request.url.path.endswith("/media-mime-mismatch-1"):
                return httpx.Response(
                    200,
                    json={
                        "url": "https://lookaside.fbsbx.com/media-mime-mismatch-1",
                        "mime_type": "image/jpeg",
                        "file_size": 12,
                    },
                )
            if request.url.host == "lookaside.fbsbx.com":
                content_requests += 1
                return httpx.Response(200, content=b"not-a-pdf")
            return httpx.Response(404, json={"error": {"message": "unexpected request"}})

        async def run_download():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await download_whatsapp_media(
                    db,
                    company_id=1,
                    inbound_message_id=message.id,
                    client=client,
                )

        result = asyncio.run(run_download())
        attachment = db.scalar(select(MessageAttachment).where(MessageAttachment.inbound_message_id == message.id))
        self.assertEqual(result["downloaded"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertTrue(result["ready_for_processing"])
        self.assertEqual(content_requests, 0)
        self.assertEqual(attachment.extraction_status, "extraction_error")
        self.assertIn("no soportado", attachment.extraction_error or "")
        db.close()

    def test_media_download_rejects_oversized_stream_before_persisting(self):
        def handler(request):
            return httpx.Response(200, content=b"0123456789")

        async def run_download():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await _download_meta_binary(
                    client,
                    url="https://lookaside.fbsbx.com/oversized-media",
                    access_token="token-123",
                    max_bytes=4,
                )

        with self.assertRaises(WhatsAppEmbeddedSignupError) as context:
            asyncio.run(run_download())
        self.assertEqual(context.exception.error_type, "media_too_large")

    def test_media_download_rejects_redirect_response_instead_of_storing_it(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                302,
                headers={"Location": "https://lookaside.fbsbx.com/actual-media"},
                content=b"redirect body is not the media",
            )

        async def run_download():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await _download_meta_binary(
                    client,
                    url="https://lookaside.fbsbx.com/media-redirect",
                    access_token="token-123",
                    max_bytes=1024,
                )

        with self.assertRaises(WhatsAppEmbeddedSignupError) as context:
            asyncio.run(run_download())
        self.assertEqual(context.exception.error_type, "media_download_failed")
        self.assertEqual(len(requests), 1)

    def test_media_retry_preserves_completed_attachments(self):
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "pn-123", "business_account_id": "ba-123"},
                                "messages": [
                                    {
                                        "id": "wa-media-retry-message",
                                        "from": "+34600000000",
                                        "type": "document",
                                        "document": {"id": "media-retry-2", "filename": "primero.txt", "mime_type": "text/plain", "file_size": 7},
                                    },
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        db = self.TenantSession()
        message = persist_event(db, 1, parse_payload_events(payload)[0])
        db.add(
            MessageAttachment(
                company_id=1,
                inbound_message_id=message.id,
                filename="segundo.txt",
                content_type="text/plain",
                size_bytes=7,
                is_pdf=False,
                is_image=False,
                is_audio=False,
                extraction_status="pending",
            )
        )
        db.commit()
        media_requests = 0
        second_media_attempts = 0

        def handler(request):
            nonlocal media_requests, second_media_attempts
            if request.url.host == "graph.facebook.com" and request.url.path.endswith("/media-retry-2"):
                media_requests += 1
                if media_requests == 2 and second_media_attempts == 0:
                    second_media_attempts += 1
                    raise httpx.ReadTimeout("temporary timeout", request=request)
                return httpx.Response(200, json={"url": "https://lookaside.fbsbx.com/media-retry-2", "mime_type": "text/plain", "file_size": 7})
            if request.url.host == "lookaside.fbsbx.com":
                return httpx.Response(200, content=b"pedido")
            return httpx.Response(404, json={"error": {"message": "unexpected request"}})

        storage_root = Path(self.tempdir.name) / "storage"
        with patch.dict(os.environ, {"TEMP_STORAGE_DIR": str(storage_root)}):
            async def run_download():
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    return await download_whatsapp_media(db, company_id=1, inbound_message_id=message.id, client=client)

            with self.assertRaises(WhatsAppEmbeddedSignupError):
                asyncio.run(run_download())

            db.expire_all()
            attachments = db.scalars(
                select(MessageAttachment)
                .where(MessageAttachment.inbound_message_id == message.id)
                .order_by(MessageAttachment.id)
            ).all()
            self.assertEqual(len(attachments), 2)
            self.assertIsNotNone(attachments[0].storage_path)
            self.assertEqual(attachments[0].extraction_status, "extracted")
            self.assertIsNone(attachments[1].storage_path)

            result = asyncio.run(run_download())

        self.assertEqual(result["downloaded"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertTrue(result["ready_for_processing"])
        db.close()

    def test_attachment_extraction_supports_text_and_docx_without_llm(self):
        text_result = extract_attachment_text(
            b"Cliente Demo\n10 cajas de producto",
            filename="pedido.txt",
            content_type="text/plain",
        )
        self.assertEqual(text_result.status, "extracted")
        self.assertIn("10 cajas", text_result.text or "")

        document = BytesIO()
        with ZipFile(document, "w") as archive:
            archive.writestr(
                "word/document.xml",
                (
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    "<w:body><w:p><w:r><w:t>Pedido DOCX</w:t></w:r></w:p>"
                    "<w:p><w:r><w:t>5 unidades</w:t></w:r></w:p></w:body></w:document>"
                ),
            )
        docx_result = extract_attachment_text(
            document.getvalue(),
            filename="pedido.docx",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertEqual(docx_result.status, "extracted")
        self.assertIn("Pedido DOCX", docx_result.text or "")
        self.assertIn("5 unidades", docx_result.text or "")

        audio_result = extract_attachment_text(
            b"audio",
            filename="nota.ogg",
            content_type="audio/ogg",
        )
        self.assertEqual(audio_result.status, "transcription_pending")
        self.assertIsNone(audio_result.text)

    def test_audio_download_is_queued_for_handoff_without_transcription(self):
        payload = {
            "entry": [
                {
                    "id": "ba-123",
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "pn-123"},
                                "messages": [
                                    {
                                        "id": "wa-audio-download-1",
                                        "from": "+34600000000",
                                        "type": "audio",
                                        "audio": {"id": "audio-download-1", "mime_type": "audio/ogg"},
                                    }
                                ],
                            }
                        }
                    ],
                }
            ]
        }
        db = self.TenantSession()
        message = persist_event(db, 1, parse_payload_events(payload)[0])

        def handler(request):
            if request.url.host == "graph.facebook.com" and request.url.path.endswith("/audio-download-1"):
                return httpx.Response(200, json={"url": "https://lookaside.fbsbx.com/audio-download-1", "mime_type": "audio/ogg", "file_size": 4})
            if request.url.host == "lookaside.fbsbx.com":
                return httpx.Response(200, content=b"OggS")
            return httpx.Response(404, json={"error": {"message": "unexpected request"}})

        try:
            async def run_download():
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    return await download_whatsapp_media(db, company_id=1, inbound_message_id=message.id, client=client)

            result = asyncio.run(run_download())
            attachment = db.scalar(select(MessageAttachment).where(MessageAttachment.inbound_message_id == message.id))
            self.assertEqual(result["downloaded"], 1)
            self.assertEqual(attachment.extraction_status, "transcription_pending")
            self.assertTrue(result["ready_for_processing"])
            self.assertTrue(Path(attachment.storage_path).is_file())
        finally:
            db.close()

    def test_identifier_resolution_requires_both_identifiers_when_present(self):
        db = self.MasterSession()
        company, tenant = resolve_company_from_whatsapp_identifiers(
            db,
            business_account_id="ba-other",
            phone_number_id="pn-123",
        )
        self.assertIsNone(company)
        self.assertIsNone(tenant)
        company, tenant = resolve_company_from_whatsapp_identifiers(
            db,
            business_account_id="ba-123",
            phone_number_id="pn-123",
        )
        self.assertEqual(company.id, 1)
        self.assertEqual(tenant.company_id, 1)
        db.close()

    def test_manual_response_is_not_marked_as_sent_before_provider_delivery(self):
        db = self.TenantSession()
        inbound = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="whatsapp",
            provider="meta",
            external_id="wa-inbound-for-response",
            sender="+34600000000",
            recipients=["+34910000000"],
            text_content="Hola",
            external_thread_id="+34600000000",
            content_type="text",
        )[0]
        db.commit()
        response = record_manual_response(
            db,
            company_id=1,
            conversation_id=inbound.conversation_id,
            body="Te respondemos en breve",
        )
        self.assertEqual(response.status, "recorded")
        self.assertEqual(response.processing_step, "outbound_recorded")
        db.close()

    def test_manual_response_sends_text_and_records_meta_id(self):
        db = self.TenantSession()
        inbound = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="whatsapp",
            provider="meta",
            external_id="wa-inbound-for-send",
            sender="+34600000000",
            recipients=["+34910000000"],
            text_content="Hola",
            external_thread_id="+34600000000",
            content_type="text",
            received_at=datetime.now(timezone.utc),
        )[0]
        db.commit()

        def handler(request):
            request_payload = json.loads(request.content)
            self.assertEqual(request.url.path, "/v24.0/pn-123/messages")
            self.assertEqual(request_payload["to"], "34600000000")
            self.assertEqual(request_payload["type"], "text")
            return httpx.Response(200, json={"messages": [{"id": "wamid.test-1"}]})

        async def run_send():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await send_manual_response(
                    db,
                    company_id=1,
                    conversation_id=inbound.conversation_id,
                    body="Respuesta real de prueba",
                    client=client,
                )

        response = asyncio.run(run_send())
        self.assertEqual(response.source_external_id, "wamid.test-1")
        self.assertEqual(response.status, "accepted")
        self.assertEqual(response.processing_step, "outbound_accepted")
        db.close()

    def test_manual_response_sends_whatsapp_reply_context_and_persists_it(self):
        db = self.TenantSession()
        inbound = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="whatsapp",
            provider="meta",
            external_id="wa-inbound-for-quoted-reply",
            sender="+34600000000",
            recipients=["+34910000000"],
            text_content="¿Puedes confirmar el pedido?",
            external_thread_id="+34600000000",
            content_type="text",
            received_at=datetime.now(timezone.utc),
        )[0]
        db.commit()

        def handler(request):
            request_payload = json.loads(request.content)
            self.assertEqual(request_payload["context"], {"message_id": "wa-inbound-for-quoted-reply"})
            return httpx.Response(200, json={"messages": [{"id": "wamid.quoted-reply-1"}]})

        async def run_send():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await send_manual_response(
                    db,
                    company_id=1,
                    conversation_id=inbound.conversation_id,
                    body="Sí, queda confirmado.",
                    reply_to_message_id=inbound.id,
                    client=client,
                )

        response = asyncio.run(run_send())
        self.assertEqual(response.source_external_id, "wamid.quoted-reply-1")
        self.assertEqual(json.loads(response.raw_payload_json)["context"], {"message_id": "wa-inbound-for-quoted-reply"})
        db.close()

    def test_manual_response_retries_without_rejected_reply_context(self):
        db = self.TenantSession()
        inbound = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="whatsapp",
            provider="meta",
            external_id="wa-inbound-invalid-reply-context",
            sender="+34600000000",
            recipients=["+34910000000"],
            text_content="Mensaje citado",
            external_thread_id="+34600000000",
            content_type="text",
            received_at=datetime.now(timezone.utc),
        )[0]
        db.commit()
        requests = []

        def handler(request):
            payload = json.loads(request.content)
            requests.append(payload)
            if "context" in payload:
                return httpx.Response(
                    400,
                    json={"error": {"code": 100, "message": "Invalid reply context"}},
                )
            return httpx.Response(200, json={"messages": [{"id": "wamid.context-fallback-1"}]})

        async def run_send():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await send_manual_response(
                    db,
                    company_id=1,
                    conversation_id=inbound.conversation_id,
                    body="Respuesta sin perder el mensaje",
                    reply_to_message_id=inbound.id,
                    client=client,
                )

        response = asyncio.run(run_send())
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["context"], {"message_id": "wa-inbound-invalid-reply-context"})
        self.assertNotIn("context", requests[1])
        self.assertEqual(response.status, "accepted")
        self.assertEqual(response.source_external_id, "wamid.context-fallback-1")
        db.close()

    def test_meta_rejection_marks_outbound_message_as_failed(self):
        db = self.TenantSession()
        inbound = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="whatsapp",
            provider="meta",
            external_id="wa-inbound-meta-rejection",
            sender="+34600000000",
            recipients=["+34910000000"],
            text_content="Hola",
            external_thread_id="+34600000000",
            content_type="text",
            received_at=datetime.now(timezone.utc),
        )[0]
        db.commit()

        def handler(request):
            payload = json.loads(request.content)
            self.assertEqual(payload["to"], "34600000000")
            return httpx.Response(
                400,
                json={"error": {"code": 131000, "message": "Permission denied"}},
            )

        async def run_send():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(WhatsAppEmbeddedSignupError) as context:
                    await send_manual_response(
                        db,
                        company_id=1,
                        conversation_id=inbound.conversation_id,
                        body="Respuesta rechazada",
                        client=client,
                        idempotency_key="reply-meta-rejection-1",
                    )
                return context.exception

        error = asyncio.run(run_send())
        self.assertEqual(error.error_type, "meta_api_error")
        self.assertEqual(error.meta_error_code, "131000")
        message = db.scalar(
            select(InboundMessage).where(
                InboundMessage.company_id == 1,
                InboundMessage.source_message_id == "reply-meta-rejection-1",
            )
        )
        self.assertEqual(message.status, "failed")
        self.assertEqual(message.processing_step, "outbound_send_failed")
        self.assertIn("Permission denied", message.processing_error)
        db.close()

    def test_definitive_outbound_failure_can_be_retried_and_resolves_alert(self):
        db = self.TenantSession()
        inbound = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="whatsapp",
            provider="meta",
            external_id="wa-inbound-retry-failed",
            sender="+34600000000",
            recipients=["+34910000000"],
            text_content="Hola",
            external_thread_id="+34600000000",
            content_type="text",
            received_at=datetime.now(timezone.utc),
        )[0]
        failed = record_manual_response(
            db,
            company_id=1,
            conversation_id=inbound.conversation_id,
            body="Reintentar este mensaje",
            external_id="wa-pending-retry-failed",
            status="failed",
            processing_step="outbound_send_failed",
            idempotency_key="retry-failed-original",
        )
        db.add(
            Alert(
                company_id=1,
                inbound_message_id=failed.id,
                alert_type="whatsapp_outbound_failed",
                severity="high",
                status="open",
                title="Falló un envío de WhatsApp",
                message="Revisar",
            )
        )
        db.commit()

        async def run_retry():
            async def fake_send(*_args, **_kwargs):
                return {"provider_message_id": "wamid.retry-success"}

            with patch(
                "app.whatsapp.service.send_whatsapp_text",
                new=fake_send,
            ):
                return await retry_failed_whatsapp_message(
                    db,
                    company_id=1,
                    message_id=failed.id,
                    user_id=1,
                )

        result = asyncio.run(run_retry())
        self.assertEqual(result.source_external_id, "wamid.retry-success")
        self.assertEqual(result.status, "accepted")
        alert = db.scalar(
            select(Alert).where(
                Alert.inbound_message_id == failed.id,
                Alert.alert_type == "whatsapp_outbound_failed",
            )
        )
        self.assertEqual(alert.status, "resolved")
        outbound_count = db.scalar(
            select(func.count(InboundMessage.id)).where(
                InboundMessage.company_id == 1,
                InboundMessage.conversation_id == inbound.conversation_id,
                InboundMessage.direction == "outbound",
            )
        )
        self.assertEqual(outbound_count, 2)
        db.close()

    def test_manual_response_idempotency_key_does_not_send_twice(self):
        db = self.TenantSession()
        inbound = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="whatsapp",
            provider="meta",
            external_id="wa-inbound-idempotent-response",
            sender="+34600000000",
            recipients=["+34910000000"],
            text_content="Hola",
            external_thread_id="+34600000000",
            content_type="text",
            received_at=datetime.now(timezone.utc),
        )[0]
        db.commit()
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"messages": [{"id": "wamid.idempotent-1"}]})

        async def run_send():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                first = await send_manual_response(
                    db,
                    company_id=1,
                    conversation_id=inbound.conversation_id,
                    body="Respuesta única",
                    client=client,
                    idempotency_key="reply-test-1",
                )
                second = await send_manual_response(
                    db,
                    company_id=1,
                    conversation_id=inbound.conversation_id,
                    body="Respuesta única",
                    client=client,
                    idempotency_key="reply-test-1",
                )
                return first, second

        first, second = asyncio.run(run_send())
        self.assertEqual(calls, 1)
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.source_message_id, "reply-test-1")
        db.close()

    def test_idempotent_response_is_reserved_before_provider_call(self):
        db = self.TenantSession()
        self.addCleanup(db.close)
        inbound = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="whatsapp",
            provider="meta",
            external_id="wa-inbound-reservation",
            sender="+34600000000",
            recipients=["+34910000000"],
            text_content="Hola",
            external_thread_id="+34600000000",
            content_type="text",
            received_at=datetime.now(timezone.utc),
        )[0]
        db.commit()
        observed = []

        def handler(request):
            check_db = self.TenantSession()
            try:
                reserved = check_db.scalar(
                    select(InboundMessage).where(
                        InboundMessage.company_id == 1,
                        InboundMessage.source_message_id == "reply-reservation-1",
                    )
                )
                observed.append((reserved.status, reserved.processing_step, reserved.source_external_id.startswith("wa-pending-")))
            finally:
                check_db.close()
            return httpx.Response(200, json={"messages": [{"id": "wamid.reserved-1"}]})

        async def run_send():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await send_manual_response(
                    db,
                    company_id=1,
                    conversation_id=inbound.conversation_id,
                    body="Respuesta reservada",
                    client=client,
                    idempotency_key="reply-reservation-1",
                )

        response = asyncio.run(run_send())
        self.assertEqual(observed, [("sending", "outbound_sending", True)])
        self.assertEqual(response.status, "accepted")
        self.assertEqual(response.source_external_id, "wamid.reserved-1")
        db.close()

    def test_unknown_provider_result_is_not_retried_with_same_idempotency_key(self):
        db = self.TenantSession()
        self.addCleanup(db.close)
        inbound = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="whatsapp",
            provider="meta",
            external_id="wa-inbound-unknown-send",
            sender="+34600000000",
            recipients=["+34910000000"],
            text_content="Hola",
            external_thread_id="+34600000000",
            content_type="text",
            received_at=datetime.now(timezone.utc),
        )[0]
        db.commit()
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout("provider timeout")

        async def run_send():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(WhatsAppEmbeddedSignupError) as first_error:
                    await send_manual_response(
                        db,
                        company_id=1,
                        conversation_id=inbound.conversation_id,
                        body="No duplicar",
                        client=client,
                        idempotency_key="reply-unknown-1",
                    )
                with self.assertRaises(WhatsAppEmbeddedSignupError) as second_error:
                    await send_manual_response(
                        db,
                        company_id=1,
                        conversation_id=inbound.conversation_id,
                        body="No duplicar",
                        client=client,
                        idempotency_key="reply-unknown-1",
                    )
                return first_error.exception, second_error.exception

        first_error, second_error = asyncio.run(run_send())
        self.assertEqual(first_error.error_type, "timeout")
        self.assertEqual(second_error.error_type, "send_unknown")
        self.assertEqual(calls, 1)
        message = db.scalar(
            select(InboundMessage).where(
                InboundMessage.company_id == 1,
                InboundMessage.source_message_id == "reply-unknown-1",
            )
        )
        self.assertEqual(message.status, "send_unknown")
        self.assertEqual(message.processing_step, "outbound_send_unknown")
        unknown_alert = db.scalar(
            select(Alert).where(
                Alert.company_id == 1,
                Alert.inbound_message_id == message.id,
                Alert.alert_type == "whatsapp_outbound_unknown",
            )
        )
        self.assertIsNotNone(unknown_alert)
        db.close()

    def test_accepted_idempotent_replay_bypasses_expired_reply_window(self):
        db = self.TenantSession()
        self.addCleanup(db.close)
        inbound = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="whatsapp",
            provider="meta",
            external_id="wa-inbound-replay-window",
            sender="+34600000000",
            recipients=["+34910000000"],
            text_content="Hola",
            external_thread_id="+34600000000",
            content_type="text",
            received_at=datetime.now(timezone.utc),
        )[0]
        db.commit()
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"messages": [{"id": "wamid.replay-window-1"}]})

        async def run_send():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                first = await send_manual_response(
                    db,
                    company_id=1,
                    conversation_id=inbound.conversation_id,
                    body="Respuesta única",
                    client=client,
                    idempotency_key="reply-replay-window-1",
                )
                inbound.received_at = datetime.now(timezone.utc) - timedelta(days=3)
                db.commit()
                second = await send_manual_response(
                    db,
                    company_id=1,
                    conversation_id=inbound.conversation_id,
                    body="Respuesta única",
                    client=client,
                    idempotency_key="reply-replay-window-1",
                )
                return first, second

        first, second = asyncio.run(run_send())
        self.assertEqual(calls, 1)
        self.assertEqual(first.id, second.id)
        db.close()

    def test_manual_response_can_send_template_outside_reply_window(self):
        db = self.TenantSession()
        inbound = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="whatsapp",
            provider="meta",
            external_id="wa-inbound-template-response",
            sender="+34600000000",
            recipients=["+34910000000"],
            text_content="Pedido",
            external_thread_id="+34600000000",
            content_type="text",
            received_at=datetime.now(timezone.utc) - timedelta(days=3),
        )[0]
        db.commit()

        def handler(request):
            payload = json.loads(request.content)
            self.assertEqual(payload["type"], "template")
            self.assertEqual(payload["template"]["name"], "pedido_confirmado")
            self.assertEqual(payload["template"]["language"]["code"], "es")
            self.assertNotIn("text", payload)
            return httpx.Response(200, json={"messages": [{"id": "wamid.template-1"}]})

        async def run_send():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await send_manual_response(
                    db,
                    company_id=1,
                    conversation_id=inbound.conversation_id,
                    body="",
                    template_name="pedido_confirmado",
                    client=client,
                )

        response = asyncio.run(run_send())
        self.assertEqual(response.source_external_id, "wamid.template-1")
        self.assertIn("pedido_confirmado", response.original_content)
        db.close()

    def test_delivery_status_updates_existing_outbound_conversation(self):
        db = self.TenantSession()
        inbound = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="whatsapp",
            provider="meta",
            external_id="wa-inbound-for-status",
            sender="+34600000000",
            recipients=["+34910000000"],
            text_content="Hola",
            external_thread_id="+34600000000",
            content_type="text",
        )[0]
        db.commit()
        outbound = record_manual_response(
            db,
            company_id=1,
            conversation_id=inbound.conversation_id,
            body="Respuesta",
            external_id="wamid.delivery-1",
            status="accepted",
            processing_step="outbound_accepted",
        )
        status_event = {
            "kind": "status",
            "external_id": "wamid.delivery-1",
            "external_thread_id": "provider-conversation-1",
            "occurred_at": datetime.now(timezone.utc),
            "metadata": {"payload": {"status": "delivered"}},
        }
        updated = persist_event(db, 1, status_event)
        self.assertEqual(updated.id, outbound.id)
        self.assertEqual(updated.conversation_id, inbound.conversation_id)
        self.assertEqual(updated.status, "delivered")

        older_status_event = {
            "kind": "status",
            "external_id": "wamid.delivery-1",
            "metadata": {"payload": {"status": "sent"}},
        }
        persist_event(db, 1, older_status_event)
        self.assertEqual(updated.status, "delivered")
        self.assertEqual(updated.processing_step, "delivery_delivered")
        persist_event(
            db,
            1,
            {
                "kind": "status",
                "external_id": "wamid.delivery-1",
                "metadata": {"payload": {"status": "failed"}},
            },
        )
        self.assertEqual(updated.status, "delivered")
        self.assertIsNone(updated.processing_error)
        db.close()

    def test_failed_delivery_preserves_provider_error_detail(self):
        db = self.TenantSession()
        inbound = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="whatsapp",
            provider="meta",
            external_id="wa-inbound-for-failed-status",
            sender="+34600000000",
            recipients=["+34910000000"],
            text_content="Hola",
            external_thread_id="+34600000000",
            content_type="text",
        )[0]
        db.commit()
        outbound = record_manual_response(
            db,
            company_id=1,
            conversation_id=inbound.conversation_id,
            body="Respuesta",
            external_id="wamid.failed-detail-1",
            status="accepted",
            processing_step="outbound_accepted",
        )

        persist_event(
            db,
            1,
            {
                "kind": "status",
                "external_id": outbound.source_external_id,
                "metadata": {
                    "payload": {
                        "status": "failed",
                        "errors": [{"code": 131, "title": "Rate limit", "message": "Try again later"}],
                    }
                },
            },
        )

        self.assertEqual(outbound.status, "failed")
        self.assertIn("131", outbound.processing_error or "")
        self.assertIn("Try again later", outbound.processing_error or "")
        db.close()

    def test_mulet_local_uat_covers_bidirectional_flow_without_duplicates(self):
        text_payload = {
            "entry": [
                {
                    "id": "ba-123",
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "pn-123"},
                                "messages": [
                                    {
                                        "id": "wa-mulet-uat-text-1",
                                        "from": "+34600000000",
                                        "type": "text",
                                        "text": {
                                            "body": "Pedido de 5 unidades de P-100 para Cliente WhatsApp SL. Nada más, gracias"
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                }
            ]
        }
        db = self.TenantSession()
        inbound = persist_event(db, 1, parse_payload_events(text_payload)[0])
        duplicate = persist_event(db, 1, parse_payload_events(text_payload)[0])
        conversation_id = inbound.conversation_id
        self.assertTrue(db.get(Conversation, conversation_id).whatsapp_bot_enabled)
        enqueue_whatsapp_processing(db, 1, inbound.id)
        db.close()

        classification = json.dumps(
            {"tipo_correo": "pedido", "confianza": 0.97, "motivo": "Pedido claro"},
            ensure_ascii=False,
        )
        extraction = json.dumps(
            {
                "cliente": {"nombre_detectado": "Cliente WhatsApp SL", "codigo_cliente_detectado": "C001"},
                "pedido": {
                    "fecha_pedido": "2026-08-31",
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
        with patch("app.whatsapp.agent.evaluate_whatsapp_agent_turn", return_value=self._complete_agent_turn()), patch(
            "app.agent.platform.classify_sample", return_value={"ok": True, "content": classification}
        ), patch(
            "app.agent.platform.extract_sample", return_value={"ok": True, "content": extraction}
        ), patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession):
            worker_summary = run_worker_cycle()

        db = self.TenantSession()
        self.assertIs(inbound, duplicate)
        self.assertEqual(worker_summary["processed"], 2)
        order = db.scalar(select(Order).where(Order.company_id == 1))
        self.assertIsNotNone(order)
        self.assertEqual(order.customer_id, 1)
        self.assertEqual(order.lines[0].validated_product_id, 1)
        self.assertEqual(db.scalar(select(func.count()).select_from(Order)), 1)

        media_payload = {
            "entry": [
                {
                    "id": "ba-123",
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "pn-123"},
                                "messages": [
                                    {
                                        "id": "wa-mulet-uat-document-1",
                                        "from": "+34600000000",
                                        "type": "document",
                                        "document": {
                                            "id": "m-media-mulet-uat-1",
                                            "filename": "pedido.pdf",
                                            "mime_type": "application/pdf",
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                }
            ]
        }
        media_message = persist_event(db, 1, parse_payload_events(media_payload)[0])

        def media_handler(request):
            if request.url.host == "graph.facebook.com" and request.url.path.endswith("/m-media-mulet-uat-1"):
                return httpx.Response(
                    200,
                    json={
                        "url": "https://lookaside.fbsbx.com/m-media-mulet-uat-1",
                        "mime_type": "application/pdf",
                        "file_size": 32,
                    },
                )
            if request.url.host == "lookaside.fbsbx.com":
                return httpx.Response(200, content=b"%PDF-1.4\nBT\n(Pedido 5 unidades) Tj\nET")
            return httpx.Response(404, json={"error": {"message": "unexpected request"}})

        storage_root = Path(self.tempdir.name) / "mulet-uat-storage"
        with patch.dict(os.environ, {"TEMP_STORAGE_DIR": str(storage_root)}):
            async def run_media_download():
                async with httpx.AsyncClient(transport=httpx.MockTransport(media_handler)) as client:
                    return await download_whatsapp_media(
                        db,
                        company_id=1,
                        inbound_message_id=media_message.id,
                        client=client,
                    )

            media_result = asyncio.run(run_media_download())

        attachment = db.scalar(select(MessageAttachment).where(MessageAttachment.inbound_message_id == media_message.id))
        self.assertEqual(media_result["downloaded"], 1)
        self.assertTrue(media_result["ready_for_processing"])
        self.assertEqual(attachment.extraction_status, "extracted")
        self.assertIn("Pedido 5 unidades", attachment.extracted_text or "")

        provider_calls = 0

        def outbound_handler(request):
            nonlocal provider_calls
            provider_calls += 1
            return httpx.Response(200, json={"messages": [{"id": "wamid.mulet-uat-reply-1"}]})

        async def send_reply():
            async with httpx.AsyncClient(transport=httpx.MockTransport(outbound_handler)) as client:
                return await send_manual_response(
                    db,
                    company_id=1,
                    conversation_id=conversation_id,
                    body="Pedido recibido y en revisión.",
                    client=client,
                    idempotency_key="mulet-uat-reply-1",
                )

        outbound = asyncio.run(send_reply())
        self.assertEqual(outbound.status, "accepted")
        self.assertEqual(provider_calls, 1)
        delivered = persist_event(
            db,
            1,
            {
                "kind": "status",
                "external_id": outbound.source_external_id,
                "metadata": {"payload": {"status": "delivered"}},
            },
        )
        self.assertEqual(delivered.status, "delivered")

        uncertain_calls = 0

        def uncertain_handler(request):
            nonlocal uncertain_calls
            uncertain_calls += 1
            raise httpx.ReadTimeout("provider timeout", request=request)

        async def send_uncertain_reply():
            async with httpx.AsyncClient(transport=httpx.MockTransport(uncertain_handler)) as client:
                with self.assertRaises(WhatsAppEmbeddedSignupError) as first_error:
                    await send_manual_response(
                        db,
                        company_id=1,
                        conversation_id=conversation_id,
                        body="No duplicar esta respuesta.",
                        client=client,
                        idempotency_key="mulet-uat-uncertain-1",
                    )
                with self.assertRaises(WhatsAppEmbeddedSignupError) as second_error:
                    await send_manual_response(
                        db,
                        company_id=1,
                        conversation_id=conversation_id,
                        body="No duplicar esta respuesta.",
                        client=client,
                        idempotency_key="mulet-uat-uncertain-1",
                    )
                return first_error.exception, second_error.exception

        first_error, second_error = asyncio.run(send_uncertain_reply())
        self.assertEqual(first_error.error_type, "timeout")
        self.assertEqual(second_error.error_type, "send_unknown")
        self.assertEqual(uncertain_calls, 1)
        db.close()

    def test_delayed_webhook_cannot_extend_customer_service_window(self):
        received_at = datetime.now(timezone.utc) - timedelta(hours=25)
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
                                        "id": "wa-delayed-inbound-window-1",
                                        "from": "+34600000000",
                                        "timestamp": str(int(received_at.timestamp())),
                                        "type": "text",
                                        "text": {"body": "Quiero hacer un pedido"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        db = self.TenantSession()
        try:
            message = persist_event(db, 1, parse_payload_events(payload)[0])
            persisted_received_at = message.received_at
            if persisted_received_at.tzinfo is None:
                persisted_received_at = persisted_received_at.replace(tzinfo=timezone.utc)
            expected_received_at = datetime.fromtimestamp(
                int(received_at.timestamp()),
                tz=timezone.utc,
            )
            self.assertEqual(persisted_received_at, expected_received_at)

            provider_calls = 0

            def handler(request: httpx.Request) -> httpx.Response:
                nonlocal provider_calls
                provider_calls += 1
                return httpx.Response(200, json={"messages": [{"id": "wamid.unexpected"}]})

            async def send_freeform_reply():
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    await send_manual_response(
                        db,
                        company_id=1,
                        conversation_id=message.conversation_id,
                        body="Respuesta fuera de ventana",
                        client=client,
                        idempotency_key="wa-delayed-inbound-window-reply-1",
                    )

            with self.assertRaises(WhatsAppEmbeddedSignupError) as error:
                asyncio.run(send_freeform_reply())

            self.assertEqual(error.exception.error_type, "response_window_expired")
            self.assertEqual(provider_calls, 0)
            outbound_count = db.scalar(
                select(func.count())
                .select_from(InboundMessage)
                .where(
                    InboundMessage.company_id == 1,
                    InboundMessage.conversation_id == message.conversation_id,
                    InboundMessage.direction == "outbound",
                )
            )
            self.assertEqual(outbound_count, 0)
        finally:
            db.close()

    def test_downloaded_pdf_flows_from_webhook_to_order_review(self):
        received_at = datetime.now(timezone.utc) - timedelta(hours=13)
        pdf_text = "5 unidades de Producto WhatsApp P-100 para Cliente WhatsApp SL"
        pdf_stream = f"BT /F1 12 Tf 72 720 Td ({pdf_text}) Tj ET".encode("ascii")
        pdf_objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
            f"<< /Length {len(pdf_stream)} >>\nstream\n".encode("ascii")
            + pdf_stream
            + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
        pdf_buffer = bytearray(b"%PDF-1.4\n")
        pdf_offsets = [0]
        for object_number, object_body in enumerate(pdf_objects, start=1):
            pdf_offsets.append(len(pdf_buffer))
            pdf_buffer.extend(f"{object_number} 0 obj\n".encode("ascii"))
            pdf_buffer.extend(object_body + b"\nendobj\n")
        xref_offset = len(pdf_buffer)
        pdf_buffer.extend(b"xref\n0 6\n0000000000 65535 f \n")
        for object_offset in pdf_offsets[1:]:
            pdf_buffer.extend(f"{object_offset:010d} 00000 n \n".encode("ascii"))
        pdf_buffer.extend(
            b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"
            + str(xref_offset).encode("ascii")
            + b"\n%%EOF\n"
        )
        pdf_payload = bytes(pdf_buffer)
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "pn-123", "business_account_id": "ba-123"},
                                "messages": [
                                    {
                                        "id": "wa-pdf-order-1",
                                        "from": "+34600000000",
                                        "timestamp": str(int(received_at.timestamp())),
                                        "type": "document",
                                        "document": {
                                            "id": "meta-pdf-order-1",
                                            "filename": "pedido.pdf",
                                            "mime_type": "application/pdf",
                                            "file_size": len(pdf_payload),
                                            "caption": "Te adjunto el pedido.",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        db = self.TenantSession()
        message = persist_event(db, 1, parse_payload_events(payload)[0])
        persisted_received_at = message.received_at.replace(tzinfo=timezone.utc)
        self.assertTrue(db.get(Conversation, message.conversation_id).whatsapp_bot_enabled)
        enqueue_whatsapp_media_download(db, 1, message.id)
        db.commit()
        message_id = message.id
        db.close()
        expected_received_at = datetime.fromtimestamp(
            int(received_at.timestamp()),
            tz=timezone.utc,
        )
        self.assertEqual(persisted_received_at, expected_received_at)

        def media_handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "graph.facebook.com" and request.url.path.endswith("/meta-pdf-order-1"):
                return httpx.Response(
                    200,
                    json={
                        "url": "https://lookaside.fbsbx.com/meta-pdf-order-1",
                        "mime_type": "application/pdf",
                        "file_size": len(pdf_payload),
                    },
                )
            if request.url.host == "lookaside.fbsbx.com":
                return httpx.Response(200, content=pdf_payload)
            return httpx.Response(404, json={"error": {"message": "unexpected test request"}})

        storage_root = Path(self.tempdir.name) / "whatsapp-pdf-order-storage"
        with patch.dict(os.environ, {"TEMP_STORAGE_DIR": str(storage_root)}):
            async def download_pdf():
                async with httpx.AsyncClient(transport=httpx.MockTransport(media_handler)) as client:
                    with self.TenantSession() as media_db:
                        return await download_whatsapp_media(
                            media_db,
                            company_id=1,
                            inbound_message_id=message_id,
                            client=client,
                        )

            media_result = asyncio.run(download_pdf())

        self.assertTrue(media_result["ready_for_processing"])
        db = self.TenantSession()
        attachment = db.scalar(
            select(MessageAttachment).where(MessageAttachment.inbound_message_id == message_id)
        )
        self.assertEqual(attachment.extraction_status, "extracted")
        self.assertIn("Producto WhatsApp P-100", attachment.extracted_text or "")
        db.close()

        classification = json.dumps(
            {"tipo_correo": "pedido", "confianza": 0.97, "motivo": "Pedido claro"},
            ensure_ascii=False,
        )
        extraction = json.dumps(
            {
                "cliente": {"nombre_detectado": "Cliente WhatsApp SL", "codigo_cliente_detectado": "C001"},
                "pedido": {
                    "lineas": [
                        {
                            "texto_original": "5 unidades de Producto WhatsApp P-100",
                            "referencia_detectada": "P-100",
                            "producto_detectado": "Producto WhatsApp",
                            "cantidad": 5,
                            "unidad": "uds",
                            "confianza_extraccion": 0.95,
                        }
                    ]
                },
            },
            ensure_ascii=False,
        )
        with (
            patch(
                "app.whatsapp.agent.evaluate_whatsapp_agent_turn",
                return_value=self._complete_agent_turn(),
            ) as evaluate_turn,
            patch("app.whatsapp.agent.await_send_automatic_response", return_value={"sent": True}),
            patch("app.agent.platform.classify_sample", return_value={"ok": True, "content": classification}),
            patch("app.agent.platform.extract_sample", return_value={"ok": True, "content": extraction}),
            patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession),
        ):
            worker_summary = run_worker_cycle()

        db = self.TenantSession()
        order = db.scalar(select(Order).where(Order.company_id == 1))
        session = db.scalar(select(WhatsAppOrderSession).where(WhatsAppOrderSession.company_id == 1))
        session_state = (
            session.status,
            session.due_at.isoformat() if session.due_at else None,
            session.active_key,
            session.finalization_job_key,
        ) if session else None
        job_states = db.scalars(
            select(BackgroundJob.status).order_by(BackgroundJob.id)
        ).all()
        order_fields = (
            order.id,
            order.customer_id,
            order.lines[0].validated_product_id,
            order.lines[0].quantity,
        ) if order and order.lines else None
        session_fields = (session.status, session.order_id) if session else None
        pdf_transcript = evaluate_turn.call_args.kwargs["transcript"]
        order_count = db.scalar(select(func.count()).select_from(Order))
        db.close()

        self.assertEqual(
            worker_summary["processed"],
            3,
            msg=f"session={session_state!r}; jobs={job_states!r}",
        )
        self.assertIn("Producto WhatsApp P-100", pdf_transcript)
        self.assertIsNotNone(order_fields)
        self.assertEqual(order_fields[1:], (1, 1, 5))
        self.assertEqual(session_fields[0], "closed_review")
        self.assertEqual(session_fields[1], order_fields[0])
        self.assertEqual(order_count, 1)

    def test_whatsapp_service_alert_links_back_to_its_conversation(self):
        db = self.TenantSession()
        self.addCleanup(db.close)
        channel = db.scalar(select(InputChannel).where(InputChannel.company_id == 1, InputChannel.key == "whatsapp"))
        conversation = Conversation(
            company_id=1,
            channel_id=channel.id,
            provider="meta",
            external_thread_id="+34600000009",
            status="open",
        )
        db.add(conversation)
        db.flush()
        inbound = InboundMessage(
            company_id=1,
            channel_id=channel.id,
            provider="meta",
            conversation_id=conversation.id,
            source_external_id="wa-alert-test-1",
            direction="inbound",
            sender="+34600000009",
            original_content="Adjunto de prueba",
        )
        db.add(inbound)
        db.flush()

        first_alert = _upsert_whatsapp_alert(
            db,
            company_id=1,
            alert_type="whatsapp_media_processing_failed",
            title="Fallo de media de prueba",
            message="El adjunto de prueba requiere revisión.",
            severity="medium",
            inbound_message_id=inbound.id,
        )
        db.commit()
        first_alert_id = first_alert.id
        self.assertEqual(json.loads(first_alert.payload_json)["conversation_id"], conversation.id)
        self.assertEqual(
            alert_default_action(first_alert)[1],
            f"/whatsapp/inbox?conversation_id={conversation.id}",
        )

        updated_alert = _upsert_whatsapp_alert(
            db,
            company_id=1,
            alert_type="whatsapp_media_processing_failed",
            title="Fallo de media actualizado",
            message="El adjunto sigue requiriendo revisión.",
            severity="high",
            inbound_message_id=inbound.id,
        )
        db.commit()
        self.assertEqual(updated_alert.id, first_alert_id)
        self.assertEqual(json.loads(updated_alert.payload_json)["conversation_id"], conversation.id)
        self.assertEqual(db.scalar(select(func.count()).select_from(Alert)), 1)
        db.close()

    def test_multiturn_webhook_text_and_pdf_build_one_review_order(self):
        db = self.TenantSession()
        db.add(
            Product(
                company_id=1,
                reference="V-200",
                alternative_code="ALT-V200",
                name="Segundo producto WhatsApp",
                sale_unit="uds",
                sale_price=2.5,
                status="active",
            )
        )
        db.add(
            Product(
                company_id=1,
                reference="P-300",
                name="Tercer producto WhatsApp",
                sale_unit="cajas",
                sale_price=4.0,
                status="active",
            )
        )
        db.commit()
        db.close()

        def classify_turn(_db, _company_id, text, **_kwargs):
            normalized = str(text).casefold()
            if "nada" in normalized:
                return {"tipo_correo": "no_pedido", "confianza": 0.99, "motivo": "Cierre explícito"}
            if "v-200" in normalized or "corrige" in normalized:
                return {"tipo_correo": "no_pedido", "confianza": 0.82, "motivo": "Continuación del pedido"}
            return {"tipo_correo": "pedido", "confianza": 0.97, "motivo": "Pedido claro"}

        def extract_turn(_db, _company_id, transcript, _inbound_message, **_kwargs):
            normalized = str(transcript).casefold()
            lines = []
            if "p-100" in normalized:
                lines.append(
                    {
                        "texto_original": "6 unidades de P-100" if "son 6" in normalized else "5 unidades de P-100",
                        "referencia_detectada": "P-100",
                        "producto_detectado": "Producto WhatsApp",
                        "cantidad": 6 if "son 6" in normalized else 5,
                        "unidad": "uds",
                    }
                )
            if "v-200" in normalized:
                lines.append(
                    {
                        "texto_original": "2 unidades de V-200",
                        "referencia_detectada": "V-200",
                        "producto_detectado": "Segundo producto WhatsApp",
                        "cantidad": 2,
                        "unidad": "uds",
                    }
                )
            if "p-300" in normalized:
                lines.append(
                    {
                        "texto_original": "3 cajas de P-300",
                        "referencia_detectada": "P-300",
                        "producto_detectado": "Tercer producto WhatsApp",
                        "cantidad": 3,
                        "unidad": "cajas",
                    }
                )
            return {
                "cliente": {"nombre_detectado": "Cliente WhatsApp SL"},
                "pedido": {
                    "lineas": lines,
                    "observaciones": "Entrega el jueves" if "jueves" in normalized else "",
                },
            }
        customer_messages = [
            {
                "id": "wa-multiturn-order-1",
                "kind": "text",
                "body": "Hola, quiero 5 unidades del P-100.",
            },
            {
                "id": "wa-multiturn-order-2",
                "kind": "text",
                "body": "A\u00f1ade 2 unidades del V-200, por favor.",
            },
            {
                "id": "wa-multiturn-order-pdf",
                "kind": "pdf",
                "body": "Te adjunto el PDF con otra l\u00ednea.",
                "extracted_text": "Pedido adicional: 3 cajas de P-300.",
            },
            {
                "id": "wa-multiturn-order-4",
                "kind": "text",
                "body": "Corrige P-100: son 6 unidades. La entrega, el jueves.",
            },
            {
                "id": "wa-multiturn-order-5",
                "kind": "text",
                "body": "Nada m\u00e1s, gracias.",
            },
        ]
        start = datetime.now(timezone.utc) - timedelta(seconds=30)
        with (
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
                side_effect=classify_turn,
            ) as classifier,
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.extract_inbound_order",
                side_effect=extract_turn,
            ) as extractor,
            patch(
                "app.whatsapp.agent.await_send_automatic_response",
                return_value={"sent": True, "reason": "sent"},
            ) as send_reply,
            patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession),
        ):
            conversation_id = None
            message_ids_by_external_id = {}
            for index, customer_message in enumerate(customer_messages):
                received_at = start + timedelta(seconds=index * 5)
                if customer_message["kind"] == "pdf":
                    incoming_message = {
                        "id": customer_message["id"],
                        "from": "+34600000000",
                        "timestamp": str(int(received_at.timestamp())),
                        "type": "document",
                        "document": {
                            "id": "media-multiturn-order-pdf",
                            "filename": "pedido-adicional.pdf",
                            "mime_type": "application/pdf",
                            "caption": customer_message["body"],
                        },
                    }
                else:
                    incoming_message = {
                        "id": customer_message["id"],
                        "from": "+34600000000",
                        "timestamp": str(int(received_at.timestamp())),
                        "type": "text",
                        "text": {"body": customer_message["body"]},
                    }
                payload = {
                    "entry": [
                        {
                            "id": "ba-123",
                            "changes": [
                                {
                                    "field": "messages",
                                    "value": {
                                        "metadata": {
                                            "phone_number_id": "pn-123",
                                            "business_account_id": "ba-123",
                                        },
                                        "messages": [incoming_message],
                                    },
                                }
                            ],
                        }
                    ]
                }
                with self.TenantSession() as message_db:
                    inbound = persist_event(message_db, 1, parse_payload_events(payload)[0])
                    conversation_id = inbound.conversation_id
                    message_id = inbound.id
                    message_ids_by_external_id[customer_message["id"]] = message_id
                    if customer_message["kind"] == "pdf":
                        attachment = message_db.scalar(
                            select(MessageAttachment).where(
                                MessageAttachment.company_id == 1,
                                MessageAttachment.inbound_message_id == message_id,
                            )
                        )
                        self.assertIsNotNone(attachment)
                        attachment.content_type = "application/pdf"
                        attachment.is_pdf = True
                        attachment.extracted_text = customer_message["extracted_text"]
                        attachment.extraction_status = "extracted"
                    if index == len(customer_messages) - 1:
                        self.assertTrue(has_explicit_order_close(inbound.original_content))
                    self.assertTrue(
                        message_db.get(Conversation, conversation_id).whatsapp_bot_enabled
                    )
                    enqueue_whatsapp_processing(message_db, 1, message_id)

                cycle = run_worker_cycle(max_jobs=1)
                self.assertEqual(cycle["processed"], 1, msg=f"turn={index + 1}; cycle={cycle!r}")
                with self.TenantSession() as state_db:
                    session = state_db.scalar(
                        select(WhatsAppOrderSession).where(
                            WhatsAppOrderSession.company_id == 1,
                            WhatsAppOrderSession.conversation_id == conversation_id,
                        )
                    )
                    self.assertIsNotNone(session)
                    self.assertEqual(session.last_processed_message_id, message_id)
                    self.assertEqual(
                        state_db.scalar(select(func.count()).select_from(Order)),
                        0,
                        "El pedido no debe persistirse hasta que se cierre el turno.",
                    )

            with self.TenantSession() as state_db:
                pending_session = state_db.scalar(
                    select(WhatsAppOrderSession).where(
                        WhatsAppOrderSession.company_id == 1,
                        WhatsAppOrderSession.conversation_id == conversation_id,
                    )
                )
                pending_state = (
                    pending_session.status,
                    pending_session.handoff_reason,
                    pending_session.missing_fields_json,
                    pending_session.draft_json,
                    pending_session.finalization_job_key,
                ) if pending_session else None
                pending_jobs = state_db.execute(
                    select(BackgroundJob.job_type, BackgroundJob.status)
                    .where(BackgroundJob.company_id == 1)
                    .order_by(BackgroundJob.id)
                ).all()
                latest_customer_message = state_db.scalar(
                    select(InboundMessage)
                    .where(
                        InboundMessage.company_id == 1,
                        InboundMessage.conversation_id == conversation_id,
                        InboundMessage.direction == "inbound",
                    )
                    .order_by(InboundMessage.received_at.desc(), InboundMessage.id.desc())
                    .limit(1)
                )
                latest_body = latest_customer_message.original_content if latest_customer_message else None
                latest_close_detected = has_explicit_order_close(latest_body)
            self.assertEqual(
                pending_state[0] if pending_state else None,
                "ready_for_review",
                msg=(
                    f"pending_state={pending_state!r}; jobs={pending_jobs!r}; "
                    f"classifier_calls={classifier.call_count}; extractor_calls={extractor.call_count}; latest_body={latest_body!r}; "
                    f"close_detected={latest_close_detected}"
                ),
            )
            self.assertIsNotNone(pending_state[4])
            self.assertIn(("whatsapp_finalize_session", "queued"), pending_jobs)
            finalization_cycle = run_worker_cycle(max_jobs=1)

        db = self.TenantSession()
        session = db.scalar(
            select(WhatsAppOrderSession).where(
                WhatsAppOrderSession.company_id == 1,
                WhatsAppOrderSession.conversation_id == conversation_id,
            )
        )
        order = db.scalar(select(Order).where(Order.company_id == 1))
        order_lines = [
            (item.validated_product_id, item.quantity, item.unit)
            for item in sorted(order.lines, key=lambda item: item.id)
        ] if order else []
        linked_message_ids = db.scalars(
            select(InboundMessage.id).where(
                InboundMessage.company_id == 1,
                InboundMessage.conversation_id == conversation_id,
                InboundMessage.direction == "inbound",
                InboundMessage.order_id == (order.id if order else -1),
            )
        ).all()
        order_count = db.scalar(select(func.count()).select_from(Order))
        session_state = (session.status, session.order_id, json.loads(session.draft_json)) if session else None
        db.close()

        self.assertEqual(finalization_cycle["processed"], 1)
        self.assertEqual(classifier.call_count, 5)
        self.assertEqual(extractor.call_count, 4)
        self.assertEqual(send_reply.call_count, 4)
        self.assertEqual(order_count, 1)
        self.assertIsNotNone(order)
        self.assertEqual(order.customer_id, 1)
        self.assertEqual(order_lines, [(1, 6, "uds"), (2, 2, "uds"), (3, 3, "cajas")])
        self.assertEqual(session_state[0:2], ("closed_review", order.id))
        self.assertEqual(session_state[2]["notes"], "Entrega el jueves")
        source_ids_by_reference = {
            line["reference"]: line["source_message_ids"]
            for line in session_state[2]["lines"]
        }
        self.assertEqual(
            source_ids_by_reference["P-100"],
            [message_ids_by_external_id["wa-multiturn-order-4"]],
        )
        self.assertEqual(
            source_ids_by_reference["V-200"],
            [message_ids_by_external_id["wa-multiturn-order-2"]],
        )
        self.assertEqual(
            source_ids_by_reference["P-300"],
            [message_ids_by_external_id["wa-multiturn-order-pdf"]],
        )
        self.assertEqual(len(linked_message_ids), 5)

    def test_late_pdf_is_included_when_customer_closes_before_download_finishes(self):
        with self.TenantSession() as db:
            db.add(
                Product(
                    company_id=1,
                    reference="P-300",
                    name="Tercer producto WhatsApp",
                    sale_unit="cajas",
                    sale_price=4.0,
                    status="active",
                )
            )
            db.commit()

        def classify_turn(_db, _company_id, text, **_kwargs):
            if "nada m" in str(text).casefold():
                return {"tipo_correo": "no_pedido", "confianza": 0.99, "motivo": "Cierre explÃ­cito"}
            return {"tipo_correo": "pedido", "confianza": 0.97, "motivo": "Pedido"}

        def extract_turn(_db, _company_id, transcript, _inbound_message, **_kwargs):
            normalized = str(transcript).casefold()
            lines = []
            if "p-100" in normalized:
                lines.append(
                    {
                        "texto_original": "5 unidades de P-100",
                        "referencia_detectada": "P-100",
                        "producto_detectado": "Producto WhatsApp",
                        "cantidad": 5,
                        "unidad": "uds",
                    }
                )
            if "p-300" in normalized:
                lines.append(
                    {
                        "texto_original": "3 cajas de P-300",
                        "referencia_detectada": "P-300",
                        "producto_detectado": "Tercer producto WhatsApp",
                        "cantidad": 3,
                        "unidad": "cajas",
                    }
                )
            return {
                "cliente": {"nombre_detectado": "Cliente WhatsApp SL"},
                "pedido": {"lineas": lines},
            }

        received_at = datetime.now(timezone.utc) - timedelta(seconds=45)

        def make_message_event(message):
            return parse_payload_events(
                {
                    "entry": [
                        {
                            "id": "ba-123",
                            "changes": [
                                {
                                    "field": "messages",
                                    "value": {
                                        "metadata": {
                                            "phone_number_id": "pn-123",
                                            "business_account_id": "ba-123",
                                        },
                                        "messages": [message],
                                    },
                                }
                            ],
                        }
                    ]
                }
            )[0]

        text_event = make_message_event(
            {
                "id": "wa-late-pdf-order-text",
                "from": "+34600000000",
                "timestamp": str(int(received_at.timestamp())),
                "type": "text",
                "text": {"body": "Hola, quiero 5 unidades de P-100."},
            }
        )
        with self.TenantSession() as db:
            first_message = persist_event(db, 1, text_event)
            conversation_id = first_message.conversation_id
            first_job = enqueue_whatsapp_processing(db, 1, first_message.id)
            first_job_id = first_job.id

        pdf_event = make_message_event(
            {
                "id": "wa-late-pdf-order-document",
                "from": "+34600000000",
                "timestamp": str(int((received_at + timedelta(seconds=5)).timestamp())),
                "type": "document",
                "document": {
                    "id": "meta-late-pdf-order",
                    "filename": "pedido.pdf",
                    "mime_type": "application/pdf",
                    "caption": "Te adjunto el resto del pedido.",
                },
            }
        )
        close_event = make_message_event(
            {
                "id": "wa-late-pdf-order-close",
                "from": "+34600000000",
                "timestamp": str(int((received_at + timedelta(seconds=10)).timestamp())),
                "type": "text",
                "text": {"body": "Nada m\u00e1s, gracias."},
            }
        )

        with (
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
                side_effect=classify_turn,
            ) as classifier,
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.extract_inbound_order",
                side_effect=extract_turn,
            ) as extractor,
            patch(
                "app.whatsapp.agent.await_send_automatic_response",
                return_value={"sent": True, "reason": "sent"},
            ),
            patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession),
        ):
            first_cycle = run_worker_cycle(company_id=1, job_id=first_job_id)
            self.assertEqual(first_cycle["processed"], 1)

            with self.TenantSession() as db:
                pdf_message = persist_event(db, 1, pdf_event)
                self.assertEqual(pdf_message.conversation_id, conversation_id)
                media_job = enqueue_whatsapp_media_download(db, 1, pdf_message.id)
                pdf_message_id = pdf_message.id
                media_job_id = media_job.id

            with self.TenantSession() as db:
                close_message = persist_event(db, 1, close_event)
                close_job = enqueue_whatsapp_processing(db, 1, close_message.id)
                close_message_id = close_message.id
                close_job_id = close_job.id

            close_cycle = run_worker_cycle(company_id=1, job_id=close_job_id)
            self.assertEqual(close_cycle["processed"], 1)
            with self.TenantSession() as db:
                pending_session = db.scalar(
                    select(WhatsAppOrderSession).where(
                        WhatsAppOrderSession.company_id == 1,
                        WhatsAppOrderSession.conversation_id == conversation_id,
                    )
                )
                close_message = db.get(InboundMessage, close_message_id)
                self.assertEqual(pending_session.last_processed_message_id, close_message_id)
                self.assertEqual(close_message.processing_step, "whatsapp_media_pending")
                self.assertEqual(
                    db.scalar(select(func.count()).select_from(Order)),
                    0,
                    "No debe cerrarse el pedido mientras el PDF siga pendiente.",
                )
                attachment = db.scalar(
                    select(MessageAttachment).where(
                        MessageAttachment.company_id == 1,
                        MessageAttachment.inbound_message_id == pdf_message_id,
                    )
                )
                self.assertEqual(attachment.extraction_status, "pending")
                attachment.extraction_status = "extracted"
                attachment.extracted_text = "Pedido adicional: 3 cajas de P-300."
                attachment.content_type = "application/pdf"
                attachment.is_pdf = True
                process_job = enqueue_whatsapp_processing(db, 1, pdf_message_id)
                process_job_id = process_job.id

            # The PDF download job remains queued: target only the reprocessing job
            # created after extraction completed, as the worker does in production.
            self.assertIsNotNone(media_job_id)
            media_cycle = run_worker_cycle(company_id=1, job_id=process_job_id)
            self.assertEqual(media_cycle["processed"], 1)

            with self.TenantSession() as db:
                pending_media_job = db.get(BackgroundJob, media_job_id)
                session = db.scalar(
                    select(WhatsAppOrderSession).where(
                        WhatsAppOrderSession.company_id == 1,
                        WhatsAppOrderSession.conversation_id == conversation_id,
                    )
                )
                draft = json.loads(session.draft_json)
                lines_by_reference = {line["reference"]: line for line in draft["lines"]}
                finalization_job = db.scalar(
                    select(BackgroundJob).where(
                        BackgroundJob.company_id == 1,
                        BackgroundJob.job_type == "whatsapp_finalize_session",
                        BackgroundJob.status == "queued",
                    )
                )

        self.assertEqual(classifier.call_count, 2)
        self.assertEqual(extractor.call_count, 2)
        self.assertIn("P-100", lines_by_reference)
        self.assertIn("P-300", lines_by_reference)
        self.assertEqual(lines_by_reference["P-300"]["source_message_ids"], [pdf_message_id])
        self.assertEqual(session.status, "ready_for_review")
        self.assertIsNotNone(finalization_job)
        self.assertEqual(pending_media_job.status, "queued")

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
                                        "text": {"body": "Pedido de 5 unidades de P-100 para Cliente WhatsApp SL. Nada más, gracias"},
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
        self.assertTrue(db.get(Conversation, message.conversation_id).whatsapp_bot_enabled)
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
        with patch("app.whatsapp.agent.evaluate_whatsapp_agent_turn", return_value=self._complete_agent_turn()), patch(
            "app.agent.platform.classify_sample", return_value={"ok": True, "content": classification}
        ), patch(
            "app.agent.platform.extract_sample", return_value={"ok": True, "content": extraction}
        ), patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession):
            summary = run_worker_cycle()

        db = self.TenantSession()
        self.assertEqual(summary["processed"], 2)
        order = db.scalar(select(Order).where(Order.company_id == 1))
        self.assertIsNotNone(order)
        self.assertEqual(order.customer_id, 1)
        self.assertEqual(order.lines[0].validated_product_id, 1)
        conversation = db.scalar(select(Conversation).where(Conversation.company_id == 1))
        self.assertIsNotNone(conversation)
        db.close()

    def test_exhausted_whatsapp_agent_job_creates_visible_handoff_and_acknowledges_customer(self):
        payload = {
            "entry": [
                {
                    "id": "ba-123",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": "pn-123"},
                                "messages": [
                                    {
                                        "id": "wa-agent-failure-1",
                                        "from": "+34600000000",
                                        "type": "text",
                                        "text": {"body": "Quiero 5 cajas del producto P-100"},
                                    }
                                ],
                            },
                        }
                    ],
                }
            ]
        }
        with self.TenantSession() as db:
            message = persist_event(db, 1, parse_payload_events(payload)[0])
            conversation = db.get(Conversation, message.conversation_id)
            self.assertTrue(conversation.whatsapp_bot_enabled)
            job = enqueue_whatsapp_processing(db, 1, message.id)
            message_id = message.id
            job_id = job.id

        with (
            patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession),
            patch(
                "app.workers.jobs_worker._process_job",
                side_effect=RuntimeError("synthetic exhausted processing error"),
            ),
            patch(
                "app.whatsapp.agent.await_send_automatic_response",
                return_value={"sent": True, "reason": "sent"},
            ) as acknowledgement,
        ):
            summary = run_worker_cycle(max_jobs=1)

        with self.TenantSession() as db:
            message = db.get(InboundMessage, message_id)
            conversation = db.get(Conversation, message.conversation_id)
            job = db.get(BackgroundJob, job_id)
            alert = db.scalar(
                select(Alert).where(
                    Alert.company_id == 1,
                    Alert.inbound_message_id == message_id,
                    Alert.alert_type == "whatsapp_agent_processing_failed",
                    Alert.status == "open",
                )
            )
            self.assertEqual(job.status, "failed")
            self.assertEqual(message.status, "doubtful")
            self.assertEqual(message.processing_step, "whatsapp_agent_failed")
            self.assertEqual(
                message.processing_error,
                "No se pudo completar el procesamiento automático tras varios intentos.",
            )
            self.assertEqual(conversation.status, "human_owned")
            self.assertFalse(conversation.whatsapp_bot_enabled)
            self.assertIsNotNone(alert)
            self.assertEqual(alert.title, "Un mensaje de WhatsApp necesita atención")
        self.assertEqual(summary["attempted"], 1)
        self.assertEqual(summary["processed"], 0)
        acknowledgement.assert_called_once()
        self.assertIn("pendiente de revisión", acknowledgement.call_args.kwargs["body"])

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
        self.assertTrue(db.get(Conversation, message.conversation_id).whatsapp_bot_enabled)
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

    def test_worker_retries_retryable_pipeline_result(self):
        upgrade_tenant_schema(self.tenant_engine, company_id=1, application_version="1.2.3")
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "pn-123", "business_account_id": "ba-123"},
                                "messages": [
                                    {
                                        "id": "wa-retry-1",
                                        "from": "+34600000000",
                                        "type": "text",
                                        "text": {"body": "Pedido de 5 unidades de P-100. Confirma el pedido"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        db = self.TenantSession()
        message = persist_event(db, 1, parse_payload_events(payload)[0])
        self.assertTrue(db.get(Conversation, message.conversation_id).whatsapp_bot_enabled)
        job = enqueue_whatsapp_processing(db, 1, message.id)
        db.close()

        with patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession), patch(
            "app.whatsapp.agent.evaluate_whatsapp_agent_turn", return_value=self._complete_agent_turn()
        ), patch(
            "app.agent.platform.UnifiedOrderPipelineService.process_inbound_message",
            return_value={"ok": False, "retryable": True, "error_type": "provider_unavailable", "message": "Proveedor temporalmente no disponible"},
        ):
            summary = run_worker_cycle()

        db = self.TenantSession()
        processed_job = db.get(BackgroundJob, job.id)
        finalization_job = db.scalar(
            select(BackgroundJob).where(
                BackgroundJob.company_id == 1,
                BackgroundJob.job_type == "whatsapp_finalize_session",
            )
        )
        self.assertEqual(summary["processed"], 1)
        self.assertEqual(processed_job.status, "success")
        self.assertIsNotNone(finalization_job)
        self.assertEqual(finalization_job.status, "retrying")
        self.assertEqual(finalization_job.last_error_type, "provider_unavailable")
        db.close()

    def test_worker_retries_a_definitively_rate_limited_whatsapp_reply(self):
        upgrade_tenant_schema(self.tenant_engine, company_id=1, application_version="1.2.3")
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "pn-123", "business_account_id": "ba-123"},
                                "messages": [
                                    {
                                        "id": "wa-agent-rate-limit-1",
                                        "from": "+34600000000",
                                        "type": "text",
                                        "text": {"body": "Hola"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        db = self.TenantSession()
        message = persist_event(db, 1, parse_payload_events(payload)[0])
        job = enqueue_whatsapp_processing(db, 1, message.id)
        db.close()

        delivery_error = WhatsAppAgentReplyDeliveryError("rate_limited", retryable=True)
        with patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession), patch(
            "app.workers.jobs_worker.process_whatsapp_agent_turn",
            side_effect=delivery_error,
        ):
            summary = run_worker_cycle()

        db = self.TenantSession()
        retried_job = db.get(BackgroundJob, job.id)
        self.assertEqual(summary["attempted"], 1)
        self.assertEqual(summary["processed"], 0)
        self.assertEqual(retried_job.status, "retrying")
        self.assertEqual(retried_job.last_error_type, "rate_limited")
        self.assertIsNotNone(retried_job.next_retry_at)
        db.close()

    def test_exhausted_media_download_retries_create_human_handoff(self):
        upgrade_tenant_schema(self.tenant_engine, company_id=1, application_version="1.2.3")
        db = self.TenantSession()
        self.addCleanup(db.close)
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "pn-123", "business_account_id": "ba-123"},
                                "messages": [
                                    {
                                        "id": "wa-media-retry-exhausted-1",
                                        "from": "+34600000000",
                                        "type": "document",
                                        "document": {
                                            "id": "media-retry-exhausted-1",
                                            "filename": "pedido.pdf",
                                            "mime_type": "application/pdf",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        message = persist_event(db, 1, parse_payload_events(payload)[0])
        message.original_content = "Adjunto el pedido, por favor."
        conversation = db.get(Conversation, message.conversation_id)
        self.assertTrue(conversation.whatsapp_bot_enabled)
        download_job = enqueue_whatsapp_media_download(db, 1, message.id)
        download_job.max_retries = 0
        db.commit()
        message_id = message.id
        download_job_id = download_job.id
        db.close()

        with patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession), patch(
            "app.whatsapp.service.download_whatsapp_media",
            new=AsyncMock(side_effect=WhatsAppEmbeddedSignupError("temporary media timeout", error_type="timeout")),
        ):
            failed_cycle = run_worker_cycle(max_jobs=1)

        db = self.TenantSession()
        self.addCleanup(db.close)
        failed_job = db.get(BackgroundJob, download_job_id)
        attachment = db.scalar(select(MessageAttachment).where(MessageAttachment.inbound_message_id == message_id))
        self.assertEqual(failed_cycle["attempted"], 1)
        self.assertEqual(failed_job.status, "failed")
        self.assertEqual(attachment.extraction_status, "extraction_error")
        agent_job = db.scalar(
            select(BackgroundJob).where(
                BackgroundJob.company_id == 1,
                BackgroundJob.job_type == "process_inbound_message",
            )
        )
        self.assertIsNotNone(agent_job)
        db.close()

        with patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession), patch(
            "app.whatsapp.agent.await_send_automatic_response",
            return_value={"sent": True, "reason": "sent"},
        ), patch(
            "app.whatsapp.agent.evaluate_whatsapp_agent_turn",
            return_value=self._complete_agent_turn(),
        ) as evaluator:
            handoff_cycle = run_worker_cycle(max_jobs=1)

        db = self.TenantSession()
        self.addCleanup(db.close)
        session = db.scalar(select(WhatsAppOrderSession).where(WhatsAppOrderSession.company_id == 1))
        self.assertEqual(handoff_cycle["processed"], 1)
        self.assertEqual(session.status, "human_handoff")
        self.assertEqual(db.scalar(select(func.count()).select_from(Order)), 0)
        self.assertEqual(json.loads(session.draft_json)["lines"][0]["source_message_ids"], [])
        evaluator.assert_called_once()
        db.close()

    def test_embedded_signup_exchanges_code_registers_phone_and_subscribes_webhook(self):
        settings = SimpleNamespace(
            meta_whatsapp_embedded_signup_ready=True,
            meta_app_id="12345000000",
            meta_app_secret="server-only-app-secret",
            meta_embedded_signup_config_id="22345000000",
            meta_graph_api_version="v24.0",
            meta_embedded_signup_version="v4",
            meta_whatsapp_registration_pin="123456",
            meta_whatsapp_verify_token="global-verify-token",
            meta_oauth_redirect_uri="",
            meta_request_timeout_seconds=5,
            app_url="https://anchi.example.com",
        )
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            path = request.url.path
            if path.endswith("/oauth/access_token"):
                self.assertEqual(request.url.params["client_id"], settings.meta_app_id)
                self.assertEqual(request.url.params["code"], "temporary-auth-code")
                self.assertNotIn("redirect_uri", request.url.params)
                return httpx.Response(200, json={"access_token": "tenant-access-token"})
            if path.endswith("/12345678901/phone_numbers"):
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "10987654321",
                                "display_phone_number": "+34 600 000 000",
                                "verified_name": "Anchi Demo",
                            }
                        ]
                    },
                )
            if path.endswith("/10987654321/register"):
                self.assertEqual(json.loads(request.content)["pin"], settings.meta_whatsapp_registration_pin)
                self.assertEqual(request.headers["Authorization"], "Bearer tenant-access-token")
                return httpx.Response(200, json={"success": True})
            if path.endswith("/10987654321"):
                return httpx.Response(
                    200,
                    json={
                        "id": "10987654321",
                        "status": "PENDING",
                        "account_mode": "LIVE",
                        "platform_type": "CLOUD_API",
                        "is_on_biz_app": False,
                    },
                )
            if path.endswith("/12345678901/subscribed_apps"):
                if not request.content:
                    return httpx.Response(200, json={"success": True})
                payload = json.loads(request.content)
                self.assertEqual(payload["override_callback_uri"], "https://anchi.example.com/webhooks/whatsapp/whatsapp-demo")
                self.assertTrue(payload["verify_token"])
                return httpx.Response(200, json={"success": True})
            if path.endswith("/12345678901"):
                return httpx.Response(200, json={"id": "12345678901", "name": "Anchi WABA"})
            return httpx.Response(404, json={"error": {"message": "unexpected test request"}})

        async def execute_signup():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                db = self.TenantSession()
                try:
                    return await complete_embedded_signup(
                        db,
                        company_id=1,
                        company_slug="whatsapp-demo",
                        code="temporary-auth-code",
                        business_account_id="12345678901",
                        phone_number_id="10987654321",
                        business_id="11223344556",
                        client=client,
                    )
                finally:
                    db.close()

        with patch("app.whatsapp.service.get_settings", return_value=settings):
            result = asyncio.run(execute_signup())

        self.assertEqual(result.connection_status, "connected")
        self.assertEqual(result.phone_number_id, "10987654321")
        self.assertEqual(len(calls), 7)
        with self.TenantSession() as db:
            config = whatsapp_config(db, 1)
            self.assertTrue(config.enabled)
            self.assertTrue(config.webhook_enabled)
            self.assertEqual(config.connection_status, "connected")
            self.assertEqual(config.access_token, "tenant-access-token")
            self.assertEqual(config.display_phone_number, "+34 600 000 000")
            token_setting = db.scalar(
                select(ChannelSetting).where(
                    ChannelSetting.company_id == 1,
                    ChannelSetting.key == "access_token",
                )
            )
            self.assertNotEqual(token_setting.value, "tenant-access-token")
            self.assertEqual(decrypt_secret(token_setting.value), "tenant-access-token")

    def test_coexistence_signup_discovers_business_app_phone_without_registering_it(self):
        settings = SimpleNamespace(
            meta_whatsapp_embedded_signup_ready=True,
            meta_app_id="12345000000",
            meta_app_secret="server-only-app-secret",
            meta_embedded_signup_config_id="22345000000",
            meta_graph_api_version="v24.0",
            meta_embedded_signup_version="v4",
            meta_whatsapp_registration_pin="",
            meta_whatsapp_verify_token="global-verify-token",
            meta_oauth_redirect_uri="",
            meta_request_timeout_seconds=5,
            app_url="https://anchi.example.com",
        )
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            path = request.url.path
            if path.endswith("/oauth/access_token"):
                return httpx.Response(200, json={"access_token": "coexistence-access-token"})
            if path.endswith("/12345678901/phone_numbers"):
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "10987654321",
                                "display_phone_number": "+34 600 000 000",
                                "verified_name": "Anchi Demo",
                            }
                        ]
                    },
                )
            if path.endswith("/10987654321"):
                self.assertIn("is_on_biz_app", request.url.params["fields"])
                return httpx.Response(
                    200,
                    json={
                        "id": "10987654321",
                        "status": "CONNECTED",
                        "account_mode": "LIVE",
                        "platform_type": "CLOUD_API",
                        "is_on_biz_app": True,
                        "display_phone_number": "+34 600 000 000",
                        "verified_name": "Anchi Demo",
                        "code_verification_status": "VERIFIED",
                    },
                )
            if path.endswith("/12345678901/subscribed_apps"):
                if not request.content:
                    return httpx.Response(200, json={"success": True})
                payload = json.loads(request.content)
                self.assertTrue(payload["override_callback_uri"])
                self.assertTrue(payload["verify_token"])
                return httpx.Response(200, json={"success": True})
            if path.endswith("/12345678901"):
                return httpx.Response(200, json={"id": "12345678901", "name": "Anchi WABA"})
            return httpx.Response(404, json={"error": {"message": "unexpected test request"}})

        async def execute_signup():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.TenantSession() as db:
                    return await complete_embedded_signup(
                        db,
                        company_id=1,
                        company_slug="whatsapp-demo",
                        code="temporary-auth-code",
                        business_account_id="12345678901",
                        phone_number_id="",
                        business_id="11223344556",
                        onboarding_mode="coexistence",
                        client=client,
                    )

        with patch("app.whatsapp.service.get_settings", return_value=settings):
            result = asyncio.run(execute_signup())

        self.assertEqual(result.onboarding_mode, "coexistence")
        self.assertTrue(result.is_on_biz_app)
        self.assertEqual(result.phone_number_id, "10987654321")
        self.assertFalse(any(request.url.path.endswith("/register") for request in calls))
        self.assertEqual(len(calls), 6)
        with self.TenantSession() as db:
            config = whatsapp_config(db, 1)
            self.assertEqual(config.onboarding_mode, "coexistence")
            self.assertTrue(config.is_on_biz_app)
            self.assertEqual(config.phone_status, "CONNECTED")
            self.assertEqual(config.account_mode, "LIVE")

    def test_cloud_signup_does_not_reregister_an_already_connected_test_phone(self):
        settings = SimpleNamespace(
            meta_whatsapp_embedded_signup_ready=True,
            meta_app_id="12345000000",
            meta_app_secret="server-only-app-secret",
            meta_embedded_signup_config_id="22345000000",
            meta_graph_api_version="v24.0",
            meta_embedded_signup_version="v4",
            meta_whatsapp_registration_pin="",
            meta_whatsapp_verify_token="global-verify-token",
            meta_oauth_redirect_uri="",
            meta_request_timeout_seconds=5,
            app_url="https://anchi.example.com",
        )
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            path = request.url.path
            if path.endswith("/oauth/access_token"):
                return httpx.Response(200, json={"access_token": "connected-test-token"})
            if path.endswith("/12345678901/phone_numbers"):
                return httpx.Response(200, json={"data": [{"id": "10987654321", "display_phone_number": "+34 600 000 000"}]})
            if path.endswith("/10987654321"):
                return httpx.Response(200, json={"id": "10987654321", "status": "CONNECTED", "platform_type": "CLOUD_API"})
            if path.endswith("/12345678901/subscribed_apps"):
                if request.content:
                    payload = json.loads(request.content)
                    self.assertIn("override_callback_uri", payload)
                    self.assertIn("verify_token", payload)
                return httpx.Response(200, json={"success": True})
            if path.endswith("/12345678901"):
                return httpx.Response(200, json={"id": "12345678901", "name": "Anchi WABA"})
            return httpx.Response(404, json={"error": {"message": "unexpected test request"}})

        async def execute_signup():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.TenantSession() as db:
                    return await complete_embedded_signup(
                        db,
                        company_id=1,
                        company_slug="whatsapp-demo",
                        code="temporary-auth-code",
                        business_account_id="12345678901",
                        phone_number_id="10987654321",
                        client=client,
                    )

        with patch("app.whatsapp.service.get_settings", return_value=settings):
            result = asyncio.run(execute_signup())

        self.assertEqual(result.phone_number_id, "10987654321")
        self.assertEqual(len(calls), 6)
        self.assertFalse(any(request.url.path.endswith("/register") for request in calls))

    def test_coexistence_webhooks_separate_live_echo_history_and_contact_sync(self):
        payload = {
            "entry": [
                {
                    "id": "ba-123",
                    "changes": [
                        {
                            "field": "smb_message_echoes",
                            "value": {
                                "metadata": {"phone_number_id": "pn-123", "display_phone_number": "+34610000000"},
                                "contacts": [{"wa_id": "+34600000000"}],
                                "message_echoes": [
                                    {
                                        "id": "wa-echo-1",
                                        "from": "+34610000000",
                                        "to": "+34600000000",
                                        "timestamp": "1787600000",
                                        "type": "text",
                                        "text": {"body": "Te confirmo el pedido desde el móvil"},
                                    }
                                ],
                            },
                        },
                        {
                            "field": "history",
                            "value": {
                                "metadata": {"phone_number_id": "pn-123", "display_phone_number": "+34610000000"},
                                "history": [
                                    {
                                        "metadata": {"phase": 0, "chunk_order": 1, "progress": 100},
                                        "threads": [
                                            {
                                                "id": "+34600000000",
                                                "messages": [
                                                    {
                                                        "id": "wa-history-in-1",
                                                        "from": "+34600000000",
                                                        "timestamp": "1787500000",
                                                        "type": "text",
                                                        "text": {"body": "Necesito diez unidades"},
                                                        "history_context": {"status": "READ"},
                                                    },
                                                    {
                                                        "id": "wa-history-out-1",
                                                        "from": "+34610000000",
                                                        "to": "+34600000000",
                                                        "timestamp": "1787500100",
                                                        "type": "text",
                                                        "text": {"body": "Pedido recibido"},
                                                        "history_context": {"status": "DELIVERED"},
                                                    },
                                                ],
                                            }
                                        ],
                                    }
                                ],
                            },
                        },
                        {
                            "field": "smb_app_state_sync",
                            "value": {
                                "metadata": {"phone_number_id": "pn-123"},
                                "state_sync": [
                                    {
                                        "type": "contact",
                                        "action": "add",
                                        "contact": {"full_name": "Cliente", "phone_number": "+34600000000"},
                                        "metadata": {"timestamp": "1787600100"},
                                    }
                                ],
                            },
                        },
                    ],
                }
            ]
        }

        events = parse_payload_events(payload)
        self.assertEqual(
            [event["kind"] for event in events],
            ["message_echo", "history_sync", "history_message", "history_message", "contact_sync"],
        )
        with self.TenantSession() as db:
            channel = db.scalar(
                select(InputChannel).where(
                    InputChannel.company_id == 1,
                    InputChannel.key == "whatsapp",
                )
            )
            conversation = Conversation(
                company_id=1,
                channel_id=channel.id,
                provider="meta",
                external_thread_id=events[0]["external_thread_id"],
                status="open",
                whatsapp_bot_enabled=True,
            )
            db.add(conversation)
            db.commit()
            stored = [persist_event(db, 1, event) for event in events]
            self.assertIsNone(stored[1])
            self.assertIsNone(stored[4])
            echo = db.scalar(select(InboundMessage).where(InboundMessage.source_external_id == "wa-echo-1"))
            history_in = db.scalar(select(InboundMessage).where(InboundMessage.source_external_id == "wa-history-in-1"))
            history_out = db.scalar(select(InboundMessage).where(InboundMessage.source_external_id == "wa-history-out-1"))
            self.assertEqual(echo.direction, "outbound")
            self.assertEqual(echo.processing_step, "echoed_from_business_app")
            db.refresh(conversation)
            self.assertFalse(conversation.whatsapp_bot_enabled)
            self.assertEqual(conversation.status, "human_owned")
            self.assertEqual(history_in.direction, "inbound")
            self.assertEqual(history_in.processing_step, "history_synced")
            self.assertEqual(history_in.status, "read")
            self.assertEqual(history_out.direction, "outbound")
            self.assertEqual(history_out.status, "delivered")
            settings = {
                item.key: item.value
                for item in db.scalars(select(ChannelSetting).where(ChannelSetting.company_id == 1)).all()
            }
            self.assertEqual(settings["last_history_sync_progress"], "100")
            self.assertEqual(settings["last_contact_sync_action"], "add")

    def test_stale_outbound_reservation_becomes_unknown_without_retry(self):
        now = datetime.now(timezone.utc)
        with self.TenantSession() as db:
            channel = db.scalar(
                select(InputChannel).where(
                    InputChannel.company_id == 1,
                    InputChannel.key == "whatsapp",
                )
            )
            stale = InboundMessage(
                company_id=1,
                channel_id=channel.id,
                provider="meta",
                source_external_id="wa-pending-stale",
                direction="outbound",
                original_content="Mensaje con resultado desconocido",
                content_type="text",
                status="sending",
                created_at=now - timedelta(minutes=10),
                updated_at=now - timedelta(minutes=10),
            )
            recent = InboundMessage(
                company_id=1,
                channel_id=channel.id,
                provider="meta",
                source_external_id="wa-pending-recent",
                direction="outbound",
                original_content="Mensaje que sigue enviándose",
                content_type="text",
                status="sending",
                created_at=now,
                updated_at=now,
            )
            db.add_all([stale, recent])
            db.commit()

            recovered = recover_stale_whatsapp_outbound_sends(
                db,
                company_id=1,
                now=now,
                stale_after_seconds=120,
            )
            db.refresh(stale)
            db.refresh(recent)
            alert = db.scalar(
                select(Alert).where(
                    Alert.company_id == 1,
                    Alert.inbound_message_id == stale.id,
                    Alert.alert_type == "whatsapp_outbound_unknown",
                )
            )

        self.assertEqual(recovered, 1)
        self.assertEqual(stale.status, "send_unknown")
        self.assertEqual(recent.status, "sending")
        self.assertIsNotNone(alert)

    def test_webhook_tenant_resolution_by_slug_and_meta_identifiers(self):
        master_db = self.MasterSession()
        company, tenant = resolve_company_from_slug(master_db, "whatsapp-demo")
        self.assertIsNotNone(company)
        self.assertIsNotNone(tenant)
        self.assertEqual(company.id, 1)
        company, tenant = resolve_company_from_whatsapp_identifiers(
            master_db,
            business_account_id="ba-123",
            phone_number_id="pn-123",
        )
        self.assertIsNotNone(company)
        self.assertIsNotNone(tenant)
        self.assertEqual(company.id, 1)
        master_db.close()
