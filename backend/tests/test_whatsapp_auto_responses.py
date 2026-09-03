from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import ChannelSetting, Company, Conversation, InboundMessage, InputChannel
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
        )

        metadata = json.loads(message.raw_payload_json)
        conversation = db.get(Conversation, 1)

        self.assertTrue(metadata["auto_response"])
        self.assertEqual(metadata["trigger_message_id"], 1)
        self.assertEqual(metadata["semantic_state"], "needs_clarification")
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
            trigger_message_id=200,
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
            trigger_message_id=200,
        )

        self.assertFalse(status["allowed"])
        self.assertEqual(status["reason"], "auto_message_limit")
        self.assertEqual(status["count"], 3)
        self.assertEqual(status["limit"], 3)
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
            )

        self.assertTrue(result["sent"])

        outbound = db.query(InboundMessage).filter(
            InboundMessage.source_external_id == "wamid-auto-3"
        ).one_or_none()
        self.assertIsNotNone(outbound)
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
