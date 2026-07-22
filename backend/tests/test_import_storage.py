from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
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
                self.assertEqual(str(module.PREVIEW_DIR), "/tmp/anchi/import_previews")
        finally:
            sys.modules.pop(module_name, None)
            if previous is not None:
                sys.modules[module_name] = previous


if __name__ == "__main__":
    unittest.main()
