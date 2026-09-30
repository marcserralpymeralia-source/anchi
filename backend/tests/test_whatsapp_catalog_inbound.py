from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.connectors.contracts import ConnectorRegistry  # noqa: E402
from app.connectors.fake import FakeERPConnector  # noqa: E402
from app.connectors.identity import bind_external_identity  # noqa: E402
from app.connectors.whatsapp_catalog import WhatsAppCatalogInboundConnector  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import (  # noqa: E402
    BackgroundJob,
    ChannelSetting,
    Company,
    Customer,
    ExternalIdentity,
    InboundMessage,
    InputChannel,
    Order,
    OrderDelivery,
    OrderLine,
    OrderRoute,
    Product,
)
from app.jobs.service import claim_next_job, finish_job  # noqa: E402
from app.whatsapp.catalog import (  # noqa: E402
    bind_catalog_product_identity,
    bind_whatsapp_customer_identity,
    process_whatsapp_catalog_message,
)
from app.whatsapp.service import (  # noqa: E402
    WhatsAppTenantConfig,
    enqueue_whatsapp_processing,
    parse_payload_events,
    persist_event,
    whatsapp_catalog_event_matches_config,
)
from app.workers.jobs_worker import _process_job  # noqa: E402


class WhatsAppCatalogInboundTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            f"sqlite:///{(Path(self.tempdir.name) / 'tenant.sqlite').as_posix()}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        self._seed()

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def _seed(self):
        db = self.Session()
        company = Company(id=1, name="Catalog tenant")
        customer = Customer(
            id=1,
            company_id=1,
            code="C001",
            fiscal_name="Cliente Catalog",
            phone="+34600000000",
            status="active",
        )
        product_one = Product(id=1, company_id=1, reference="P-100", name="Producto uno", status="active")
        product_two = Product(id=2, company_id=1, reference="P-200", name="Producto dos", status="active")
        channel = InputChannel(
            company_id=1,
            key="whatsapp",
            name="WhatsApp",
            channel_type="message",
            is_active=True,
            supports_text=True,
        )
        db.add_all([company, customer, product_one, product_two, channel])
        db.flush()
        db.add_all(
            [
                ChannelSetting(company_id=1, channel_id=channel.id, key="catalog_enabled", value="true", value_type="string"),
                ChannelSetting(company_id=1, channel_id=channel.id, key="catalog_orders_enabled", value="true", value_type="string"),
                ChannelSetting(company_id=1, channel_id=channel.id, key="catalog_id", value="catalog-001", value_type="string"),
            ]
        )
        bind_whatsapp_customer_identity(db, company_id=1, customer_id=1, wa_id="+34600000000")
        bind_catalog_product_identity(db, company_id=1, product_id=1, product_retailer_id="ret-100", catalog_id="catalog-001")
        bind_catalog_product_identity(db, company_id=1, product_id=2, product_retailer_id="ret-200", catalog_id="catalog-001")
        db.add(OrderRoute(company_id=1, order_type="order", destination_connector_key="fake_erp"))
        db.commit()
        db.close()

    @staticmethod
    def _config(catalog_id: str = "catalog-001") -> WhatsAppTenantConfig:
        return WhatsAppTenantConfig(
            catalog_enabled=True,
            catalog_orders_enabled=True,
            catalog_id=catalog_id,
        )

    @staticmethod
    def _payload(*messages: dict) -> dict:
        return {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "ba-001",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": "phone-001", "display_phone_number": "34611111111"},
                                "contacts": [{"wa_id": "34600000000"}],
                                "messages": list(messages),
                            },
                        }
                    ],
                }
            ],
        }

    @staticmethod
    def _order_message(
        message_id: str = "wamid.catalog.001",
        *,
        items: list[dict] | None = None,
        catalog_id: str = "catalog-001",
        sender: str = "34600000000",
        text: str = "Entregar el viernes",
    ) -> dict:
        return {
            "id": message_id,
            "from": sender,
            "timestamp": "1780000000",
            "type": "order",
            "order": {
                "catalog_id": catalog_id,
                "text": text,
                "product_items": items
                or [
                    {"product_retailer_id": "ret-100", "quantity": "2", "item_price": "12.50", "currency": "EUR"},
                ],
            },
        }

    def _persist_order(self, db, message_id: str = "wamid.catalog.001", **kwargs):
        event = parse_payload_events(self._payload(self._order_message(message_id, **kwargs)))[0]
        message = persist_event(db, 1, event)
        self.assertIsNotNone(message)
        return event, message

    def test_connector_maps_order_to_neutral_structured_event(self):
        payload = {"message": self._order_message(), "sender": "+34600000000"}
        event = WhatsAppCatalogInboundConnector().to_inbound_order_event(payload, company_id=1)
        self.assertEqual(event.source_mode.value, "structured")
        self.assertEqual(event.external_event_id, "wamid.catalog.001")
        self.assertEqual(event.external_system, "meta_whatsapp_catalog")
        self.assertEqual(event.customer_external_system, "meta_whatsapp")
        self.assertEqual(event.source_catalog_id, "catalog-001")
        self.assertEqual(event.items[0].source_product_id, "ret-100")
        self.assertEqual(event.items[0].source_product_system, "meta_whatsapp_catalog")
        self.assertEqual(event.items[0].quantity, 2)
        self.assertEqual(event.items[0].unit_price, 12.5)
        self.assertEqual(event.items[0].currency, "EUR")
        self.assertEqual(event.notes, "Entregar el viernes")

    def test_batch_preserves_text_and_catalog_order_as_separate_events(self):
        events = parse_payload_events(
            self._payload(
                self._order_message(),
                {"id": "wamid.text.001", "from": "34600000000", "type": "text", "text": {"body": "ret-100"}, "context": {"referred_product": {"catalog_id": "catalog-001", "product_retailer_id": "ret-100"}}},
            )
        )
        self.assertEqual([event["message_type"] for event in events], ["order", "text"])
        self.assertEqual(events[0]["catalog_id"], "catalog-001")
        self.assertEqual(events[1]["metadata"]["payload"]["context"]["referred_product"]["product_retailer_id"], "ret-100")
        self.assertNotEqual(events[1]["message_type"], "order")

    def test_valid_order_resolves_customer_and_products_without_agent(self):
        db = self.Session()
        _, message = self._persist_order(db)
        processing_job = enqueue_whatsapp_processing(db, 1, message.id)
        claimed = claim_next_job(db, owner="catalog-worker", company_id=1, job_types={"process_inbound_message"})
        self.assertEqual(claimed.id, processing_job.id)
        with patch("app.workers.jobs_worker.AgentProcessingService", side_effect=AssertionError("Catalog no debe usar agente")):
            result = _process_job(db, claimed)
        finish_job(db, claimed, result)
        order = db.get(Order, result["order_id"])
        self.assertEqual(order.status, "pedido_validado")
        self.assertEqual(order.source_catalog_id, "catalog-001")
        self.assertEqual(order.customer_identification_method, "external_identity")
        self.assertEqual(order.lines[0].validated_product_id, 1)
        self.assertEqual(order.lines[0].source_product_system, "meta_whatsapp_catalog")
        self.assertEqual(order.lines[0].unit_price_received, 12.5)
        self.assertEqual(order.lines[0].currency_received, "EUR")
        self.assertEqual(order.notes, "Entregar el viernes")
        self.assertEqual(db.scalar(select(func.count()).select_from(OrderDelivery)), 1)
        delivery_job = claim_next_job(db, owner="delivery-worker", company_id=1, job_types={"deliver_order"})
        self.assertIsNotNone(delivery_job)
        with patch("app.workers.jobs_worker.default_destination_registry", return_value=ConnectorRegistry([FakeERPConnector()])):
            delivery_result = _process_job(db, delivery_job)
        finish_job(db, delivery_job, delivery_result)
        self.assertEqual(order.export_status, "exported")
        self.assertEqual(order.destination_external_order_id, "FAKE-ORDER-000001")
        db.close()

    def test_phone_fallback_resolves_customer_when_wa_identity_is_missing(self):
        db = self.Session()
        identity = db.scalar(select(ExternalIdentity).where(ExternalIdentity.entity_type == "customer"))
        db.delete(identity)
        db.commit()
        _, message = self._persist_order(db, message_id="wamid.phone.001")
        result = process_whatsapp_catalog_message(db, message, config=self._config())
        order = db.get(Order, result["order_id"])
        self.assertEqual(order.customer_id, 1)
        self.assertEqual(order.customer_identification_method, "phone")
        db.close()

    def test_duplicate_wamid_creates_one_message_order_delivery_and_job(self):
        db = self.Session()
        event, first = self._persist_order(db)
        duplicate = persist_event(db, 1, event)
        self.assertEqual(first.id, duplicate.id)
        first_result = process_whatsapp_catalog_message(db, first, config=self._config())
        second_result = process_whatsapp_catalog_message(db, duplicate, config=self._config())
        self.assertEqual(first_result["order_id"], second_result["order_id"])
        self.assertEqual(db.scalar(select(func.count()).select_from(InboundMessage)), 1)
        self.assertEqual(db.scalar(select(func.count()).select_from(Order)), 1)
        self.assertEqual(db.scalar(select(func.count()).select_from(OrderDelivery)), 1)
        self.assertEqual(db.scalar(select(func.count()).select_from(BackgroundJob).where(BackgroundJob.job_type == "deliver_order")), 1)
        db.close()

    def test_multiple_items_and_source_prices_are_preserved(self):
        db = self.Session()
        _, message = self._persist_order(
            db,
            message_id="wamid.multi.001",
            items=[
                {"product_retailer_id": "ret-100", "quantity": "2", "item_price": "12.50", "currency": "EUR"},
                {"product_retailer_id": "ret-200", "quantity": "1.5", "item_price": "8", "currency": "EUR"},
            ],
        )
        result = process_whatsapp_catalog_message(db, message, config=self._config())
        order = db.get(Order, result["order_id"])
        self.assertEqual(len(order.lines), 2)
        self.assertEqual([(line.source_product_external_id, line.quantity) for line in order.lines], [("ret-100", 2), ("ret-200", 1.5)])
        self.assertEqual([line.unit_price_received for line in order.lines], [12.5, 8.0])
        db.close()

    def test_unknown_identity_is_reviewable_and_does_not_enqueue_delivery(self):
        db = self.Session()
        _, message = self._persist_order(
            db,
            message_id="wamid.unknown.001",
            items=[{"product_retailer_id": "missing", "quantity": "1", "item_price": "4", "currency": "EUR"}],
            sender="34999999999",
        )
        result = process_whatsapp_catalog_message(db, message, config=self._config())
        order = db.get(Order, result["order_id"])
        self.assertTrue(result["review_required"])
        self.assertEqual(order.status, "pending_review")
        self.assertIsNone(result["delivery_id"])
        self.assertEqual(db.scalar(select(func.count()).select_from(OrderDelivery)), 0)
        db.close()

    def test_invalid_quantity_is_controlled_review_without_order(self):
        db = self.Session()
        _, message = self._persist_order(
            db,
            message_id="wamid.invalid.001",
            items=[{"product_retailer_id": "ret-100", "quantity": "0", "item_price": "4", "currency": "EUR"}],
        )
        result = process_whatsapp_catalog_message(db, message, config=self._config())
        self.assertTrue(result["review_required"])
        self.assertIsNone(result.get("order_id"))
        self.assertEqual(db.scalar(select(func.count()).select_from(Order)), 0)
        self.assertEqual(message.processing_step, "catalog_order_review")
        db.close()

    def test_unauthorized_catalog_is_reviewable_and_tenant_config_is_exact(self):
        db = self.Session()
        event = parse_payload_events(self._payload(self._order_message(catalog_id="catalog-other")))[0]
        self.assertFalse(whatsapp_catalog_event_matches_config(event, self._config()))
        message = persist_event(db, 1, event)
        result = process_whatsapp_catalog_message(db, message, config=self._config())
        self.assertTrue(result["review_required"])
        self.assertEqual(db.scalar(select(func.count()).select_from(Order)), 0)
        db.close()

    def test_mixed_currency_is_rejected_deterministically(self):
        db = self.Session()
        _, message = self._persist_order(
            db,
            message_id="wamid.currency.001",
            items=[
                {"product_retailer_id": "ret-100", "quantity": "1", "item_price": "4", "currency": "EUR"},
                {"product_retailer_id": "ret-200", "quantity": "1", "item_price": "5", "currency": "USD"},
            ],
        )
        result = process_whatsapp_catalog_message(db, message, config=self._config())
        self.assertTrue(result["review_required"])
        self.assertEqual(db.scalar(select(func.count()).select_from(Order)), 0)
        db.close()


if __name__ == "__main__":
    unittest.main()
