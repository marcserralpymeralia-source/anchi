from __future__ import annotations

import unittest
import json
from unittest.mock import patch

from fastapi import FastAPI
from starlette.requests import Request
from sqlalchemy import func, select

from app.db.models import BackgroundJob, Conversation, Customer, InboundMessage, Order, Product
from app.demo_scenarios import load_demo_scenarios
from app.master.service import TenantRole, TenantUser
from app.orders.routes import list_orders
from app.whatsapp.inbox_routes import whatsapp_inbox
from tests.test_setup_onboarding import SetupFixture

TEST_DEMO_DATA = {
    "customers": {
    f"customer_{index}": f"demo-test-{index}"
    for index in range(1, 6)
    },
    "products": {
        "breast_5kg": "test-product-breast-small",
        "breast_10kg": "test-product-breast-large",
        "duck_burger_mini": "test-product-duck-mini",
        "thighs_5kg": "test-product-thigh-small",
        "thighs_10kg": "test-product-thigh-large",
        "stew_beef_a": "test-product-stew-a",
        "stew_beef_b": "test-product-stew-b",
    },
}


class DemoViewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = SetupFixture()
        self.user = TenantUser(
            id=1,
            email="admin@setup.local",
            name="Admin Setup",
            is_active=True,
            company_id=1,
            company_name="Setup Demo",
            company_slug="setup",
            role=TenantRole(name="Administrador"),
            membership_id=1,
        )
        self.app = FastAPI()
        self.request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/orders",
                "raw_path": b"/orders",
                "query_string": b"view=cards&demo=1",
                "headers": [],
                "server": ("testserver", 80),
                "client": ("testclient", 50000),
                "app": self.app,
            }
        )
        with self.fixture.TenantSession() as db:
            db.add_all(
                [
                    Customer(company_id=1, code=TEST_DEMO_DATA["customers"]["customer_1"], fiscal_name="Demo test customer 1"),
                    Customer(company_id=1, code=TEST_DEMO_DATA["customers"]["customer_2"], fiscal_name="Demo test customer 2"),
                    Customer(company_id=1, code=TEST_DEMO_DATA["customers"]["customer_3"], fiscal_name="Demo test customer 3"),
                    Customer(company_id=1, code=TEST_DEMO_DATA["customers"]["customer_4"], fiscal_name="Demo test customer 4"),
                    Customer(company_id=1, code=TEST_DEMO_DATA["customers"]["customer_5"], fiscal_name="Demo test customer 5"),
                    Product(company_id=1, reference=TEST_DEMO_DATA["products"]["breast_5kg"], name="Demo product breast small"),
                    Product(company_id=1, reference=TEST_DEMO_DATA["products"]["breast_10kg"], name="Demo product breast large"),
                    Product(company_id=1, reference=TEST_DEMO_DATA["products"]["duck_burger_mini"], name="Demo product duck mini"),
                    Product(company_id=1, reference=TEST_DEMO_DATA["products"]["thighs_5kg"], name="Demo product thigh small"),
                    Product(company_id=1, reference=TEST_DEMO_DATA["products"]["thighs_10kg"], name="Demo product thigh large"),
                    Product(company_id=1, reference=TEST_DEMO_DATA["products"]["stew_beef_a"], name="Demo product stew a"),
                    Product(company_id=1, reference=TEST_DEMO_DATA["products"]["stew_beef_b"], name="Demo product stew b"),
                ]
            )
            db.commit()

    def tearDown(self):
        self.fixture.cleanup()

    def test_scenarios_resolve_only_in_the_requested_tenant(self):
        with self.fixture.TenantSession() as db:
            scenarios = load_demo_scenarios(db, 1, demo_data=TEST_DEMO_DATA)
            other_tenant = load_demo_scenarios(db, 2, demo_data=TEST_DEMO_DATA)

        self.assertEqual(len(scenarios), 5)
        self.assertTrue(all(not item["customer_missing"] for item in scenarios))
        self.assertTrue(all(not line["product_missing"] for item in scenarios for line in item["lines"] if line.get("reference")))
        self.assertEqual([item["reference"] for item in scenarios[3]["lines"]], [TEST_DEMO_DATA["products"]["breast_5kg"]])
        self.assertEqual([item["reference"] for item in scenarios[2]["lines"]], [None])
        self.assertEqual([item["reference"] for item in scenarios[4]["lines"]], [None])
        self.assertEqual(
            [item["reference"] for item in other_tenant[0]["lines"]],
            [TEST_DEMO_DATA["products"]["breast_10kg"], TEST_DEMO_DATA["products"]["duck_burger_mini"]],
        )
        self.assertTrue(all(item["customer_missing"] for item in other_tenant))
        self.assertTrue(all(line["product_missing"] for item in other_tenant for line in item["lines"] if line.get("reference")))
        self.assertTrue(all(item["customer_name"] is None for item in other_tenant))

    def test_order_demo_renders_five_read_only_cards_and_final_correction_only(self):
        with self.fixture.TenantSession() as db:
            before = db.scalar(select(func.count(Order.id))) or 0
            with patch.dict("os.environ", {"ANCHI_DEMO_CONFIG": json.dumps(TEST_DEMO_DATA)}):
                response = list_orders(self.request, view="cards", demo=True, db=db, user=self.user)
            html = response.body.decode()
            after = db.scalar(select(func.count(Order.id))) or 0

        self.assertEqual(html.count('class="demo-order-card"'), 5)
        self.assertIn("Proposta de comanda · DEMO", html)
        self.assertIn("Demo test customer 5", html)
        self.assertIn(TEST_DEMO_DATA["products"]["stew_beef_a"], html)
        self.assertIn(TEST_DEMO_DATA["products"]["stew_beef_b"], html)
        tamoil = html.split("Demo test customer 4", 1)[1].split("</article>", 1)[0]
        self.assertIn(TEST_DEMO_DATA["products"]["breast_5kg"], tamoil)
        self.assertNotIn(TEST_DEMO_DATA["products"]["breast_10kg"], tamoil)
        self.assertNotIn("demo-test-", html)
        self.assertNotIn('href="/orders/', html)
        self.assertEqual(before, after)

    def test_whatsapp_demo_has_five_selectable_conversations_without_send_controls_or_sage_codes(self):
        import asyncio

        self.request.scope["path"] = "/whatsapp/inbox"
        self.request.scope["query_string"] = b"demo=1&case=0"
        with self.fixture.TenantSession() as db:
            before = (
                db.scalar(select(func.count(Conversation.id))) or 0,
                db.scalar(select(func.count(InboundMessage.id))) or 0,
                db.scalar(select(func.count(BackgroundJob.id))) or 0,
            )
            with patch.dict("os.environ", {"ANCHI_DEMO_CONFIG": json.dumps(TEST_DEMO_DATA)}):
                response = asyncio.run(whatsapp_inbox(self.request, demo=True, db=db, user=self.user))
            html = response.body.decode()
            after = (
                db.scalar(select(func.count(Conversation.id))) or 0,
                db.scalar(select(func.count(InboundMessage.id))) or 0,
                db.scalar(select(func.count(BackgroundJob.id))) or 0,
            )

        self.assertEqual(html.count('role="listitem"'), 5)
        self.assertIn("Demo test customer 1", html)
        self.assertIn("Bon dia! Em poses dues caixes", html)
        self.assertIn("Assistent (simulat)", html)
        self.assertIn("case=4", html)
        self.assertNotIn(TEST_DEMO_DATA["products"]["breast_10kg"], html)
        self.assertNotIn(TEST_DEMO_DATA["products"]["duck_burger_mini"], html)
        self.assertNotIn('action="/whatsapp/inbox/', html)
        self.assertNotIn("channel-chat-compose", html)
        self.assertEqual(before, after)

    def test_unresolved_customer_scenario_does_not_show_a_fallback_identity_or_messages(self):
        import asyncio

        self.request.scope["path"] = "/whatsapp/inbox"
        self.request.scope["query_string"] = b"demo=1&case=0"
        with self.fixture.TenantSession() as db, patch.dict("os.environ", {"ANCHI_DEMO_CONFIG": "{}"}):
            response = asyncio.run(whatsapp_inbox(self.request, demo=True, db=db, user=self.user))
            html = response.body.decode()

        self.assertIn("Client no disponible", html)
        self.assertIn("Escenari no disponible en aquest tenant", html)
        self.assertNotIn("Bon dia! Em poses dues caixes", html)
        self.assertNotIn("Demo test customer 1", html)

    def test_demo_routes_keep_the_existing_authentication_requirement(self):
        client, cleanup = self.fixture.client()
        try:
            orders_response = client.get("/orders?view=cards&demo=1", follow_redirects=False)
            whatsapp_response = client.get("/whatsapp/inbox?demo=1", follow_redirects=False)
        finally:
            cleanup()

        self.assertEqual(orders_response.status_code, 303)
        self.assertEqual(whatsapp_response.status_code, 303)
        self.assertIn("/login", orders_response.headers.get("location", ""))
        self.assertIn("/login", whatsapp_response.headers.get("location", ""))


if __name__ == "__main__":
    unittest.main()
