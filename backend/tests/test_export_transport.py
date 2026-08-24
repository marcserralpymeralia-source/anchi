from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.core.encryption import encrypt_secret
from app.exports.service import FTPService


class FTPTransportTests(unittest.TestCase):
    def _settings(self, connection_type: str, port: int):
        return SimpleNamespace(
            connection_type=connection_type,
            host="ftp.example.com",
            port=port,
            username="user",
            password_encrypted=encrypt_secret("secret"),
            destination_path="/orders",
            passive_mode=True,
            overwrite_files=True,
            timeout_seconds=30,
        )

    def _export(self):
        return SimpleNamespace(
            filename="PEDIDO_1.csv",
            content="pedido;cliente\n1;C001\n",
        )

    @patch("app.exports.service.ftplib.FTP")
    def test_plain_ftp_upload(self, ftp_cls):
        client = MagicMock()
        ftp_cls.return_value = client

        ok = FTPService().send(
            self._export(),
            self._settings("ftp", 21),
        )

        self.assertTrue(ok)
        client.connect.assert_called_once_with("ftp.example.com", 21)
        client.login.assert_called_once_with("user", "secret")
        client.set_pasv.assert_called_once_with(True)
        client.storbinary.assert_called_once()
        client.quit.assert_called_once()

    @patch("app.exports.service.ftplib.FTP_TLS")
    def test_explicit_ftps_uses_protected_data_channel(self, ftp_tls_cls):
        client = MagicMock()
        ftp_tls_cls.return_value = client

        ok = FTPService().send(
            self._export(),
            self._settings("ftps_explicit", 21),
        )

        self.assertTrue(ok)
        client.connect.assert_called_once_with("ftp.example.com", 21)
        client.login.assert_called_once_with("user", "secret")
        client.prot_p.assert_called_once()
        client.storbinary.assert_called_once()

    @patch("app.exports.service.ImplicitFTP_TLS")
    def test_implicit_ftps_upload(self, implicit_cls):
        client = MagicMock()
        implicit_cls.return_value = client

        ok = FTPService().send(
            self._export(),
            self._settings("ftps_implicit", 990),
        )

        self.assertTrue(ok)
        client.connect.assert_called_once_with("ftp.example.com", 990)
        client.login.assert_called_once_with("user", "secret")
        client.prot_p.assert_called_once()
        client.storbinary.assert_called_once()

    def test_unsupported_protocol_is_rejected(self):
        settings = self._settings("sftp", 22)

        with self.assertRaisesRegex(ValueError, "Tipo de conexion no soportado"):
            FTPService().send(self._export(), settings)

    @patch("app.exports.service.ftplib.FTP")
    def test_upload_respects_configured_encoding(self, ftp_cls):
        client = MagicMock()
        ftp_cls.return_value = client

        export = SimpleNamespace(
            filename="PEDIDO_1.csv",
            content="descripcion\nCafé\n",
        )

        ok = FTPService().send(
            export,
            self._settings("ftp", 21),
            encoding="iso-8859-1",
        )

        self.assertTrue(ok)

        args = client.storbinary.call_args.args
        payload_stream = args[1]

        self.assertEqual(
            payload_stream.getvalue(),
            "descripcion\nCafé\n".encode("iso-8859-1"),
        )

    def test_invalid_encoding_is_rejected(self):
        settings = self._settings("ftp", 21)

        with patch("app.exports.service.ftplib.FTP") as ftp_cls:
            client = MagicMock()
            ftp_cls.return_value = client

            with self.assertRaisesRegex(
                ValueError,
                "Encoding de exportacion no valido",
            ):
                FTPService().send(
                    self._export(),
                    settings,
                    encoding="encoding-que-no-existe",
                )


if __name__ == "__main__":
    unittest.main()
