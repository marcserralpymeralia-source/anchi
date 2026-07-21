from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.whatsapp.importer import parse_manual_whatsapp_text


class BrandingPayload(dict):
    def __getattr__(self, item):  # noqa: D401
        return self.get(item)


def build_request_state() -> SimpleNamespace:
    branding = BrandingPayload(
        theme={
            "colors": {
                "background": "#f5f7f6",
                "surface": "#ffffff",
                "text": "#1b1f22",
                "muted": "#5f6b73",
                "border": "#dde5e2",
            },
            "buttons": {"primary": "#157f6e", "primary_hover": "#0f6759", "primary_text": "#ffffff", "secondary": "#eaf0ee", "secondary_text": "#1b1f22", "danger": "#d61f2c", "radius": 8, "font_size": 14},
            "sidebar": {"background": "#123a32", "text": "#ffffff", "muted": "#d7e5df", "hover": "#1e4a40", "active_background": "#ffffff", "active_text": "#123a32", "width": 260},
            "cards": {"background": "#ffffff", "border": "#dde5e2", "radius": 14, "shadow": "0 8px 24px rgba(18,58,50,.08)", "padding": 16},
            "tables": {"header_background": "#eef3f1", "header_text": "#31443f", "row_odd": "#ffffff", "row_even": "#f8faf9", "row_hover": "#eef6ef", "border": "#dde5e2", "vertical_padding": 14},
            "scoring": {"safe": "#2e8b57", "reviewable": "#d6a700", "doubtful": "#e67e22", "not_importable": "#d61f2c", "without_score": "#8a969d"},
            "login": {"background": "#f5f7f6", "card": "#ffffff", "button": "#157f6e"},
            "typography": {"font_family": "Inter, ui-sans-serif, system-ui, sans-serif"},
            "status_badges": {"pending_review_bg": "#eaf0ee", "pending_review_text": "#1b1f22", "confirmed_bg": "#eef6ef", "confirmed_text": "#2e8b57", "exported_bg": "#dff3e5", "exported_text": "#1f6b43", "error_bg": "#fdecec", "error_text": "#d61f2c", "no_order_bg": "#eceff1", "no_order_text": "#5f6b73", "doubtful_bg": "#fff3e0", "doubtful_text": "#e67e22", "discarded_bg": "#f2f2f2", "discarded_text": "#5f6b73"},
        },
        favicon_url=None,
        logo_url=None,
        dark_logo_url=None,
        show_logo_sidebar=True,
        show_app_name_sidebar=True,
        show_claim_sidebar=True,
        app_name="Anchi",
        company_name="Demo",
        secondary_claim="Gestion inteligente de pedidos",
    )
    alert_center = SimpleNamespace(total=0, critical=0, high=0, medium=0, low=0, has_critical=False, recent=[])
    return SimpleNamespace(branding=branding, alert_center=alert_center)


class ManualWhatsAppImportTests(unittest.TestCase):
    def test_parser_extracts_chat_and_stable_hash(self):
        raw_text = "\n".join(
            [
                "[16/07/26, 09:30] Cliente Demo: Buenos dias, adjunto pedido PDF",
                "Seguimos con 5 cajas de P-100",
                "[16/07/26, 09:31] Empresa Demo: Recibido, lo revisamos",
            ]
        )
        parsed = parse_manual_whatsapp_text(raw_text, client_participant="Cliente Demo", company_participant="Empresa Demo")

        self.assertEqual(parsed["participants"]["client"], "Cliente Demo")
        self.assertEqual(parsed["participants"]["company"], "Empresa Demo")
        self.assertEqual(len(parsed["messages"]), 2)
        self.assertEqual(parsed["messages"][0]["direction"], "inbound")
        self.assertEqual(parsed["messages"][0]["attachments_referenced"], ["document"])
        self.assertEqual(parsed["messages"][1]["direction"], "outbound")
        self.assertTrue(parsed["normalized_text"])
        self.assertTrue(parsed["dedupe_hash"])
        self.assertTrue(parsed["thread_key"])

        duplicate = parse_manual_whatsapp_text(raw_text, client_participant="Cliente Demo", company_participant="Empresa Demo")
        self.assertEqual(parsed["dedupe_hash"], duplicate["dedupe_hash"])
        self.assertEqual(parsed["thread_key"], duplicate["thread_key"])


if __name__ == "__main__":
    unittest.main()
