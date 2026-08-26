from __future__ import annotations

import unittest

from fastapi import Request

from app.core.router_registry import get_registered_routers
from app.legal.routes import privacy_policy


class PrivacyPageTests(unittest.TestCase):
    def test_privacy_route_is_public_and_renders_policy(self):
        route_paths = {route.path for router in get_registered_routers() for route in router.routes}
        self.assertIn("/privacy", route_paths)

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/privacy",
                "raw_path": b"/privacy",
                "query_string": b"",
                "headers": [],
                "scheme": "https",
                "server": ("testserver", 443),
                "client": ("testclient", 1234),
            }
        )
        response = privacy_policy(request)
        body = response.body.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Política de privacidad", body)
        self.assertIn("PYMERALIA S.L.", body)
        self.assertIn("whatsapp", body.lower())
        self.assertIn("hola@pymeralia.com", body)
        self.assertNotIn("Iniciar sesión", body)


if __name__ == "__main__":
    unittest.main()
