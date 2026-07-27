from __future__ import annotations

import os
import unittest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")
os.environ.setdefault("PERFORMANCE_PROFILING_ENABLED", "true")
os.environ.setdefault("ENABLE_PERFORMANCE_PROFILING", "true")

from scripts.performance_data import build_performance_fixture, performance_test_client  # noqa: E402


class SettingsImapConnectionRouteTests(unittest.TestCase):
    def test_imap_test_route_never_surfaces_internal_error(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client, unittest.mock.patch(
                "app.settings.routes.test_imap_connection",
                side_effect=ConnectionRefusedError("refused"),
            ):
                response = client.post("/settings/email/imap/test", follow_redirects=False)

            self.assertEqual(response.status_code, 303)
            self.assertTrue(response.headers["location"].endswith("#email-diagnostics"))
            self.assertNotIn("internal_error", response.text.lower())
        finally:
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
