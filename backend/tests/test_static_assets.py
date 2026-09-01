from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.core.app_factory import create_app
from app.core.assets import versioned_asset_url


class StaticAssetTests(unittest.TestCase):
    def test_bundled_assets_have_versioned_urls(self):
        url = versioned_asset_url("styles.css")

        self.assertRegex(url, r"^/static/styles\.css\?v=[0-9a-f]+-[0-9a-f]+$")
        with self.assertRaises(ValueError):
            versioned_asset_url("../secrets.txt")

    def test_bundled_css_is_cacheable(self):
        with TestClient(create_app(), raise_server_exceptions=False) as client:
            response = client.get("/static/styles.css?v=test")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "public, max-age=31536000, immutable")
