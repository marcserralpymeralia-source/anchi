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
from unittest.mock import patch
from zipfile import ZipFile

import httpx
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.agent.platform import UnifiedOrderPipelineService
from app.core.attachment_extraction import extract_attachment_text
from app.core.encryption import decrypt_secret, encrypt_secret
from app.db.database import Base
from app.db.models import BackgroundJob, ChannelSetting, Conversation, Customer, CustomerContactPoint, InboundMessage, InputChannel, LLMSettings, MessageAttachment, Order, Product, ProductAlias, ScoringSettings
from app.jobs.service import enqueue_job
from app.master.database import MasterBase
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser
from app.master.service import TenantRole, TenantUser
from app.messages.service import upsert_inbound_message
from app.tenancy.database import get_tenant_engine
from app.tenancy.migrations import upgrade_tenant_schema
from app.whatsapp.service import (
    complete_embedded_signup,
    download_whatsapp_media,
    enqueue_whatsapp_processing,
    parse_payload_events,
    persist_event,
    redact_whatsapp_config,
    record_manual_response,
    send_manual_response,
    resolve_company_from_slug,
    resolve_company_from_whatsapp_identifiers,
    verify_signature,
    verify_webhook_token,
    whatsapp_event_matches_config,
    whatsapp_ingress_is_ready,
    whatsapp_config,
)
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
            "connection_status": "connected",
            "webhook_enabled": "true",
            "bot_enabled": "true",
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
        self.assertNotIn("app_secret", redacted)

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

    def test_media_policy_only_allows_documents_text_and_audio(self):
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
        self.assertFalse(event["attachments"][0]["downloadable"])

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

    def test_audio_download_waits_for_transcription_before_pipeline(self):
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
            self.assertFalse(result["ready_for_processing"])
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
            self.assertEqual(request_payload["to"], "+34600000000")
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
                                        "text": {"body": "Reintentar. Confirma el pedido"},
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

        with patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession), patch(
            "app.agent.platform.UnifiedOrderPipelineService.process_inbound_message",
            return_value={"ok": False, "retryable": True, "error_type": "provider_unavailable", "message": "Proveedor temporalmente no disponible"},
        ):
            summary = run_worker_cycle()

        db = self.TenantSession()
        processed_job = db.get(BackgroundJob, job.id)
        self.assertEqual(summary["processed"], 0)
        self.assertEqual(processed_job.status, "retrying")
        self.assertEqual(processed_job.last_error_type, "provider_unavailable")
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
            stored = [persist_event(db, 1, event) for event in events]
            self.assertIsNone(stored[1])
            self.assertIsNone(stored[4])
            echo = db.scalar(select(InboundMessage).where(InboundMessage.source_external_id == "wa-echo-1"))
            history_in = db.scalar(select(InboundMessage).where(InboundMessage.source_external_id == "wa-history-in-1"))
            history_out = db.scalar(select(InboundMessage).where(InboundMessage.source_external_id == "wa-history-out-1"))
            self.assertEqual(echo.direction, "outbound")
            self.assertEqual(echo.processing_step, "echoed_from_business_app")
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
