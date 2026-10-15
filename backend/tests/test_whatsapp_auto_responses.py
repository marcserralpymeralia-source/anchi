from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import (
    ChannelSetting,
    Company,
    Conversation,
    InboundMessage,
    InputChannel,
    WhatsAppOrderSession,
)
from app.whatsapp.service import (
    automatic_response_status,
    record_automatic_response,
    send_automatic_response,
)


class WhatsAppAutomaticResponseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        db = self.Session()
        db.add(Company(id=1, name="Test"))
        db.add(
            InputChannel(
                id=1,
                company_id=1,
                key="whatsapp",
                name="WhatsApp",
                channel_type="message",
                is_active=True,
            )
        )
        db.add_all(
            [
                ChannelSetting(company_id=1, channel_id=1, key="enabled", value="true"),
                ChannelSetting(company_id=1, channel_id=1, key="max_auto_messages", value="3"),
                ChannelSetting(company_id=1, channel_id=1, key="provider", value="meta"),
                ChannelSetting(company_id=1, channel_id=1, key="phone_number_id", value="pn-123"),
                ChannelSetting(company_id=1, channel_id=1, key="access_token", value="test-token"),
                ChannelSetting(company_id=1, channel_id=1, key="connection_status", value="connected"),
            ]
        )
        db.add(
            Conversation(
                id=1,
                company_id=1,
                channel_id=1,
                provider="meta",
                external_thread_id="+34600000000",
                whatsapp_bot_enabled=True,
            )
        )
        db.add(
            InboundMessage(
                id=1,
                company_id=1,
                channel_id=1,
                provider="meta",
                conversation_id=1,
                source_external_id="wa-trigger-1",
                direction="inbound",
                sender="+34600000000",
                original_content="Ponme cuatro de tomate",
                content_type="text",
            )
        )
        db.commit()
        db.close()

    def test_record_automatic_response_does_not_take_human_ownership(self):
        db = self.Session()

        message = record_automatic_response(
            db,
            company_id=1,
            conversation_id=1,
            body="¿Son cuatro cajas?",
            external_id="wamid-auto-1",
            trigger_message_id=1,
            semantic_state="needs_clarification",
            prompt_execution_id=99,
            assistant_session_id=44,
            draft_revision=7,
        )

        metadata = json.loads(message.raw_payload_json)
        conversation = db.get(Conversation, 1)

        self.assertTrue(metadata["auto_response"])
        self.assertEqual(metadata["trigger_message_id"], 1)
        self.assertEqual(metadata["semantic_state"], "needs_clarification")
        self.assertEqual(metadata["assistant_session_id"], 44)
        self.assertEqual(metadata["draft_revision"], 7)
        self.assertEqual(message.direction, "outbound")
        self.assertEqual(message.processing_step, "outbound_auto_accepted")
        self.assertNotEqual(conversation.status, "human_owned")
        db.close()

    def test_same_trigger_is_not_answered_twice(self):
        db = self.Session()

        record_automatic_response(
            db,
            company_id=1,
            conversation_id=1,
            body="¿Son cuatro cajas?",
            external_id="wamid-auto-2",
            trigger_message_id=1,
            semantic_state="needs_clarification",
        )

        status = automatic_response_status(
            db,
            company_id=1,
            conversation_id=1,
            trigger_message_id=1,
        )

        self.assertFalse(status["allowed"])
        self.assertEqual(status["reason"], "already_replied")
        db.close()

    def test_auto_message_limit_blocks_fourth_response(self):
        db = self.Session()

        for index in range(3):
            record_automatic_response(
                db,
                company_id=1,
                conversation_id=1,
                body=f"Respuesta automática {index + 1}",
                external_id=f"wamid-limit-{index + 1}",
                trigger_message_id=100 + index,
                semantic_state="needs_clarification",
            )

        status = automatic_response_status(
            db,
            company_id=1,
            conversation_id=1,
            trigger_message_id=1,
        )

        self.assertFalse(status["allowed"])
        self.assertEqual(status["reason"], "auto_message_limit")
        self.assertEqual(status["count"], 3)
        self.assertEqual(status["limit"], 3)
        db.close()


    def test_auto_message_limit_blocks_fourth_response(self):
        db = self.Session()

        for index in range(3):
            record_automatic_response(
                db,
                company_id=1,
                conversation_id=1,
                body=f"Respuesta automática {index + 1}",
                external_id=f"wamid-limit-{index + 1}",
                trigger_message_id=100 + index,
                semantic_state="needs_clarification",
            )

        status = automatic_response_status(
            db,
            company_id=1,
            conversation_id=1,
            trigger_message_id=1,
        )

        self.assertFalse(status["allowed"])
        self.assertEqual(status["reason"], "auto_message_limit")
        self.assertEqual(status["count"], 3)
        self.assertEqual(status["limit"], 3)
        db.close()

    def test_stale_trigger_is_not_eligible_for_auto_response(self):
        db = self.Session()
        db.add(
            InboundMessage(
                id=2,
                company_id=1,
                channel_id=1,
                provider="meta",
                conversation_id=1,
                source_external_id="wa-trigger-2",
                direction="inbound",
                sender="+34600000000",
                original_content="Y también una de queso",
                content_type="text",
                received_at=datetime.now(timezone.utc) + timedelta(minutes=1),
            )
        )
        db.commit()

        status = automatic_response_status(
            db,
            company_id=1,
            conversation_id=1,
            trigger_message_id=1,
        )

        self.assertFalse(status["allowed"])
        self.assertEqual(status["reason"], "stale_trigger")
        db.close()

    def test_human_owned_conversation_is_not_eligible_for_auto_response(self):
        db = self.Session()
        db.get(Conversation, 1).status = "human_owned"
        db.commit()

        status = automatic_response_status(
            db,
            company_id=1,
            conversation_id=1,
            trigger_message_id=1,
        )

        self.assertFalse(status["allowed"])
        self.assertEqual(status["reason"], "human_owned")
        db.close()

    def test_disabled_chat_bot_is_not_eligible_for_auto_response(self):
        db = self.Session()
        db.get(Conversation, 1).whatsapp_bot_enabled = False
        db.commit()

        status = automatic_response_status(
            db,
            company_id=1,
            conversation_id=1,
            trigger_message_id=1,
        )

        self.assertFalse(status["allowed"])
        self.assertEqual(status["reason"], "bot_disabled")
        db.close()

    def test_human_active_order_session_is_not_eligible_for_auto_response(self):
        db = self.Session()
        db.add(
            WhatsAppOrderSession(
                company_id=1,
                conversation_id=1,
                active_key="conversation:1",
                due_at=datetime.now(timezone.utc) + timedelta(hours=12),
                human_active=True,
            )
        )
        db.commit()

        status = automatic_response_status(
            db,
            company_id=1,
            conversation_id=1,
            trigger_message_id=1,
        )

        self.assertFalse(status["allowed"])
        self.assertEqual(status["reason"], "human_active")
        db.close()

    def test_conversation_lookup_is_scoped_to_tenant(self):
        db = self.Session()

        status = automatic_response_status(
            db,
            company_id=1,
            conversation_id=999,
            trigger_message_id=1,
        )

        self.assertFalse(status["allowed"])
        self.assertEqual(status["reason"], "conversation_not_found")
        db.close()

    async def test_reservation_rechecks_human_takeover_after_initial_status(self):
        db = self.Session()
        original_status = automatic_response_status

        def status_then_take_over(session_db, **kwargs):
            status = original_status(session_db, **kwargs)
            conversation = session_db.get(Conversation, 1)
            conversation.status = "human_owned"
            session_db.commit()
            return status

        provider = AsyncMock()
        with (
            patch(
                "app.whatsapp.service.automatic_response_status",
                side_effect=status_then_take_over,
            ),
            patch("app.whatsapp.service._prepare_whatsapp_text_send"),
            patch("app.whatsapp.service.send_whatsapp_text", new=provider),
        ):
            result = await send_automatic_response(
                db,
                company_id=1,
                conversation_id=1,
                trigger_message_id=1,
                body="Respuesta de prueba",
                semantic_state="needs_clarification",
                assistant_session_id=44,
                draft_revision=7,
            )

        self.assertFalse(result["sent"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "human_owned")
        provider.assert_not_awaited()
        self.assertEqual(
            db.query(InboundMessage).filter(InboundMessage.direction == "outbound").count(),
            0,
        )
        db.close()


    async def test_send_persists_only_after_provider_accepts(self):
        db = self.Session()

        with patch(
            "app.whatsapp.service.send_whatsapp_text",
            new=AsyncMock(
                return_value={
                    "provider_message_id": "wamid-auto-3",
                    "recipient": "+34600000000",
                }
            ),
        ):
            result = await send_automatic_response(
                db,
                company_id=1,
                conversation_id=1,
                trigger_message_id=1,
                body="¿Son cuatro cajas?",
                semantic_state="needs_clarification",
                prompt_execution_id=100,
                assistant_session_id=44,
                draft_revision=7,
            )

        self.assertTrue(result["sent"])

        outbound = db.query(InboundMessage).filter(
            InboundMessage.source_external_id == "wamid-auto-3"
        ).one_or_none()
        self.assertIsNotNone(outbound)
        metadata = json.loads(outbound.raw_payload_json)
        self.assertEqual(metadata["assistant_session_id"], 44)
        self.assertEqual(metadata["draft_revision"], 7)
        db.close()

    async def test_takeover_after_reservation_cancels_automatic_send(self):
        db = self.Session()
        guard_results = [(None, None), (None, None), ("human_active", None)]
        provider = AsyncMock()

        with patch(
            "app.whatsapp.service._automatic_response_guard",
            side_effect=guard_results,
        ), patch(
            "app.whatsapp.service.send_whatsapp_text",
            new=provider,
        ):
            result = await send_automatic_response(
                db,
                company_id=1,
                conversation_id=1,
                trigger_message_id=1,
                body="¿Son cuatro cajas?",
                semantic_state="needs_clarification",
            )

        outbound = db.query(InboundMessage).filter(InboundMessage.direction == "outbound").one()
        self.assertFalse(result["sent"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "human_active")
        self.assertEqual(outbound.status, "cancelled")
        self.assertEqual(outbound.processing_step, "outbound_auto_cancelled")
        provider.assert_not_awaited()
        db.close()

    async def test_chat_bot_toggle_off_after_reservation_cancels_automatic_send(self):
        db = self.Session()
        guard_results = [(None, None), (None, None), ("bot_disabled", None)]
        provider = AsyncMock()

        with patch(
            "app.whatsapp.service._automatic_response_guard",
            side_effect=guard_results,
        ), patch(
            "app.whatsapp.service.send_whatsapp_text",
            new=provider,
        ):
            result = await send_automatic_response(
                db,
                company_id=1,
                conversation_id=1,
                trigger_message_id=1,
                body="¿Son cuatro cajas?",
                semantic_state="needs_clarification",
            )

        outbound = db.query(InboundMessage).filter(InboundMessage.direction == "outbound").one()
        self.assertFalse(result["sent"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "bot_disabled")
        self.assertEqual(outbound.status, "cancelled")
        self.assertEqual(outbound.processing_step, "outbound_auto_cancelled")
        provider.assert_not_awaited()
        db.close()

    async def test_provider_failure_records_unknown_outbound_without_retrying(self):
        db = self.Session()
        provider_calls = 0

        async def provider_failure(*args, **kwargs):
            nonlocal provider_calls
            provider_calls += 1
            raise RuntimeError("Meta unavailable")

        with patch(
            "app.whatsapp.service.send_whatsapp_text",
            new=AsyncMock(side_effect=provider_failure),
        ):
            with self.assertRaises(RuntimeError):
                await send_automatic_response(
                    db,
                    company_id=1,
                    conversation_id=1,
                    trigger_message_id=1,
                    body="¿Son cuatro cajas?",
                    semantic_state="needs_clarification",
                )
            retry = await send_automatic_response(
                db,
                company_id=1,
                conversation_id=1,
                trigger_message_id=1,
                body="¿Son cuatro cajas?",
                semantic_state="needs_clarification",
            )

        outbound = db.query(InboundMessage).filter(
            InboundMessage.direction == "outbound"
        ).all()
        self.assertEqual(len(outbound), 1)
        self.assertEqual(outbound[0].status, "send_unknown")
        self.assertEqual(outbound[0].processing_step, "outbound_send_unknown")
        self.assertFalse(retry["sent"])
        self.assertTrue(retry["skipped"])
        self.assertEqual(provider_calls, 1)
        db.close()


if __name__ == "__main__":
    unittest.main()
