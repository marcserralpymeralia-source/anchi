import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.channel_identity import inbound_channel_key, order_channel_key
from app.orders.routes import _conversation_preview as order_conversation_preview
from app.workbench.routes import _conversation_preview as workbench_conversation_preview


class ChannelIdentityTests(unittest.TestCase):
    def test_email_order_wins_when_pipeline_also_created_a_conversation(self):
        order = SimpleNamespace(
            email_id=17,
            conversation=SimpleNamespace(
                provider="imap",
                messages=[SimpleNamespace(provider="imap", raw_payload_json=None)],
            ),
        )
        self.assertEqual(order_channel_key(order), "email")

    def test_whatsapp_order_is_detected_from_provider(self):
        order = SimpleNamespace(
            email_id=None,
            conversation=SimpleNamespace(
                provider="whatsapp",
                messages=[SimpleNamespace(provider="whatsapp", raw_payload_json=None)],
            ),
        )
        self.assertEqual(order_channel_key(order), "whatsapp")

    def test_manual_whatsapp_import_is_not_treated_as_generic_entry(self):
        message = SimpleNamespace(provider="manual_import", raw_payload_json=json.dumps({"import_type": "manual_whatsapp"}))
        self.assertEqual(inbound_channel_key(message), "whatsapp")

    def test_meta_provider_is_whatsapp_for_live_cloud_api_messages(self):
        message = SimpleNamespace(provider="meta", raw_payload_json=None)
        order = SimpleNamespace(
            email_id=None,
            conversation=SimpleNamespace(provider="meta", messages=[message]),
        )
        self.assertEqual(inbound_channel_key(message), "whatsapp")
        self.assertEqual(order_channel_key(order), "whatsapp")

    def test_meta_provider_is_labeled_whatsapp_in_both_conversation_previews(self):
        message = SimpleNamespace(
            provider="meta",
            raw_payload_json=None,
            received_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
            created_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
            sender="34600000000",
            direction="inbound",
            original_content="Necesito 10 unidades",
            normalized_text=None,
        )

        workbench_preview = workbench_conversation_preview([message])
        order_preview = order_conversation_preview(
            SimpleNamespace(
                email_id=None,
                conversation=SimpleNamespace(provider="meta", messages=[message]),
            )
        )

        self.assertEqual(workbench_preview["provider_label"], "WhatsApp")
        self.assertEqual(order_preview["provider_label"], "WhatsApp")

    def test_unknown_provider_remains_an_unidentified_entry(self):
        message = SimpleNamespace(provider="some_provider", raw_payload_json=None)
        self.assertEqual(inbound_channel_key(message), "inbound")


if __name__ == "__main__":
    unittest.main()
