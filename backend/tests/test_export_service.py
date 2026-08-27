from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import (
    Company,
    Customer,
    ExportSettings,
    Order,
    OrderLine,
    Product,
)
from app.exports.service import ExportService


class ExportServiceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "tenant.sqlite"
        self.engine = create_engine(
            f"sqlite:///{self.path.as_posix()}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def _seed(self):
        db = self.Session()

        db.add(Company(id=1, name="Demo"))

        customer = Customer(
            id=1,
            company_id=1,
            code="C001",
            fiscal_name="Cliente Demo",
            status="active",
        )

        product = Product(
            id=1,
            company_id=1,
            reference="P-100",
            name="Producto Demo",
            sale_unit="uds",
            sale_price=10,
            status="active",
        )

        order = Order(
            id=1,
            company_id=1,
            customer_id=1,
            validated_customer_id=1,
            order_date="2026-08-24",
            status="pedido_confirmado",
        )

        line = OrderLine(
            company_id=1,
            order_id=1,
            product_id=1,
            validated_product_id=1,
            quantity=2.5,
            unit="cajas",
            validation_status="validated",
        )

        db.add_all([customer, product, order, line])
        db.commit()

        return db, order

    def test_csv_respects_configured_fields_and_order(self):
        db, order = self._seed()

        db.add(
            ExportSettings(
                company_id=1,
                file_type="csv",
                csv_separator=";",
                include_header=True,
                date_format="%d/%m/%Y",
                decimal_separator=",",
                filename_template="PEDIDO_{codigo_cliente}_{fecha}_{id_pedido}.csv",
                header_fields="customer_code,order_id,order_date",
                line_fields="reference,description,quantity,unit",
            )
        )
        db.commit()
        db.refresh(order)

        export = ExportService().generate_csv(db, order)

        lines = export.content.strip().splitlines()

        self.assertEqual(
            lines[0],
            "customer_code;order_id;order_date;reference;description;quantity;unit",
        )
        self.assertEqual(
            lines[1],
            "C001;1;24/08/2026;P-100;Producto Demo;2,5;cajas",
        )

        db.close()

    def test_csv_can_omit_header(self):
        db, order = self._seed()

        db.add(
            ExportSettings(
                company_id=1,
                file_type="csv",
                csv_separator="|",
                include_header=False,
                date_format="%Y%m%d",
                decimal_separator=".",
                header_fields="order_id,customer_code",
                line_fields="reference,quantity",
            )
        )
        db.commit()
        db.refresh(order)

        export = ExportService().generate_csv(db, order)

        lines = export.content.strip().splitlines()

        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "1|C001|P-100|2.5")

        db.close()

    def test_unknown_export_field_is_rejected(self):
        db, order = self._seed()

        db.add(
            ExportSettings(
                company_id=1,
                file_type="csv",
                header_fields="order_id,unknown_field",
                line_fields="reference,quantity",
            )
        )
        db.commit()
        db.refresh(order)

        with self.assertRaisesRegex(ValueError, "unknown_field"):
            ExportService().generate_csv(db, order)

        db.close()


if __name__ == "__main__":
    unittest.main()
