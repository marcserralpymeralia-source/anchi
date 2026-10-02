from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ImportStorageTests(unittest.TestCase):
    def test_resolve_temp_storage_dir_prefers_configured_and_vercel_roots(self):
        from app.core.storage import resolve_temp_storage_dir

        with patch.dict(os.environ, {"TEMP_STORAGE_DIR": "/custom/anchi", "VERCEL": ""}, clear=False):
            self.assertEqual(resolve_temp_storage_dir("import_previews"), Path("/custom/anchi/import_previews"))

        with patch.dict(os.environ, {"TEMP_STORAGE_DIR": "", "VERCEL": "1"}, clear=False):
            self.assertEqual(resolve_temp_storage_dir("import_previews"), Path("/tmp/anchi/import_previews"))

        with patch.dict(os.environ, {"TEMP_STORAGE_DIR": "", "VERCEL": ""}, clear=False):
            expected = Path(__file__).resolve().parents[1] / "app" / "storage" / "import_previews"
            self.assertEqual(resolve_temp_storage_dir("import_previews"), expected)

    def test_imports_service_does_not_create_preview_dir_during_import(self):
        module_name = "app.imports.service"
        previous = sys.modules.pop(module_name, None)
        try:
            with patch.dict(os.environ, {"APP_ENV": "test", "VERCEL": "1", "TEMP_STORAGE_DIR": ""}, clear=False), patch(
                "pathlib.Path.mkdir",
                side_effect=AssertionError("mkdir should not be called during import"),
            ):
                module = importlib.import_module(module_name)
                self.assertEqual(module.PREVIEW_DIR.as_posix(), "/tmp/anchi/import_previews")
        finally:
            sys.modules.pop(module_name, None)
            if previous is not None:
                sys.modules[module_name] = previous

    def test_read_attachment_uses_blob_client_for_http_urls(self):
        from app.core.attachment_storage import read_attachment

        calls: dict[str, object] = {}

        class FakeBlobClient:
            def __init__(self) -> None:
                calls["init"] = True

            def get(self, storage_ref: str, access: str | None = None, token: str | None = None):  # noqa: ANN001
                calls["storage_ref"] = storage_ref
                calls["access"] = access
                calls["token"] = token
                return SimpleNamespace(content=b"blob-bytes")

            def close(self) -> None:
                calls["closed"] = True

        vercel_module = ModuleType("vercel")
        blob_module = ModuleType("vercel.blob")
        blob_module.BlobClient = FakeBlobClient
        vercel_module.blob = blob_module

        reference = "https://anchi.private.blob.vercel-storage.com/tenants/7/attachments/pedido.pdf"
        with patch.dict(os.environ, {"ATTACHMENT_STORAGE_BACKEND": "vercel_blob", "BLOB_READ_WRITE_TOKEN": "test-token", "BLOB_STORE_HOST": "anchi.private.blob.vercel-storage.com"}, clear=False), patch.dict(sys.modules, {"vercel": vercel_module, "vercel.blob": blob_module}):
            content = read_attachment(reference, company_id=7)

        self.assertEqual(content, b"blob-bytes")
        self.assertTrue(calls["init"])
        self.assertEqual(calls["storage_ref"], reference)
        self.assertEqual(calls["access"], "private")
        self.assertEqual(calls["token"], "test-token")
        self.assertTrue(calls["closed"])

    def test_save_attachment_rejects_ephemeral_vercel_storage(self):
        from app.core.attachment_storage import save_attachment

        with patch.dict(
            os.environ,
            {"VERCEL": "1", "APP_ENV": "production", "ATTACHMENT_STORAGE_BACKEND": "vercel_blob", "BLOB_READ_WRITE_TOKEN": "", "BLOB_STORE_ID": ""},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "falta su credencial"):
                save_attachment(filename="pedido.txt", payload=b"pedido", content_type="text/plain")

    def test_save_attachment_namespaces_local_files_by_company(self):
        from app.core.attachment_storage import read_attachment, save_attachment

        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            os.environ,
            {"TEMP_STORAGE_DIR": tempdir, "VERCEL": "", "VERCEL_ENV": ""},
            clear=False,
        ):
            storage_ref = save_attachment(
                filename="pedido.pdf",
                payload=b"tenant-file",
                content_type="application/pdf",
                company_id=7,
            )

            path = Path(storage_ref)
            self.assertTrue(path.is_file())
            self.assertEqual(path.parent.parent.name, "7")
            self.assertEqual(path.parent.parent.parent.name, "tenants")
            self.assertEqual(read_attachment(storage_ref, company_id=7), b"tenant-file")

    def test_storage_readiness_probes_local_filesystem(self):
        from app.core.storage import check_storage_readiness

        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            os.environ,
            {"TEMP_STORAGE_DIR": tempdir, "VERCEL": "", "VERCEL_ENV": ""},
            clear=False,
        ):
            result = check_storage_readiness()

        self.assertTrue(result["ok"])
        self.assertEqual(result["backend"], "filesystem")
        self.assertEqual(result["probe"], "write_read_delete")

    def test_storage_readiness_rejects_vercel_without_blob_configuration(self):
        from app.core.storage import check_storage_readiness

        with patch.dict(
            os.environ,
            {"TEMP_STORAGE_DIR": "", "APP_ENV": "production", "ATTACHMENT_STORAGE_BACKEND": "vercel_blob", "VERCEL": "1", "VERCEL_ENV": "production", "BLOB_READ_WRITE_TOKEN": ""},
            clear=False,
        ):
            result = check_storage_readiness(force=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_type"], "missing_configuration")


if __name__ == "__main__":
    unittest.main()
