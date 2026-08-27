from datetime import date
from io import BytesIO, StringIO
import csv
import ftplib
import posixpath
import socket
import ssl

from sqlalchemy.orm import Session

from app.core.encryption import decrypt_secret
from app.db.models import ExportFile, ExportSettings, FTPSettings, Order
from app.settings.service import get_or_create_settings


class ExportService:
    ORDER_FIELDS = {
        "order_id",
        "customer_code",
        "order_date",
        "requested_delivery_date",
        "notes",
    }

    LINE_FIELDS = {
        "reference",
        "description",
        "quantity",
        "unit",
    }

    def _parse_fields(self, raw: str | None) -> list[str]:
        return [
            field.strip()
            for field in (raw or "").split(",")
            if field.strip()
        ]

    def _format_date(self, value: str | None, date_format: str) -> str:
        if not value:
            return ""

        try:
            parsed = date.fromisoformat(str(value))
        except ValueError:
            return str(value)

        return parsed.strftime(date_format)

    def _format_number(self, value, decimal_separator: str) -> str:
        if value is None:
            return ""

        number = float(value)

        if number.is_integer():
            formatted = str(int(number))
        else:
            formatted = format(number, "g")

        if decimal_separator == ",":
            formatted = formatted.replace(".", ",")

        return formatted

    def _order_value(
        self,
        field: str,
        order: Order,
        customer_code: str,
        settings: ExportSettings,
    ) -> str:
        values = {
            "order_id": str(order.id),
            "customer_code": customer_code,
            "order_date": self._format_date(
                order.order_date,
                settings.date_format,
            ),
            "requested_delivery_date": self._format_date(
                order.requested_delivery_date,
                settings.date_format,
            ),
            "notes": order.notes or "",
        }

        return values[field]

    def _line_value(
        self,
        field: str,
        line,
        settings: ExportSettings,
    ) -> str:
        product = line.validated_product or line.product

        values = {
            "reference": (
                product.reference
                if product
                else line.detected_reference or ""
            ),
            "description": (
                product.name
                if product
                else line.detected_product or ""
            ),
            "quantity": self._format_number(
                line.quantity,
                settings.decimal_separator,
            ),
            "unit": line.unit or "",
        }

        return values[field]

    def generate_csv(self, db: Session, order: Order) -> ExportFile:
        settings = get_or_create_settings(
            db,
            ExportSettings,
            order.company_id,
        )

        if (settings.file_type or "csv").lower() != "csv":
            raise ValueError(
                f"Formato de exportacion no soportado: {settings.file_type}"
            )

        header_fields = self._parse_fields(settings.header_fields)
        line_fields = self._parse_fields(settings.line_fields)

        unknown_header = [
            field
            for field in header_fields
            if field not in self.ORDER_FIELDS
        ]
        unknown_line = [
            field
            for field in line_fields
            if field not in self.LINE_FIELDS
        ]

        unknown_fields = unknown_header + unknown_line
        if unknown_fields:
            raise ValueError(
                "Campos de exportacion no soportados: "
                + ", ".join(unknown_fields)
            )

        fields = header_fields + line_fields

        if not fields:
            raise ValueError(
                "La exportacion no tiene campos configurados."
            )

        customer_code = (
            order.validated_customer.code
            if order.validated_customer
            else ""
        )

        output = StringIO()
        writer = csv.writer(
            output,
            delimiter=settings.csv_separator or ";",
            lineterminator="\n",
        )

        if settings.include_header:
            writer.writerow(fields)

        for line in order.lines:
            row = []

            for field in header_fields:
                row.append(
                    self._order_value(
                        field,
                        order,
                        customer_code,
                        settings,
                    )
                )

            for field in line_fields:
                row.append(
                    self._line_value(
                        field,
                        line,
                        settings,
                    )
                )

            writer.writerow(row)

        filename = settings.filename_template.format(
            codigo_cliente=customer_code or "SINCLIENTE",
            fecha=date.today().strftime("%Y%m%d"),
            id_pedido=order.id,
        )

        export = ExportFile(
            company_id=order.company_id,
            order_id=order.id,
            filename=filename,
            content=output.getvalue(),
            status="generated",
        )

        db.add(export)
        db.commit()
        db.refresh(export)

        return export


class ImplicitFTP_TLS(ftplib.FTP_TLS):
    """FTPS implicit: TLS is established immediately after TCP connect."""

    def connect(self, host="", port=0, timeout=-999, source_address=None):
        if host:
            self.host = host
        if port > 0:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        if source_address is not None:
            self.source_address = source_address

        self.sock = socket.create_connection(
            (self.host, self.port),
            self.timeout,
            source_address=self.source_address,
        )
        self.af = self.sock.family
        self.sock = self.context.wrap_socket(
            self.sock,
            server_hostname=self.host,
        )
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome


class FTPService:
    SUPPORTED_TYPES = {"ftp", "ftps_explicit", "ftps_implicit"}

    def _credentials(self, settings: FTPSettings) -> tuple[str, str]:
        username = (settings.username or "").strip()
        password = decrypt_secret(settings.password_encrypted) or ""
        if not username:
            raise ValueError("Falta el usuario de la conexion de exportacion.")
        if not password:
            raise ValueError("Falta la contraseña de la conexion de exportacion.")
        return username, password

    def _client(self, settings: FTPSettings):
        connection_type = (settings.connection_type or "").strip().lower()
        if connection_type not in self.SUPPORTED_TYPES:
            raise ValueError(
                f"Tipo de conexion no soportado: {connection_type or 'vacio'}"
            )

        host = (settings.host or "").strip()
        if not host:
            raise ValueError("Falta el host de la conexion de exportacion.")

        timeout = max(int(settings.timeout_seconds or 30), 1)
        port = int(settings.port or (990 if connection_type == "ftps_implicit" else 21))

        if connection_type == "ftp":
            client = ftplib.FTP(timeout=timeout)
        elif connection_type == "ftps_explicit":
            client = ftplib.FTP_TLS(
                context=ssl.create_default_context(),
                timeout=timeout,
            )
        else:
            client = ImplicitFTP_TLS(
                context=ssl.create_default_context(),
                timeout=timeout,
            )

        client.connect(host, port)
        return client

    def _login(self, client, settings: FTPSettings) -> None:
        username, password = self._credentials(settings)
        connection_type = (settings.connection_type or "").strip().lower()

        client.login(username, password)

        if connection_type in {"ftps_explicit", "ftps_implicit"}:
            client.prot_p()

        client.set_pasv(bool(settings.passive_mode))

    def test_connection(self, settings: FTPSettings) -> bool:
        client = self._client(settings)
        try:
            self._login(client, settings)
            client.voidcmd("NOOP")
            return True
        finally:
            try:
                client.quit()
            except Exception:
                try:
                    client.close()
                except Exception:
                    pass

    def send(
        self,
        export_file: ExportFile,
        settings: FTPSettings,
        *,
        encoding: str = "utf-8",
    ) -> bool:
        if not export_file.filename:
            raise ValueError("El archivo de exportacion no tiene nombre.")

        destination = (settings.destination_path or "/").strip() or "/"
        remote_path = posixpath.join(destination, export_file.filename)

        client = self._client(settings)
        try:
            self._login(client, settings)

            if not settings.overwrite_files:
                try:
                    existing = client.nlst(remote_path)
                except ftplib.error_perm as exc:
                    # 550 suele significar que el fichero/ruta no existe.
                    if not str(exc).startswith("550"):
                        raise
                    existing = []

                if existing:
                    raise FileExistsError(
                        f"El archivo remoto ya existe: {remote_path}"
                    )

            try:
                payload = (export_file.content or "").encode(encoding or "utf-8")
            except LookupError as exc:
                raise ValueError(
                    f"Encoding de exportacion no valido: {encoding}"
                ) from exc
            except UnicodeEncodeError as exc:
                raise ValueError(
                    f"El contenido no se puede codificar como {encoding}"
                ) from exc
            client.storbinary(
                f"STOR {remote_path}",
                BytesIO(payload),
            )
            return True
        finally:
            try:
                client.quit()
            except Exception:
                try:
                    client.close()
                except Exception:
                    pass
