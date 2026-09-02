from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")

from app.settings.autoconfig import (
    _mx_provider_candidates,
    _mx_provider_domains,
    _dns_candidates,
    detect_email_configuration,
    normalize_email,
)
from scripts.performance_data import build_performance_fixture, performance_test_client


class EmailAutoconfigTests(unittest.TestCase):
    def test_known_provider_returns_verified_imap_without_password(self):
        def probe_incoming(endpoint, username, password):
            return endpoint.protocol == "imap" and endpoint.host == "imap.gmail.com" and username == "demo.user@gmail.com" and password == "app-password"

        with patch("app.settings.autoconfig._probe_incoming", side_effect=probe_incoming), patch("app.settings.autoconfig._probe_smtp", return_value=False):
            result = detect_email_configuration(" Demo.User@Gmail.com ", "app-password")

        self.assertTrue(result["detected"])
        self.assertTrue(result["can_use_in_anchi"])
        self.assertEqual(result["provider"], "gmail")
        self.assertEqual(result["imap"]["host"], "imap.gmail.com")
        self.assertEqual(result["imap"]["username"], "demo.user@gmail.com")
        self.assertEqual(result["suggested_imap"]["host"], "imap.gmail.com")
        self.assertNotIn("password", result)

    def test_pop3_only_is_reported_but_not_marked_usable_by_anchi(self):
        with patch("app.settings.autoconfig._probe_incoming", side_effect=lambda endpoint, _username, _password: endpoint.protocol == "pop3"), patch("app.settings.autoconfig._probe_smtp", return_value=False):
            result = detect_email_configuration("demo.user@gmail.com", "app-password")

        self.assertTrue(result["detected"])
        self.assertFalse(result["can_use_in_anchi"])
        self.assertIsNone(result["imap"])
        self.assertEqual(result["pop3"]["protocol"], "pop3")
        self.assertIn("IMAP", result["message"])

    def test_invalid_input_is_rejected_before_network_probes(self):
        with self.assertRaises(ValueError):
            normalize_email("not-an-email")
        with self.assertRaises(ValueError):
            detect_email_configuration("demo@example.com", "")

    def test_thunderbird_configuration_expands_username_tokens(self):
        xml = b"""
        <clientConfig version=\"1.1\">
          <emailProvider id=\"example.com\">
            <domain>example.com</domain>
            <displayName>Example Mail</displayName>
            <incomingServer type=\"imap\">
              <hostname>imap.%EMAILDOMAIN%</hostname>
              <port>993</port>
              <socketType>SSL</socketType>
              <username>%EMAILADDRESS%</username>
            </incomingServer>
          </emailProvider>
        </clientConfig>
        """
        from app.settings.autoconfig import _parse_autoconfig_xml

        parsed = _parse_autoconfig_xml(xml, "person@example.com")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed[2][0].host, "imap.example.com")
        self.assertEqual(parsed[2][0].username, "person@example.com")
        self.assertEqual(parsed[2][0].security, "ssl_tls")

    def test_mx_discovery_derives_provider_domain_and_sibling_servers(self):
        provider_domains = _mx_provider_domains(
            ["mx01.mailhost.example.net.", "mx02.mailhost.example.net."],
            "customer.example.org",
        )
        self.assertEqual(provider_domains, ["example.net"])

        incoming, outgoing = _mx_provider_candidates(provider_domains)
        self.assertIn(("imap", "imap.example.net", 993), {(item.protocol, item.host, item.port) for item in incoming})
        self.assertIn(("smtp", "smtp.example.net", 587), {(item.protocol, item.host, item.port) for item in outgoing})
        self.assertTrue(all(item.source == "mx_provider_pattern" for item in (*incoming, *outgoing)))

    def test_custom_domain_uses_mx_derived_imap_before_common_fallbacks(self):
        with (
            patch("app.settings.autoconfig._dns_candidates", return_value=([], [], ["mx.mailhost.example.net"], ["mx.mailhost.example.net"])),
            patch("app.settings.autoconfig._published_configuration", return_value=None),
            patch("app.settings.autoconfig._published_configuration_from_mx_provider", return_value=None),
            patch("app.settings.autoconfig._host_is_public", return_value=True),
            patch("app.settings.autoconfig._probe_incoming", side_effect=lambda endpoint, _username, _password: endpoint.host == "imap.example.net"),
            patch("app.settings.autoconfig._probe_smtp", return_value=False),
        ):
            result = detect_email_configuration("person@customer.example.org", "app-password")

        self.assertTrue(result["detected"])
        self.assertEqual(result["imap"]["host"], "imap.example.net")
        self.assertEqual(result["imap"]["source"], "mx_provider_pattern")

    def test_mx_record_is_never_reported_as_imap_or_pop3(self):
        class FakeResolver:
            timeout = 0
            lifetime = 0

            def resolve(self, _name, record_type):
                if record_type == "MX":
                    return [SimpleNamespace(preference=10, exchange="mx.customer-mail.example.net.")]
                return []

        with patch("app.settings.autoconfig.dns_resolver", SimpleNamespace(Resolver=lambda: FakeResolver())):
            incoming, outgoing, exchanges, fingerprints = _dns_candidates("customer.example.org")

        self.assertEqual(incoming, [])
        self.assertEqual(outgoing, [])
        self.assertEqual(exchanges, ["mx.customer-mail.example.net"])
        self.assertEqual(fingerprints, exchanges)


class EmailAutoconfigRouteTests(unittest.TestCase):
    def test_channels_page_starts_with_only_email_and_password_inputs(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                response = client.get("/settings/channels")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text.count('data-email-autoconfig-root'), 1)
            self.assertIn('name="email"', response.text)
            self.assertIn('name="password"', response.text)
            self.assertIn('data-email-manual-panel hidden', response.text)
            self.assertIn('action="/settings/email/autoconfig"', response.text)
        finally:
            fixture.cleanup()

    def test_route_returns_discovery_result_without_persisting_credentials(self):
        fixture = build_performance_fixture("small")
        try:
            result = {
                "ok": True,
                "detected": True,
                "can_use_in_anchi": True,
                "provider": "gmail",
                "message": "Configuración IMAP encontrada y verificada.",
                "imap": {"protocol": "imap", "host": "imap.gmail.com", "port": 993, "security": "ssl_tls", "username": "demo@example.com", "folder": "INBOX", "source": "known_provider"},
            }
            with patch("app.settings.routes.detect_email_configuration", return_value=result):
                with performance_test_client(fixture) as client:
                    response = client.post(
                        "/settings/email/autoconfig",
                        data={"email": "demo@example.com", "password": "temporary-test-password"},
                    )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["imap"]["host"], "imap.gmail.com")
            self.assertNotIn("temporary-test-password", response.text)
        finally:
            fixture.cleanup()
