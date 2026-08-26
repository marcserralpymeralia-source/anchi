from __future__ import annotations

import unittest

from scripts.simulate_whatsapp_demo import build_demo_payload
from app.whatsapp.service import parse_payload_events


class WhatsAppDemoSimulatorTests(unittest.TestCase):
    def test_demo_payload_covers_realistic_coexistence_events(self):
        payload = build_demo_payload(
            business_account_id="demo-waba",
            phone_number_id="demo-phone",
            run_id="test-run",
        )

        events = parse_payload_events(payload)

        self.assertEqual(
            [event["kind"] for event in events],
            [
                "message",
                "status",
                "message_echo",
                "history_sync",
                "history_message",
                "history_message",
                "contact_sync",
                "account_update",
            ],
        )
        self.assertEqual(events[0]["text_content"], "Hola Anchi, necesitamos 12 unidades del producto P-100.")
        self.assertEqual(events[2]["direction"], "outbound")
        self.assertEqual(events[3]["metadata"]["sync"]["progress"], 100)
        self.assertEqual(events[-1]["metadata"]["payload"]["event"], "ACCOUNT_RECONNECTED")


if __name__ == "__main__":
    unittest.main()
