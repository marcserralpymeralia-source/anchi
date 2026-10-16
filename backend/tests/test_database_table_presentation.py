from __future__ import annotations

import unittest

import pandas as pd
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.databases.service import _customer_row
from app.db.database import Base
from app.db.models import Company, Customer, CustomerContactPoint
from app.imports.service import confirm_import
from app.master_data.service import format_phone


class DatabaseTablePresentationTests(unittest.TestCase):
    def test_phone_format_preserves_leading_zeroes_and_international_prefixes(self):
        self.assertEqual(format_phone("901.001.870"), "901 001 870")
        self.assertEqual(format_phone("001.234.567"), "001 234 567")
        self.assertEqual(format_phone("+34 901.001.870"), "+34 901 001 870")
        self.assertEqual(format_phone("0034.901.001.870"), "0034 901 001 870")
        self.assertEqual(format_phone("+1 (202) 555-0148"), "+1 (202) 555-0148")
        self.assertEqual(format_phone("+90 1234567"), "+90 1234567")
        self.assertEqual(format_phone("900.12 ext. 4"), "900 12 ext 4")

    def test_customer_row_formats_primary_phone_and_deduplicates_active_associated_phones(self):
        customer = Customer(
            id=10,
            company_id=1,
            code="C-10",
            fiscal_name="Cliente de prueba",
            commercial_name="Cliente visible",
            primary_email="compras@example.test",
            phone="901.001.870",
        )
        customer.contact_points = [
            CustomerContactPoint(company_id=1, customer_id=10, type="phone", value="901001870", active=True, is_primary=True),
            CustomerContactPoint(company_id=1, customer_id=10, type="whatsapp", value="+34 901 001 870", active=True, is_primary=False),
            CustomerContactPoint(company_id=1, customer_id=10, type="phone", value="612345678", active=True, is_primary=False),
            CustomerContactPoint(company_id=1, customer_id=10, type="whatsapp", value="612.345.678", active=True, is_primary=False),
            CustomerContactPoint(company_id=1, customer_id=10, type="phone", value="+44 20 1234 5678", active=True, is_primary=False),
            CustomerContactPoint(company_id=1, customer_id=10, type="phone", value="699123456", active=False, is_primary=False),
            CustomerContactPoint(company_id=1, customer_id=10, type="email", value="ventas@example.test", active=True, is_primary=False),
        ]

        row = _customer_row(customer, orders_count=0, last_order_at=None, contact_count=0)

        self.assertEqual(row["phone"], "901 001 870")
        self.assertEqual(row["phone_href"], "tel:901001870")
        self.assertEqual(row["primary_email"], "compras@example.test")
        self.assertEqual(row["associated_phones"], "612 345 678, +44 20 1234 5678")
        self.assertEqual(len(row["associated_phone_items"]), 2)
        self.assertEqual(row["associated_phone_items"][1]["href"], "tel:+442012345678")

    def test_customer_import_saves_new_phone_in_display_format(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        db = Session()
        try:
            db.add(Company(id=1, name="Empresa de prueba"))
            db.commit()

            confirm_import(
                db,
                company_id=1,
                user=type("User", (), {"id": 1})(),
                entity_type="customers",
                filename="clientes.csv",
                df=pd.DataFrame([{
                    "code": "C-IMPORT",
                    "fiscal_name": "Cliente importado",
                    "phone": "901.001.870",
                }]),
                mapping={"code": "code", "fiscal_name": "fiscal_name", "phone": "phone"},
                mode="create_update",
            )

            customer = db.scalar(select(Customer).where(Customer.company_id == 1, Customer.code == "C-IMPORT"))
            self.assertIsNotNone(customer)
            self.assertEqual(customer.phone, "901 001 870")
        finally:
            db.close()
            engine.dispose()

    def test_customer_import_preserves_existing_associated_phones_when_column_is_omitted(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        db = Session()
        try:
            company = Company(id=1, name="Empresa de prueba")
            customer = Customer(
                company_id=1,
                code="C-EXISTING",
                fiscal_name="Cliente existente",
                phone="901 001 870",
            )
            db.add_all([company, customer])
            db.flush()
            db.add_all([
                CustomerContactPoint(
                    company_id=1,
                    customer_id=customer.id,
                    type="phone",
                    value="901001870",
                    active=True,
                    is_primary=True,
                ),
                CustomerContactPoint(
                    company_id=1,
                    customer_id=customer.id,
                    type="phone",
                    value="612345678",
                    active=True,
                    is_primary=False,
                ),
            ])
            db.commit()

            confirm_import(
                db,
                company_id=1,
                user=type("User", (), {"id": 1})(),
                entity_type="customers",
                filename="clientes.csv",
                df=pd.DataFrame([{
                    "code": "C-EXISTING",
                    "fiscal_name": "Cliente existente",
                }]),
                mapping={"code": "code", "fiscal_name": "fiscal_name"},
                mode="update_existing",
            )

            db.expire_all()
            preserved = db.scalars(
                select(CustomerContactPoint).where(
                    CustomerContactPoint.customer_id == customer.id,
                    CustomerContactPoint.type == "phone",
                    CustomerContactPoint.active.is_(True),
                    CustomerContactPoint.is_primary.is_(False),
                )
            ).all()
            self.assertEqual([point.value for point in preserved], ["612345678"])
        finally:
            db.close()
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
