from __future__ import annotations

import os
import unittest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")

from app.core.app_factory import create_app  # noqa: E402
from app.whatsapp.routes import _webhook_event_metadata, _webhook_payload_summary  # noqa: E402


class WhatsAppWebhookObservabilityTests(unittest.TestCase):
    def test_payload_summary_contains_diagnostics_without_event_payload(self):
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "waba-internal-test",
                    "changes": [{"field": "messages", "value": {"messages": [{"text": {"body": "privado"}}]}}],
                }
            ],
        }
        summary = _webhook_payload_summary(
            payload,
            [{"kind": "message", "external_id": "wamid.internal-test"}],
            body_size=128,
            signature_present=True,
        )

        self.assertEqual(summary["payload_object"], "whatsapp_business_account")
        self.assertEqual(summary["entry_count"], 1)
        self.assertEqual(summary["change_count"], 1)
        self.assertEqual(summary["webhook_fields"], ["messages"])
        self.assertEqual(summary["event_kinds"], {"message": 1})
        self.assertTrue(summary["signature_present"])
        self.assertNotIn("privado", summary)
        self.assertNotIn("wamid.internal-test", summary)

    def test_event_metadata_exposes_routing_signals_without_identifiers(self):
        metadata = _webhook_event_metadata(
            {
                "kind": "message",
                "business_account_id": "waba-internal-test",
                "phone_number_id": "phone-internal-test",
                "metadata": {"webhook_field": "messages"},
                "text": "privado",
            }
        )

        self.assertEqual(metadata["event_kind"], "message")
        self.assertEqual(metadata["webhook_field"], "messages")
        self.assertTrue(metadata["business_account_id_present"])
        self.assertTrue(metadata["phone_number_id_present"])
        self.assertNotIn("waba-internal-test", metadata)
        self.assertNotIn("phone-internal-test", metadata)
        self.assertNotIn("privado", metadata)

    def test_webhook_routes_accept_both_callback_slash_variants_without_redirect(self):
        routes = {
            (route.path, tuple(sorted(route.methods or [])))
            for included_router in create_app().routes
            for route in getattr(getattr(included_router, "original_router", included_router), "routes", [])
            if hasattr(route, "path") and hasattr(route, "methods")
        }

        for path in (
            "/webhooks/whatsapp",
            "/webhooks/whatsapp/",
            "/webhooks/whatsapp/{company_slug}",
            "/webhooks/whatsapp/{company_slug}/",
        ):
            self.assertIn((path, ("GET",)), routes)
            self.assertIn((path, ("POST",)), routes)


if __name__ == "__main__":
    unittest.main()
