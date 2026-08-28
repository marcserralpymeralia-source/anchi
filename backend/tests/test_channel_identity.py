import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.channel_identity import inbound_channel_key, order_channel_key


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

    def test_unknown_provider_remains_an_unidentified_entry(self):
        message = SimpleNamespace(provider="some_provider", raw_payload_json=None)
        self.assertEqual(inbound_channel_key(message), "inbound")


if __name__ == "__main__":
    unittest.main()
