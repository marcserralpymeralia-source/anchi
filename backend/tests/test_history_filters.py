from __future__ import annotations

import re
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import Email, EmailSettings, Order, utcnow
from scripts.performance_data import build_performance_fixture, performance_test_client


class HistoryFiltersTests(unittest.TestCase):
    @staticmethod
    def _pagination_and_row_count(html: str) -> tuple[int, int]:
        pagination = re.search(r"Bandeja de mensajes \(\d+-\d+ de (\d+)\)", html)
        rows = len(re.findall(r'<article\s+class="webmail-item\b', html))
        if pagination is None:
            raise AssertionError("No se encontró la paginación del buzón")
        return int(pagination.group(1)), rows

    def test_each_history_state_renders_the_rows_reported_by_its_counter(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                for state in ("all", "current", "review", "ready", "confirmed", "sent", "blocked"):
                    response = client.get(f"/history?state={state}&kind=all&date_range=90d")

                    self.assertEqual(response.status_code, 200)
                    total, rows = self._pagination_and_row_count(response.text)
                    self.assertEqual(rows, total, state)

                current_response = client.get("/history?state=current&kind=all&date_range=90d")
                current_total, _ = self._pagination_and_row_count(current_response.text)
                self.assertGreater(current_total, 0)
        finally:
            fixture.cleanup()

    def test_history_shows_mail_account_and_sync_action_when_imap_is_ready(self):
        fixture = build_performance_fixture("small")
        engine = create_engine(
            fixture.tenant_database_url,
            connect_args={"check_same_thread": False},
        )
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        try:
            with SessionLocal() as db:
                settings = db.scalar(select(EmailSettings).where(EmailSettings.company_id == fixture.company_id))
                if settings is None:
                    settings = EmailSettings(company_id=fixture.company_id)
                    db.add(settings)
                settings.connected_email = "buzon@example.com"
                settings.imap_host = "imap.example.com"
                settings.imap_username = "buzon@example.com"
                settings.imap_password_encrypted = "test-password-encrypted"
                db.commit()

            with performance_test_client(fixture) as client:
                response = client.get("/history")

            self.assertEqual(response.status_code, 200)
            self.assertIn('<title>Buzón de correo</title>', response.text)
            self.assertIn("Cuenta:", response.text)
            self.assertIn("buzon@example.com", response.text)
            self.assertIn('action="/settings/email/read"', response.text)
            self.assertIn("Sincronizar ahora", response.text)
        finally:
            engine.dispose()
            fixture.cleanup()

    def test_history_deduplicates_linked_emails_and_maps_email_status_aliases(self):
        fixture = build_performance_fixture("small")
        engine = create_engine(
            fixture.tenant_database_url,
            connect_args={"check_same_thread": False},
        )
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        now = utcnow()
        try:
            with SessionLocal() as db:
                db.add_all(
                    [
                        Email(
                            company_id=fixture.company_id,
                            external_id="history-filter-current",
                            sender="current@example.com",
                            subject="Correo actual independiente",
                            body="Pendiente de procesar",
                            status="pending",
                            agent_status="not_processed",
                            received_at=now,
                        ),
                        Email(
                            company_id=fixture.company_id,
                            external_id="history-filter-review",
                            sender="review@example.com",
                            subject="Correo dudoso independiente",
                            body="Requiere revisión",
                            status="dudoso",
                            agent_status="processed_doubtful",
                            received_at=now,
                        ),
                        Email(
                            company_id=fixture.company_id,
                            external_id="history-filter-ready",
                            sender="ready@example.com",
                            subject="Pedido procesado independiente",
                            body="Pedido detectado",
                            status="pedido_detectado",
                            agent_status="processed_order_detected",
                            received_at=now,
                        ),
                    ]
                )
                db.commit()

                orders = db.scalars(select(Order).where(Order.company_id == fixture.company_id)).all()
                linked_email_ids = {order.email_id for order in orders if order.email_id}
                email_ids = db.scalars(select(Email.id).where(Email.company_id == fixture.company_id)).all()
                expected_unique_all = len(orders) + sum(email_id not in linked_email_ids for email_id in email_ids)

            with performance_test_client(fixture) as client:
                all_response = client.get("/history?state=all&kind=all&date_range=90d")
                all_total, all_rows = self._pagination_and_row_count(all_response.text)
                self.assertEqual(all_total, expected_unique_all)
                self.assertEqual(all_rows, expected_unique_all)

                for state, subject in (
                    ("current", "Correo actual independiente"),
                    ("review", "Correo dudoso independiente"),
                    ("ready", "Pedido procesado independiente"),
                ):
                    response = client.get(f"/history?state={state}&kind=all&date_range=90d")
                    total, rows = self._pagination_and_row_count(response.text)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(subject, response.text)
                    self.assertEqual(rows, total, state)
        finally:
            engine.dispose()
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
