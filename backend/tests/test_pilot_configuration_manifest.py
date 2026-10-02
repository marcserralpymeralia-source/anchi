from __future__ import annotations

import unittest
from unittest.mock import patch

from app.core.pilot_configuration import PilotConfigurationManifest, compare_runtime_to_approved


class PilotConfigurationManifestTests(unittest.TestCase):
    def test_hash_is_stable_across_input_order(self):
        first = PilotConfigurationManifest(tenant_id=4).build(
            connectors=[{"key": "z", "runtime_enabled": False}, {"key": "a", "runtime_enabled": True}],
            routes=[{"source": "catalog", "destination": "rest"}, {"source": "manual", "destination": "none"}],
            enabled_modules=["whatsapp_catalog", "generic_rest_mock"],
        )
        second = PilotConfigurationManifest(tenant_id=4).build(
            connectors=[{"key": "a", "runtime_enabled": True}, {"key": "z", "runtime_enabled": False}],
            routes=[{"source": "manual", "destination": "none"}, {"source": "catalog", "destination": "rest"}],
            enabled_modules=["generic_rest_mock", "whatsapp_catalog"],
        )
        self.assertEqual(first["hash"], second["hash"])

    def test_critical_profile_routes_modules_and_http_gate_drift(self):
        baseline = PilotConfigurationManifest(tenant_id=4).build()
        current = PilotConfigurationManifest(tenant_id=4).build(
            disabled_modules=["audio", "sage_real", "odoo_real"],
            routes=[{"source": "catalog", "destination": "external_http"}],
            runtime_gates={"PILOT_HTTP_CONNECTOR_ENABLED": True},
        )
        diff = PilotConfigurationManifest(tenant_id=4).compare(current, baseline)
        self.assertEqual(diff["status"], "DRIFT")
        self.assertTrue({"disabled_modules", "routes", "runtime_gates"}.issubset({item["field"] for item in diff["critical"]}))

    def test_unexpected_email_audio_connection_and_policy_changes_are_critical(self):
        baseline = PilotConfigurationManifest(tenant_id=4).build()
        current = PilotConfigurationManifest(tenant_id=4).build(
            enabled_modules=["whatsapp_catalog", "email_autodiscovery", "audio"],
            connection_fingerprints=[{"connection_id": 7, "connector_key": "generic_rest", "fingerprint": "changed"}],
            retention_policy_version="retention-v2",
            rbac_policy_version="rbac-v2",
        )
        diff = PilotConfigurationManifest(tenant_id=4).compare(current, baseline)
        fields = {item["field"] for item in diff["critical"]}
        self.assertTrue({"enabled_modules", "connection_fingerprints", "retention_policy_version", "rbac_policy_version"}.issubset(fields))

    def test_secret_material_is_redacted_and_tenant_scopes_hash(self):
        manifest = PilotConfigurationManifest(tenant_id=4).build(runtime_gates={"CRON_SECRET": "do-not-leak", "http": False})
        self.assertNotIn("do-not-leak", str(manifest))
        self.assertEqual(manifest["runtime_gates"]["CRON_SECRET"], "[REDACTED]")
        other = PilotConfigurationManifest(tenant_id=5).build(runtime_gates={"CRON_SECRET": "different"})
        self.assertNotEqual(manifest["hash"], other["hash"])

    def test_match_and_invalid_baseline_are_explicit(self):
        current = PilotConfigurationManifest().build()
        baseline = __import__("json").dumps(current)
        self.assertEqual(compare_runtime_to_approved(current, baseline)["status"], "MATCH")
        self.assertEqual(compare_runtime_to_approved(current, "not-json")["status"], "INVALID_BASELINE")

    def test_runtime_boolean_gates_parse_false_without_exposing_secret_values(self):
        with patch.dict("os.environ", {"RUN_INTERNAL_JOB_WORKER": "false", "CRON_SECRET": "sensitive-cron-value", "APP_ENV": "test"}, clear=False):
            runtime = PilotConfigurationManifest.from_runtime()
        self.assertFalse(runtime["runtime_gates"]["RUN_INTERNAL_JOB_WORKER"])
        self.assertTrue(runtime["runtime_gates"]["CRON_SECRET"])
        self.assertEqual(runtime["runtime_gates"]["secret_provider_kind"], "environment")
        self.assertNotIn("sensitive-cron-value", str(runtime))

    def test_runtime_storage_connector_cron_gates_are_manifested_and_critical(self):
        with patch.dict("os.environ", {
            "APP_ENV": "test",
            "ATTACHMENT_STORAGE_BACKEND": "vercel_blob",
            "PILOT_GENERIC_REST_RUNTIME_ENABLED": "true",
            "EXTERNAL_CONNECTOR_HTTP_ENABLED": "true",
            "PILOT_SCHEDULED_JOB_PROFILE": "CATALOG_ONLY_PILOT",
            "PILOT_CRON_ENDPOINT_URL": "https://pilot.example/cron/jobs",
            "PILOT_CRON_ALLOWED_HOST": "pilot.example",
            "BLOB_STORE_HOST": "anchi.private.blob.vercel-storage.com",
        }, clear=False):
            runtime = PilotConfigurationManifest.from_runtime()
        self.assertEqual(runtime["storage_backend"], "vercel_blob")
        self.assertTrue(runtime["runtime_gates"]["PILOT_GENERIC_REST_RUNTIME_ENABLED"])
        self.assertTrue(runtime["runtime_gates"]["EXTERNAL_CONNECTOR_HTTP_ENABLED"])
        self.assertEqual(runtime["runtime_gates"]["scheduled_job_profile"], "CATALOG_ONLY_PILOT")
        self.assertTrue(runtime["runtime_gates"]["cron_endpoint_configured"])
        prior = PilotConfigurationManifest().build(storage_backend="filesystem")
        current = PilotConfigurationManifest().build(storage_backend="vercel_blob")
        drift = PilotConfigurationManifest().compare(current, prior)
        self.assertIn("storage_backend", {item["field"] for item in drift["critical"]})


if __name__ == "__main__":
    unittest.main()
