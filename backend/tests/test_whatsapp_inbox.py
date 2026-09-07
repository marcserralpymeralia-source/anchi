from __future__ import annotations

import os
import unittest
import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")

from app.core import lifespan as lifespan_module  # noqa: E402
from app.core.app_factory import create_app  # noqa: E402
from app.db.models import ChannelSetting, InboundMessage, InputChannel, MessageAttachment, utcnow  # noqa: E402
from app.messages.service import upsert_inbound_message  # noqa: E402
from app.whatsapp.service import send_whatsapp_media  # noqa: E402
from scripts.performance_data import build_performance_fixture, temporary_performance_environment  # noqa: E402


class WhatsAppInboxTests(unittest.TestCase):
    def _client_for(self, fixture):
        context = temporary_performance_environment(fixture)
        context.__enter__()
        app = create_app()
        patches = (
            patch.object(lifespan_module, "start_email_sync_worker", lambda: None),
            patch.object(lifespan_module, "start_job_worker", lambda: None),
        )
        for item in patches:
            item.__enter__()
        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()

        def cleanup():
            client.__exit__(None, None, None)
            for item in reversed(patches):
                item.__exit__(None, None, None)
            context.__exit__(None, None, None)

        return client, cleanup

    def _seed_whatsapp(self, fixture, *, active: bool = True, configured: bool = True) -> int:
        engine = create_engine(fixture.tenant_database_url, connect_args={"check_same_thread": False})
        session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        with session() as db:
            channel = db.scalar(select(InputChannel).where(InputChannel.company_id == 1, InputChannel.key == "whatsapp"))
            if channel is None:
                channel = InputChannel(
                    company_id=1,
                    key="whatsapp",
                    name="WhatsApp",
                    channel_type="message",
                    is_active=active,
                    supports_text=True,
                    supports_attachments=True,
                    supports_audio=True,
                    supports_documents=True,
                    supports_images=True,
                )
                db.add(channel)
                db.flush()
            else:
                channel.is_active = active
            if configured:
                values = {
                    "enabled": "true",
                    "provider": "meta",
                    "phone_number_id": "phone-test",
                    "business_account_id": "business-test",
                    "access_token": "token-test",
                    "verify_token": "verify-test",
                    "connection_status": "connected",
                    "webhook_enabled": "true",
                }
                for key, value in values.items():
                    setting = db.scalar(
                        select(ChannelSetting).where(
                            ChannelSetting.company_id == 1,
                            ChannelSetting.channel_id == channel.id,
                            ChannelSetting.key == key,
                        )
                    )
                    if setting is None:
                        setting = ChannelSetting(company_id=1, channel_id=channel.id, key=key)
                        db.add(setting)
                    setting.value = value
            db.flush()
            message, conversation = upsert_inbound_message(
                db,
                company_id=1,
                channel_key="whatsapp",
                provider="meta",
                external_id="wamid.inbox-test",
                sender="+34600000000",
                recipients=["+34910000000"],
                subject="Consulta de WhatsApp",
                text_content="Necesitamos confirmar la entrega.",
                external_thread_id="+34600000000",
                received_at=utcnow() - timedelta(minutes=2),
                content_type="text",
            )
            message.status = "received"
            db.commit()
            conversation_id = conversation.id
        engine.dispose()
        return conversation_id

    def _login(self, client, fixture):
        return client.post(
            "/login",
            data={"email": fixture.admin_email, "password": fixture.admin_password, "next": "/whatsapp/inbox"},
            follow_redirects=False,
        )

    def test_inbox_lists_only_active_whatsapp_conversations_and_renders_split_chat(self):
        fixture = build_performance_fixture("small")
        conversation_id = self._seed_whatsapp(fixture)
        client, cleanup = self._client_for(fixture)
        try:
            self._login(client, fixture)
            response = client.get(f"/whatsapp/inbox?conversation_id={conversation_id}")
        finally:
            cleanup()
            fixture.cleanup()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Buzón de WhatsApp", response.text)
        self.assertIn("Contactos", response.text)
        self.assertIn("Necesitamos confirmar la entrega.", response.text)
        self.assertIn('href="/whatsapp/inbox"', response.text)
        self.assertIn("Buzón de correo", response.text)
        self.assertIn('name="files"', response.text)
        self.assertEqual(response.text.count('href="/mail"'), 1)
        self.assertEqual(response.text.count('class="nav-label">WhatsApp</span>'), 1)

    def test_inactive_channel_is_not_available(self):
        fixture = build_performance_fixture("small")
        self._seed_whatsapp(fixture, active=False, configured=False)
        client, cleanup = self._client_for(fixture)
        try:
            self._login(client, fixture)
            response = client.get("/whatsapp/inbox")
        finally:
            cleanup()
            fixture.cleanup()
        self.assertEqual(response.status_code, 404)

    def test_reply_accepts_supported_attachment_and_delegates_to_whatsapp_service(self):
        fixture = build_performance_fixture("small")
        conversation_id = self._seed_whatsapp(fixture)
        client, cleanup = self._client_for(fixture)
        try:
            self._login(client, fixture)
            with patch(
                "app.whatsapp.inbox_routes.send_manual_response",
                new=AsyncMock(return_value=InboundMessage(id=99)),
            ) as send_response:
                response = client.post(
                    f"/whatsapp/inbox/{conversation_id}/reply",
                    data={"body": "Adjunto la confirmación."},
                    files={"files": ("confirmacion.pdf", b"%PDF-1.4 demo", "application/pdf")},
                    follow_redirects=False,
                )
        finally:
            cleanup()
            fixture.cleanup()

        self.assertEqual(response.status_code, 303)
        self.assertIn("notice=sent", response.headers["location"])
        self.assertTrue(send_response.await_count == 1)
        payload = send_response.await_args.kwargs["attachments"]
        self.assertEqual(payload[0]["filename"], "confirmacion.pdf")
        self.assertEqual(payload[0]["content_type"], "application/pdf")

    def test_media_send_uploads_file_then_sends_document_message(self):
        fixture = build_performance_fixture("small")
        conversation_id = self._seed_whatsapp(fixture)
        engine = create_engine(fixture.tenant_database_url, connect_args={"check_same_thread": False})
        session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        with session() as db:
            def handler(request):
                if request.url.path.endswith("/media"):
                    self.assertIn("multipart/form-data", request.headers.get("content-type", ""))
                    return httpx.Response(200, json={"id": "media-test-1"})
                self.assertTrue(request.url.path.endswith("/messages"))
                payload = json.loads(request.content)
                self.assertEqual(payload["type"], "document")
                self.assertEqual(payload["document"]["id"], "media-test-1")
                return httpx.Response(200, json={"messages": [{"id": "wamid.document-1"}]})

            async def run_send():
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    return await send_whatsapp_media(
                        db,
                        company_id=1,
                        conversation_id=conversation_id,
                        content=b"contenido de prueba",
                        filename="pedido.docx",
                        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        client=client,
                    )

            result = asyncio.run(run_send())
        engine.dispose()
        fixture.cleanup()
        self.assertEqual(result["provider_message_id"], "wamid.document-1")

    def test_delivery_ticks_and_authentic_chat_rendering(self):
        fixture = build_performance_fixture("small")
        conversation_id = self._seed_whatsapp(fixture)
        engine = create_engine(fixture.tenant_database_url, connect_args={"check_same_thread": False})
        session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        with session() as db:
            # Seed 3 outbound messages with different statuses
            m_sent, _ = upsert_inbound_message(
                db,
                company_id=1,
                channel_key="whatsapp",
                provider="meta",
                external_id="wamid.sent-test",
                sender="Anchi",
                recipients=["+34600000000"],
                subject="Mensaje enviado",
                text_content="Mensaje de prueba enviado.",
                external_thread_id="+34600000000",
                received_at=utcnow() - timedelta(minutes=1),
                content_type="text",
                direction="outbound",
            )
            m_sent.status = "sent"

            m_deliv, _ = upsert_inbound_message(
                db,
                company_id=1,
                channel_key="whatsapp",
                provider="meta",
                external_id="wamid.deliv-test",
                sender="Anchi",
                recipients=["+34600000000"],
                subject="Mensaje entregado",
                text_content="Mensaje de prueba entregado.",
                external_thread_id="+34600000000",
                received_at=utcnow() - timedelta(seconds=40),
                content_type="text",
                direction="outbound",
            )
            m_deliv.status = "delivered"

            m_read, _ = upsert_inbound_message(
                db,
                company_id=1,
                channel_key="whatsapp",
                provider="meta",
                external_id="wamid.read-test",
                sender="Anchi",
                recipients=["+34600000000"],
                subject="Mensaje leído",
                text_content="Mensaje de prueba leído.",
                external_thread_id="+34600000000",
                received_at=utcnow() - timedelta(seconds=10),
                content_type="text",
                direction="outbound",
            )
            m_read.status = "read"
            db.commit()
        engine.dispose()

        client, cleanup = self._client_for(fixture)
        try:
            self._login(client, fixture)
            response = client.get(f"/whatsapp/inbox?conversation_id={conversation_id}")
        finally:
            cleanup()
            fixture.cleanup()

        self.assertEqual(response.status_code, 200)
        # Check single tick for sent
        self.assertIn("wa-tick-sent", response.text)
        # Check double tick for delivered
        self.assertIn("wa-tick-delivered", response.text)
        # Check double blue tick for read
        self.assertIn("wa-tick-read", response.text)
        # Check preview tick in contact list
        self.assertIn("preview-tick", response.text)
        # Check date separator
        self.assertIn("channel-chat-date-separator", response.text)

    def test_rich_media_rendering_images_pdf_audio_and_lightbox(self):
        fixture = build_performance_fixture("small")
        conversation_id = self._seed_whatsapp(fixture)
        engine = create_engine(fixture.tenant_database_url, connect_args={"check_same_thread": False})
        session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        with session() as db:
            msg, _ = upsert_inbound_message(
                db,
                company_id=1,
                channel_key="whatsapp",
                provider="meta",
                external_id="wamid.media-rich-test",
                sender="+34618741297",
                recipients=["+34910000000"],
                subject="Archivos multimedia",
                text_content="Aquí tienes la foto, el PDF y la nota de voz.",
                external_thread_id="+34600000000",
                received_at=utcnow(),
                content_type="media",
                direction="inbound",
            )
            msg.conversation_id = conversation_id
            msg.status = "received"
            db.flush()

            att_img = MessageAttachment(
                company_id=1,
                inbound_message_id=msg.id,
                filename="foto-pedido.jpg",
                content_type="image/jpeg",
                size_bytes=102400,
                storage_path="mock/path/foto-pedido.jpg",
                extraction_status="extracted",
                is_image=True,
            )
            att_pdf = MessageAttachment(
                company_id=1,
                inbound_message_id=msg.id,
                filename="albaran.pdf",
                content_type="application/pdf",
                size_bytes=55296,
                storage_path="mock/path/albaran.pdf",
                extraction_status="extracted",
                is_pdf=True,
            )
            att_audio = MessageAttachment(
                company_id=1,
                inbound_message_id=msg.id,
                filename="nota-voz.ogg",
                content_type="audio/ogg",
                size_bytes=40960,
                storage_path="mock/path/nota-voz.ogg",
                extraction_status="extracted",
                is_audio=True,
            )
            att_pending = MessageAttachment(
                company_id=1,
                inbound_message_id=msg.id,
                filename="pendiente.png",
                content_type="image/png",
                size_bytes=0,
                storage_path=None,
                extraction_status="pending",
                is_image=True,
            )
            db.add_all([att_img, att_pdf, att_audio, att_pending])
            db.commit()
            pending_att_id = att_pending.id
        engine.dispose()

        client, cleanup = self._client_for(fixture)
        try:
            self._login(client, fixture)
            response = client.get(f"/whatsapp/inbox?conversation_id={conversation_id}")
            self.assertEqual(response.status_code, 200)
            # Image card rendering and lightbox
            self.assertIn("wa-bubble-media-image", response.text)
            self.assertIn("wa-image-lightbox", response.text)
            self.assertIn("openMediaLightbox", response.text)
            # PDF preview card and action buttons
            self.assertIn("wa-bubble-doc-card", response.text)
            self.assertIn("wa-doc-iframe", response.text)
            self.assertIn("Guardar como…", response.text)
            # Audio player card
            self.assertIn("wa-bubble-audio-card", response.text)
            self.assertIn("wa-audio-control", response.text)
            # Pending media card
            self.assertIn("wa-bubble-pending-card", response.text)

            # Test sync media endpoint
            sync_resp = client.get(f"/whatsapp/inbox/{conversation_id}/sync-media/{pending_att_id}", follow_redirects=False)
            self.assertEqual(sync_resp.status_code, 303)
            self.assertIn(f"/whatsapp/inbox?conversation_id={conversation_id}", sync_resp.headers["location"])
        finally:
            cleanup()
            fixture.cleanup()

if __name__ == "__main__":
    unittest.main()
