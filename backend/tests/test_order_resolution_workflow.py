from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import os
import sys

os.environ.setdefault("APP_ENV", "development")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.encryption import encrypt_secret  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.templating import templates  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import BackgroundJob, Company, Email, InboundMessage, LLMSettings, Order, ScoringSettings  # noqa: E402
from app.channels.routes import process_channel_entry, resolve_channel_entry  # noqa: E402
from app.imports.routes import whatsapp_import_confirm, whatsapp_import_process  # noqa: E402
from app.messages.service import upsert_inbound_message  # noqa: E402
from app.tenancy.migrations import upgrade_tenant_schema  # noqa: E402
from app.whatsapp.importer import parse_manual_whatsapp_text  # noqa: E402


class BrandingPayload(dict):
    def __getattr__(self, item):  # noqa: D401
        return self.get(item)


class FakeRequest:
    def __init__(self, accept: str = "text/html"):
        self.headers = {"accept": accept}
        self.url = SimpleNamespace(path="/demo")
        self.cookies = {}
        self.scope = {"session": {}}
        self.state = SimpleNamespace(
            branding=BrandingPayload(
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
            ),
            alert_center=SimpleNamespace(total=0, critical=0, high=0, medium=0, low=0, has_critical=False, recent=[]),
        )


def _demo_user() -> SimpleNamespace:
    return SimpleNamespace(id=1, company_id=1, name="Administrador demo", role=SimpleNamespace(name="Administrador"))


class OrderResolutionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        self.tenant_path = base / "tenant.sqlite"
        self.engine = create_engine(f"sqlite:///{self.tenant_path.as_posix()}", connect_args={"check_same_thread": False}, poolclass=NullPool)
        Base.metadata.create_all(self.engine)
        templates.env.globals["branding_css_vars"] = lambda payload: ""
        templates.env.globals["app_settings"] = get_settings()
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        self._seed_base_data()

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def _seed_base_data(self):
        db = self.Session()
        db.add(Company(id=1, name="Demo", active=True))
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
        upgrade_tenant_schema(self.engine, company_id=1, application_version="1.2.3")
        db.close()

    def test_manual_whatsapp_preview_process_and_confirm(self):
        raw_text = "\n".join(
            [
                "[16/07/26, 09:30] Cliente Demo: Buenos dias, necesitamos 5 cajas de P-100",
                "[16/07/26, 09:31] Empresa Demo: Perfecto, lo revisamos",
            ]
        )
        parsed = parse_manual_whatsapp_text(raw_text, client_participant="Cliente Demo", company_participant="Empresa Demo")
        self.assertEqual(parsed["participants"]["client"], "Cliente Demo")
        self.assertEqual(len(parsed["messages"]), 2)
        self.assertEqual(parsed["messages"][0]["direction"], "inbound")
        self.assertEqual(parsed["messages"][1]["direction"], "outbound")

        db = self.Session()
        with patch(
            "app.imports.routes.analysis_context",
            return_value={
                "customer": {"name": "Cliente Demo", "score": 94.0},
                "lines": [{"original_text": "5 cajas de P-100", "reference": "P-100", "matched_product": "P-100 · Producto Demo", "match_method": "reference", "quantity": 5, "unit": "cajas", "confidence": 95.0, "has_match": True}],
                "score": 92.0,
                "category_label": "Seguro",
                "status": "pedido_confirmado",
                "source_label": "Importación manual de WhatsApp",
                "expected": {"customer": "", "score": None, "status": ""},
                "comparison": {"customer_match": True, "score_delta": None, "status_match": True},
                "suggested_action": "Procesar",
            },
        ):
            response = asyncio.run(
                whatsapp_import_process(
                    FakeRequest(),
                    raw_text=raw_text,
                    client_participant="Cliente Demo",
                    company_participant="Empresa Demo",
                    subject="Pedido WhatsApp",
                    sender_hint="Cliente Demo",
                    expected_customer="Cliente Demo",
                    expected_score="92",
                    expected_status="pedido_confirmado",
                    db=db,
                    user=_demo_user(),
                )
            )

        self.assertEqual(response.template.name, "imports/whatsapp.html")
        self.assertEqual(response.context["result"]["score"], 92.0)

        confirm_response = whatsapp_import_confirm(
            FakeRequest(),
            raw_text=raw_text,
            client_participant="Cliente Demo",
            company_participant="Empresa Demo",
            subject="Pedido WhatsApp",
            sender_hint="Cliente Demo",
            expected_customer="Cliente Demo",
            expected_score="92",
            expected_status="pedido_confirmado",
            db=db,
            user=_demo_user(),
        )
        self.assertEqual(confirm_response.status_code, 303)
        self.assertEqual(confirm_response.headers["location"], "/entries?focus=inbound-1")
        self.assertEqual(db.scalar(select(func.count()).select_from(InboundMessage)) or 0, 1)
        self.assertEqual(db.scalar(select(func.count()).select_from(BackgroundJob)) or 0, 1)

        duplicate_response = whatsapp_import_confirm(
            FakeRequest(),
            raw_text=raw_text,
            client_participant="Cliente Demo",
            company_participant="Empresa Demo",
            subject="Pedido WhatsApp",
            sender_hint="Cliente Demo",
            expected_customer="Cliente Demo",
            expected_score="92",
            expected_status="pedido_confirmado",
            db=db,
            user=_demo_user(),
        )
        self.assertEqual(duplicate_response.status_code, 200)
        self.assertEqual(db.scalar(select(func.count()).select_from(InboundMessage)) or 0, 1)
        db.close()

    def test_unified_resolution_route_handles_email_and_inbound_sources(self):
        db = self.Session()
        email = Email(company_id=1, external_id="mail-1", sender="cliente@example.com", subject="Pedido demo", body="10 cajas")
        db.add(email)
        db.commit()

        email_process_response = process_channel_entry("email", email.id, FakeRequest(), db=db, user=SimpleNamespace(id=1, company_id=1))
        self.assertEqual(email_process_response.status_code, 303)
        self.assertEqual(email_process_response.headers["location"], f"/workbench/item/email/{email.id}/detail")
        self.assertEqual(db.scalar(select(func.count()).select_from(BackgroundJob)) or 0, 1)

        email_resolve_response = resolve_channel_entry("email", email.id, FakeRequest(), db=db, user=SimpleNamespace(id=1, company_id=1))
        self.assertEqual(email_resolve_response.status_code, 303)
        self.assertEqual(email_resolve_response.headers["location"], f"/workbench/item/email/{email.id}/detail")
        self.assertEqual(db.scalar(select(func.count()).select_from(BackgroundJob)) or 0, 1)

        raw_text = "[16/07/26, 09:30] Cliente Demo: Necesitamos 3 cajas"
        parsed = parse_manual_whatsapp_text(raw_text, client_participant="Cliente Demo", company_participant="Empresa Demo")
        inbound_message, _conversation = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="whatsapp",
            provider="manual_import",
            external_id=parsed["dedupe_hash"],
            sender="Cliente Demo",
            recipients=["Empresa Demo"],
            subject="WhatsApp manual",
            text_content=raw_text,
            external_thread_id=parsed["thread_key"],
            metadata={"import_type": "manual_whatsapp", "parsed": parsed},
            content_type="whatsapp_text",
        )
        db.commit()

        inbound_process_response = process_channel_entry("inbound", inbound_message.id, FakeRequest(), db=db, user=SimpleNamespace(id=1, company_id=1))
        self.assertEqual(inbound_process_response.status_code, 303)
        self.assertEqual(inbound_process_response.headers["location"], f"/?focus=inbound-{inbound_message.id}")
        self.assertEqual(db.scalar(select(func.count()).select_from(BackgroundJob)) or 0, 2)

        inbound_response = resolve_channel_entry("inbound", inbound_message.id, FakeRequest(), db=db, user=SimpleNamespace(id=1, company_id=1))
        self.assertEqual(inbound_response.status_code, 303)
        self.assertEqual(inbound_response.headers["location"], f"/?focus=inbound-{inbound_message.id}")
        self.assertEqual(db.scalar(select(func.count()).select_from(BackgroundJob)) or 0, 2)

        order = Order(company_id=1, conversation_id=inbound_message.conversation_id, status="pedido_confirmado", score=91, customer_detected_name="Cliente Demo")
        db.add(order)
        db.flush()
        inbound_message.order_id = order.id
        db.commit()

        processed_response = resolve_channel_entry("inbound", inbound_message.id, FakeRequest(), db=db, user=SimpleNamespace(id=1, company_id=1))
        self.assertEqual(processed_response.status_code, 303)
        self.assertEqual(processed_response.headers["location"], f"/orders/{order.id}")
        self.assertEqual(db.scalar(select(func.count()).select_from(BackgroundJob)) or 0, 2)
        db.close()


if __name__ == "__main__":
    unittest.main()
