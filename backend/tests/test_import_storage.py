from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ImportStorageTests(unittest.TestCase):
    def test_normalize_store_id_removes_prefix_and_preserves_case(self):
        from app.core.attachment_storage import _normalize_store_id

        self.assertEqual(_normalize_store_id("store_tIbkSOAi0UQaYnW3"), "tIbkSOAi0UQaYnW3")
        self.assertEqual(_normalize_store_id("tIbkSOAi0UQaYnW3"), "tIbkSOAi0UQaYnW3")

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
                self.assertEqual(str(module.PREVIEW_DIR), "/tmp/anchi/import_previews")
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

            def get(self, storage_ref: str, access: str | None = None):  # noqa: ANN001
                calls["storage_ref"] = storage_ref
                calls["access"] = access
                return SimpleNamespace(content=b"blob-bytes")

            def close(self) -> None:
                calls["closed"] = True

        vercel_module = ModuleType("vercel")
        blob_module = ModuleType("vercel.blob")
        blob_module.BlobClient = FakeBlobClient
        vercel_module.blob = blob_module

        with patch.dict(
            os.environ,
            {"VERCEL": "", "VERCEL_ENV": "", "UAT_STORE_ID": ""},
            clear=False,
        ), patch.dict(sys.modules, {"vercel": vercel_module, "vercel.blob": blob_module}):
            content = read_attachment("https://blob.example.com/attachments/pedido.pdf")

        self.assertEqual(content, b"blob-bytes")
        self.assertTrue(calls["init"])
        self.assertEqual(calls["storage_ref"], "https://blob.example.com/attachments/pedido.pdf")
        self.assertEqual(calls["access"], "private")
        self.assertTrue(calls["closed"])

    def test_save_attachment_rejects_ephemeral_vercel_storage(self):
        from app.core.attachment_storage import save_attachment

        with patch.dict(
            os.environ,
            {"VERCEL": "1", "BLOB_READ_WRITE_TOKEN": "", "BLOB_STORE_ID": "", "UAT_STORE_ID": ""},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "Persistent attachment storage"):
                save_attachment(filename="pedido.txt", payload=b"pedido", content_type="text/plain")

    def test_uat_store_uses_oidc_blob_without_rw_token(self):
        from app.core.attachment_storage import save_attachment

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):  # noqa: ANN002
                return None

            def read(self):
                return b'{"url":"https://tibksoai0uqaynw3.private.blob.vercel-storage.com/test.txt"}'

        with patch.dict(
            os.environ,
            {
                "VERCEL": "1",
                "VERCEL_ENV": "preview",
                "UAT_STORE_ID": "store_tIbkSOAi0UQaYnW3",
                "BLOB_READ_WRITE_TOKEN": "",
            },
            clear=False,
        ), patch("app.core.attachment_storage._get_oidc_token", return_value="oidc-token"), patch(
            "app.core.attachment_storage.urllib.request.urlopen",
            return_value=FakeResponse(),
        ) as urlopen:
            result = save_attachment(filename="test.txt", payload=b"test", content_type="text/plain")

        request = urlopen.call_args.args[0]
        self.assertIn("pathname=attachments%2F", request.full_url)
        self.assertEqual(request.method, "PUT")
        self.assertEqual(request.headers["Authorization"], "Bearer oidc-token")
        self.assertEqual(request.headers["X-vercel-blob-store-id"], "tIbkSOAi0UQaYnW3")
        self.assertEqual(request.headers["X-vercel-blob-access"], "private")
        self.assertEqual(result, "https://tibksoai0uqaynw3.private.blob.vercel-storage.com/test.txt")

    def test_uat_store_reads_private_blob_with_oidc(self):
        from app.core.attachment_storage import read_attachment

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):  # noqa: ANN002
                return None

            def read(self):
                return b"private-content"

        with patch.dict(
            os.environ,
            {
                "VERCEL": "1",
                "VERCEL_ENV": "preview",
                "UAT_STORE_ID": "store_tIbkSOAi0UQaYnW3",
            },
            clear=False,
        ), patch("app.core.attachment_storage._get_oidc_token", return_value="oidc-token"), patch(
            "app.core.attachment_storage.urllib.request.urlopen",
            return_value=FakeResponse(),
        ) as urlopen:
            result = read_attachment("https://tibksoai0uqaynw3.private.blob.vercel-storage.com/test.txt")

        request = urlopen.call_args.args[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.headers["Authorization"], "Bearer oidc-token")
        self.assertEqual(result, b"private-content")

    def test_uat_store_rejects_private_blob_url_from_another_store(self):
        from app.core.attachment_storage import read_attachment

        with patch.dict(
            os.environ,
            {
                "VERCEL": "1",
                "VERCEL_ENV": "preview",
                "UAT_STORE_ID": "store_tIbkSOAi0UQaYnW3",
            },
            clear=False,
        ), patch("app.core.attachment_storage._get_oidc_token") as get_token:
            with self.assertRaisesRegex(RuntimeError, "outside the configured UAT Blob store"):
                read_attachment("https://other-store.private.blob.vercel-storage.com/test.txt")

        get_token.assert_not_called()

    def test_uat_store_fails_closed_without_oidc_token(self):
        from app.core.attachment_storage import save_attachment

        with patch.dict(
            os.environ,
            {
                "VERCEL": "1",
                "VERCEL_ENV": "preview",
                "UAT_STORE_ID": "store_tIbkSOAi0UQaYnW3",
                "BLOB_READ_WRITE_TOKEN": "",
            },
            clear=False,
        ), patch("app.core.attachment_storage._get_oidc_token", return_value=""):
            with self.assertRaisesRegex(RuntimeError, "OIDC token"):
                save_attachment(filename="test.txt", payload=b"test")

    def test_production_keeps_legacy_blob_client(self):
        from app.core.attachment_storage import save_attachment

        calls: dict[str, object] = {}

        class FakeBlobClient:
            def __init__(self) -> None:
                calls["init"] = True

            def put(self, *args, **kwargs):  # noqa: ANN002, ANN003
                calls["args"] = args
                calls["kwargs"] = kwargs
                return SimpleNamespace(url="https://legacy.blob.example/attachment")

            def close(self) -> None:
                calls["closed"] = True

        vercel_module = ModuleType("vercel")
        blob_module = ModuleType("vercel.blob")
        blob_module.BlobClient = FakeBlobClient
        vercel_module.blob = blob_module

        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "VERCEL": "1",
                "VERCEL_ENV": "production",
                "UAT_STORE_ID": "",
                "BLOB_READ_WRITE_TOKEN": "legacy-token",
            },
            clear=False,
        ), patch.dict(sys.modules, {"vercel": vercel_module, "vercel.blob": blob_module}):
            result = save_attachment(filename="test.txt", payload=b"test", content_type="text/plain")

        self.assertEqual(result, "https://legacy.blob.example/attachment")
        self.assertTrue(calls["init"])
        self.assertTrue(calls["closed"])


if __name__ == "__main__":
    unittest.main()
