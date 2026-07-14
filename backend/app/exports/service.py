from datetime import date
from io import StringIO
import csv

from sqlalchemy.orm import Session

from app.db.models import ExportFile, ExportSettings, Order
from app.settings.service import get_or_create_settings


class ExportService:
    def generate_csv(self, db: Session, order: Order) -> ExportFile:
        settings = get_or_create_settings(db, ExportSettings, order.company_id)
        output = StringIO()
        writer = csv.writer(output, delimiter=settings.csv_separator)
        if settings.include_header:
            writer.writerow(["pedido", "cliente", "fecha", "referencia", "producto", "cantidad", "unidad"])
        customer_code = order.validated_customer.code if order.validated_customer else ""
        for line in order.lines:
            product = line.validated_product or line.product
            writer.writerow([
                order.id,
                customer_code,
                order.order_date or date.today().isoformat(),
                product.reference if product else line.detected_reference or "",
                product.name if product else line.detected_product or "",
                line.quantity or "",
                line.unit or "",
            ])
        filename = settings.filename_template.format(codigo_cliente=customer_code or "SINCLIENTE", fecha=date.today().strftime("%Y%m%d"), id_pedido=order.id)
        export = ExportFile(company_id=order.company_id, order_id=order.id, filename=filename, content=output.getvalue(), status="generated")
        db.add(export)
        db.commit()
        db.refresh(export)
        return export


class FTPService:
    def test_connection(self) -> bool:
        return True

    def send(self, export_file: ExportFile) -> bool:
        return True
