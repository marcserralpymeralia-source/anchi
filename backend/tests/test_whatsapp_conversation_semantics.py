from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.prompt_runtime import validate_prompt_output
from app.db.database import Base
from app.db.models import Company
from app.whatsapp.conversation_semantics import (
    evaluate_whatsapp_conversation_semantics,
)


class WhatsAppConversationSemanticValidationTests(unittest.TestCase):
    def test_ready_for_confirmation_is_valid(self):
        content = """
        {
          "intent": "order",
          "state": "ready_for_confirmation",
          "missing_or_uncertain": [],
          "reply_needed": true,
          "suggested_reply": "Te confirmo el pedido antes de tramitarlo.",
          "confidence": 0.96
        }
        """

        result = validate_prompt_output("whatsapp_conversation", content)

        self.assertTrue(result.ok)
        self.assertEqual(
            result.data["state"],
            "ready_for_confirmation",
        )

    def test_clarification_requires_reply(self):
        content = """
        {
          "intent": "order",
          "state": "needs_clarification",
          "missing_or_uncertain": ["cantidad de tomates"],
          "reply_needed": false,
          "suggested_reply": "",
          "confidence": 0.84
        }
        """

        result = validate_prompt_output("whatsapp_conversation", content)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "schema_error")

    def test_invalid_state_is_rejected(self):
        content = """
        {
          "intent": "order",
          "state": "confirmed",
          "missing_or_uncertain": [],
          "reply_needed": true,
          "suggested_reply": "Pedido confirmado.",
          "confidence": 0.9
        }
        """

        result = validate_prompt_output("whatsapp_conversation", content)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "unsupported_value")


class WhatsAppConversationSemanticServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        db = self.Session()
        db.add(Company(id=1, name="Test"))
        db.commit()
        db.close()

    def test_service_returns_validated_semantic_result(self):
        db = self.Session()

        fake_result = {
            "ok": True,
            "validation_ok": True,
            "validated_content": {
                "intent": "order",
                "state": "needs_clarification",
                "missing_or_uncertain": ["unidad de tomate"],
                "reply_needed": True,
                "suggested_reply": "¿Las 4 de tomate son cajas o unidades?",
                "confidence": 0.91,
            },
            "prompt_execution_id": 123,
        }

        with patch(
            "app.whatsapp.conversation_semantics.run_prompt_execution",
            return_value=fake_result,
        ) as mocked:
            result = evaluate_whatsapp_conversation_semantics(
                db,
                company_id=1,
                transcript="CLIENTE: Ponme 4 de tomate",
                input_reference="conversation:1",
            )

        self.assertEqual(result.intent, "order")
        self.assertEqual(result.state, "needs_clarification")
        self.assertEqual(
            result.missing_or_uncertain,
            ["unidad de tomate"],
        )
        self.assertTrue(result.reply_needed)
        self.assertEqual(result.prompt_execution_id, 123)

        mocked.assert_called_once()
        args, kwargs = mocked.call_args
        self.assertEqual(args[2], "whatsapp_conversation")
        self.assertEqual(
            args[4],
            "CLIENTE: Ponme 4 de tomate",
        )
        self.assertEqual(
            kwargs["input_reference"],
            "conversation:1",
        )

        db.close()

    def test_service_rejects_invalid_provider_output(self):
        db = self.Session()

        with patch(
            "app.whatsapp.conversation_semantics.run_prompt_execution",
            return_value={
                "ok": True,
                "validation_ok": False,
                "validation_errors": ["Estado inválido"],
            },
        ):
            with self.assertRaises(RuntimeError):
                evaluate_whatsapp_conversation_semantics(
                    db,
                    company_id=1,
                    transcript="CLIENTE: Quiero tomate",
                )

        db.close()


if __name__ == "__main__":
    unittest.main()
