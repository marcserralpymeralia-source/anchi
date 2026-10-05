from __future__ import annotations

import contextlib
import io
import os
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("APP_ENV", "test")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import attachment_storage, storage  # noqa: E402
from app.core.observability import encode_structured_message  # noqa: E402
from app.connectors.generic_rest import HTTPGenericRESTTransport, build_generic_rest_real_connector  # noqa: E402
from app.integrations.readiness import IntegrationReadinessService  # noqa: E402
from app.workers import jobs_worker  # noqa: E402
from app.workers.jobs_worker import CATALOG_ONLY_JOB_TYPES, JOB_TYPES, scheduled_job_types  # noqa: E402
from scripts.pilot_cron_invoker import invoke  # noqa: E402


class FakeBlobClient:
    instances: list["FakeBlobClient"] = []
    shared_objects: dict[str, bytes] = {}

    def __init__(self):
        self.objects = self.shared_objects
        self.calls: list[tuple[str, dict]] = []
        self.closed = False
        self.instances.append(self)

    def put(self, path, payload, **kwargs):
        self.calls.append(("put", kwargs))
        self.objects[path] = payload
        return types.SimpleNamespace(url=f"https://store-id.private.blob.vercel-storage.com/{path}")

    def get(self, path, **kwargs):
        self.calls.append(("get", kwargs))
        key = path.split("/", 3)[-1]
        if key not in self.objects:
            key = next((candidate for candidate in self.objects if path.endswith(candidate)), "")
        return types.SimpleNamespace(content=self.objects[key]) if key in self.objects else None

    def delete(self, path, **kwargs):
        self.calls.append(("delete", kwargs))
        key = next((candidate for candidate in self.objects if path.endswith(candidate)), path)
        self.objects.pop(key, None)

    def close(self):
        self.closed = True


class FakeHTTPResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def install_fake_blob():
    package = types.ModuleType("vercel")
    package.__path__ = []
    module = types.ModuleType("vercel.blob")
    module.BlobClient = FakeBlobClient
    return patch.dict(sys.modules, {"vercel": package, "vercel.blob": module})


class PilotRuntimeAdaptationTests(unittest.TestCase):
    def setUp(self):
        FakeBlobClient.instances.clear()
        FakeBlobClient.shared_objects.clear()
        storage._probe_cache = None
        storage._probe_cache_until = 0
        storage._probe_cache_key = None

    def test_blob_selected_by_configuration_outside_vercel_and_tenant_namespace_preserved(self):
        with install_fake_blob(), patch.dict(os.environ, {
            "APP_ENV": "production",
            "ATTACHMENT_STORAGE_BACKEND": "vercel_blob",
            "BLOB_READ_WRITE_TOKEN": "test-only-token",
            "BLOB_STORE_HOST": "store-id.private.blob.vercel-storage.com",
        }, clear=False), patch.object(attachment_storage, "resolve_temp_storage_dir") as local_storage:
            reference = attachment_storage.save_attachment(filename="order.pdf", payload=b"synthetic", company_id=42)
            self.assertIn("tenants/42/attachments/", reference)
            self.assertEqual(FakeBlobClient.instances[0].calls[0][1]["token"], "test-only-token")
            self.assertEqual(attachment_storage.read_attachment(reference, company_id=42), b"synthetic")
            self.assertEqual(FakeBlobClient.instances[1].calls[0][1]["token"], "test-only-token")
            before = len(FakeBlobClient.instances)
            with self.assertRaisesRegex(RuntimeError, "Blob privado autorizado"):
                attachment_storage.read_attachment("https://attacker.example/steal")
            with self.assertRaisesRegex(RuntimeError, "no pertenece al tenant"):
                attachment_storage.read_attachment(reference, company_id=43)
            with self.assertRaisesRegex(RuntimeError, "Blob privado autorizado"):
                attachment_storage.read_attachment("https://other-store.private.blob.vercel-storage.com/tenants/42/attachments/file.pdf", company_id=42)
            self.assertEqual(len(FakeBlobClient.instances), before)
            local_storage.assert_not_called()

    def test_blob_missing_token_and_provider_failure_fail_closed_without_filesystem_fallback(self):
        with install_fake_blob(), patch.dict(os.environ, {"APP_ENV": "production", "ATTACHMENT_STORAGE_BACKEND": "vercel_blob"}, clear=False), patch.object(attachment_storage, "resolve_temp_storage_dir") as local_storage:
            with self.assertRaisesRegex(RuntimeError, "falta su credencial"):
                attachment_storage.save_attachment(filename="a.txt", payload=b"x", company_id=1)
            local_storage.assert_not_called()
            storage_result = storage.check_storage_readiness(force=True)
            self.assertFalse(storage_result["ok"])
            self.assertEqual(storage_result["error_type"], "missing_configuration")

    def test_blob_probe_write_read_verify_delete_is_bounded_and_secret_free(self):
        with install_fake_blob(), patch.dict(os.environ, {
            "APP_ENV": "production",
            "ATTACHMENT_STORAGE_BACKEND": "vercel_blob",
            "BLOB_READ_WRITE_TOKEN": "never-display-this-token",
            "BLOB_STORE_HOST": "store-id.private.blob.vercel-storage.com",
        }, clear=False):
            first = storage.check_storage_readiness(force=True)
            second = storage.check_storage_readiness()
            self.assertTrue(first["ok"])
            self.assertEqual(first["probe"], "write_read_delete")
            self.assertEqual(first, second)
            self.assertEqual([name for name, _ in FakeBlobClient.instances[0].calls], ["put", "get", "delete"])
            self.assertTrue(all(call[1]["token"] == "never-display-this-token" for call in FakeBlobClient.instances[0].calls))
            self.assertNotIn("never-display-this-token", str(first))
            self.assertEqual(FakeBlobClient.instances[0].objects, {})

    def test_blob_provider_unavailable_and_delete_failure_are_not_ready(self):
        with install_fake_blob(), patch.dict(os.environ, {
            "APP_ENV": "production",
            "ATTACHMENT_STORAGE_BACKEND": "vercel_blob",
            "BLOB_READ_WRITE_TOKEN": "synthetic-token",
            "BLOB_STORE_HOST": "store-id.private.blob.vercel-storage.com",
        }, clear=False), patch.object(storage, "resolve_temp_storage_dir") as local_storage:
            with patch.object(FakeBlobClient, "get", return_value=None):
                result = storage.check_storage_readiness(force=True)
            self.assertFalse(result["ok"])
            self.assertNotIn("error", result)
            with patch.object(FakeBlobClient, "delete", side_effect=OSError("private-token")):
                result = storage.check_storage_readiness(force=True)
            self.assertFalse(result["ok"])
            self.assertNotIn("private-token", str(result))
            local_storage.assert_not_called()

    def test_blob_objects_returned_from_wrong_store_are_cleaned_up(self):
        with install_fake_blob(), patch.dict(os.environ, {
            "APP_ENV": "production",
            "ATTACHMENT_STORAGE_BACKEND": "vercel_blob",
            "BLOB_READ_WRITE_TOKEN": "synthetic-token",
            "BLOB_STORE_HOST": "store-id.private.blob.vercel-storage.com",
        }, clear=False):
            wrong_store = "other-store.private.blob.vercel-storage.com"
            with patch.object(
                FakeBlobClient,
                "put",
                autospec=True,
                side_effect=lambda client, path, payload, **kwargs: (
                    client.calls.append(("put", kwargs))
                    or client.objects.__setitem__(path, payload)
                    or types.SimpleNamespace(url=f"https://{wrong_store}/{path}")
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "fuera del store"):
                    attachment_storage.save_attachment(filename="orphan.pdf", payload=b"synthetic", company_id=42)
                self.assertEqual(FakeBlobClient.instances[-1].objects, {})

            with patch.object(
                FakeBlobClient,
                "put",
                autospec=True,
                side_effect=lambda client, path, payload, **kwargs: (
                    client.calls.append(("put", kwargs))
                    or client.objects.__setitem__(path, payload)
                    or types.SimpleNamespace(url=f"https://{wrong_store}/{path}")
                ),
            ):
                result = storage.check_storage_readiness(force=True)
            self.assertFalse(result["ok"])
            self.assertEqual(FakeBlobClient.instances[-1].objects, {})

    def test_generic_rest_real_builder_uses_shared_secure_transport_and_exact_host(self):
        config = {
            "tenant_id": 9,
            "connection_id": 12,
            "base_url": "https://sandbox.example/api",
            "secret_provider_ref": "secret://tenant-9/connection-12/api_key",
            "real_http_enabled": True,
            "_connection_enabled": True,
            "auth": {"type": "bearer"},
            "operations": {
                "orders.create": {
                    "method": "POST",
                    "path": "/orders",
                    "request_mapping": {"reference": "order.idempotency_key"},
                    "response_mapping": {"external_order_id": "order.id"},
                    "idempotency": {"location": "header", "name": "Idempotency-Key"},
                }
            },
        }
        secure = Mock()
        with patch("app.connectors.generic_rest.EgressPolicy") as egress, patch("app.connectors.generic_rest.SecureHTTPTransport", return_value=secure) as transport_factory:
            connector = build_generic_rest_real_connector(config)
        self.assertIsInstance(connector.client.transport, HTTPGenericRESTTransport)
        self.assertIs(connector.client.transport.secure_transport, secure)
        secure_config = transport_factory.call_args.args[0]
        self.assertEqual(secure_config.allowed_hosts, ("sandbox.example",))
        self.assertEqual(secure_config.tenant_id, 9)
        self.assertEqual(secure_config.connection_id, 12)
        self.assertEqual(egress.call_args.kwargs["allowed_hosts"], ("sandbox.example",))

    def test_cron_invoker_is_one_shot_and_never_prints_secret(self):
        secret = "cron-secret-not-for-output"
        response = FakeHTTPResponse()
        calls = []

        def opener(request, *, timeout):
            calls.append((request, timeout))
            return response

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = invoke(endpoint_url="https://anchi-web.onrender.com/cron/jobs", allowed_host="anchi-web.onrender.com", secret=secret, timeout_seconds=12, opener=opener)
        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 12)
        self.assertEqual(calls[0][0].get_method(), "POST")
        self.assertEqual(calls[0][0].get_header("Authorization"), f"Bearer {secret}")
        self.assertNotIn(secret, stdout.getvalue())

    def test_cron_invoker_rejects_bad_url_and_returns_nonzero_on_network_error(self):
        self.assertNotEqual(invoke(endpoint_url="http://pilot.example/cron/jobs", allowed_host="pilot.example", secret="x"), 0)
        untrusted_opener = Mock()
        self.assertNotEqual(invoke(endpoint_url="https://attacker.example/cron/jobs", allowed_host="attacker.example", secret="private", opener=untrusted_opener), 0)
        untrusted_opener.assert_not_called()
        stdout = io.StringIO()
        with contextlib.redirect_stderr(stdout):
            result = invoke(endpoint_url="https://anchi-web.onrender.com/cron/jobs", allowed_host="anchi-web.onrender.com", secret="private", opener=Mock(side_effect=OSError("secret leaked")))
            invalid_secret_result = invoke(endpoint_url="https://anchi-web.onrender.com/cron/jobs", allowed_host="anchi-web.onrender.com", secret="bad\r\nAuthorization: leaked")
        self.assertNotEqual(result, 0)
        self.assertNotEqual(invalid_secret_result, 0)
        self.assertNotIn("private", stdout.getvalue())
        self.assertNotIn("secret leaked", stdout.getvalue())

    def test_catalog_only_job_profile_excludes_email_and_media_jobs(self):
        with patch.dict(os.environ, {"PILOT_PROFILE": "CATALOG_ONLY_PILOT", "PILOT_SCHEDULED_JOB_PROFILE": "CATALOG_ONLY_PILOT"}, clear=True):
            allowed = scheduled_job_types()
        self.assertEqual(allowed, set(CATALOG_ONLY_JOB_TYPES))
        self.assertFalse({"email_sync", "process_recent_emails", "download_whatsapp_media", "process_email"} & allowed)
        self.assertTrue({"process_inbound_message", "deliver_order"}.issubset(allowed))
        self.assertNotIn("process_order", allowed)
        with patch.dict(os.environ, {"APP_ENV": "production", "PILOT_PROFILE": "CATALOG_ONLY_PILOT", "PILOT_SCHEDULED_JOB_PROFILE": ""}, clear=True):
            self.assertEqual(scheduled_job_types(), set())
        with patch.dict(os.environ, {"APP_ENV": "production", "PILOT_PROFILE": "", "PILOT_SCHEDULED_JOB_PROFILE": "", "PILOT_ENABLED_MODULES": "whatsapp_catalog,generic_rest"}, clear=True):
            self.assertEqual(scheduled_job_types(), set())
        with patch.dict(os.environ, {"APP_ENV": "production", "PILOT_PROFILE": "", "PILOT_SCHEDULED_JOB_PROFILE": ""}, clear=True):
            self.assertEqual(scheduled_job_types(), set())
        with patch.dict(os.environ, {"APP_ENV": "test", "PILOT_PROFILE": "", "PILOT_SCHEDULED_JOB_PROFILE": ""}, clear=True):
            self.assertEqual(scheduled_job_types(), set(JOB_TYPES))

    def test_catalog_only_media_event_is_reviewable_and_not_enqueued(self):
        from app.whatsapp.routes import _handle_media_download_for_profile

        message = types.SimpleNamespace(id=5, status="received", processing_step="received", processing_error=None)
        with patch.dict(os.environ, {"APP_ENV": "production", "PILOT_PROFILE": "CATALOG_ONLY_PILOT", "PILOT_SCHEDULED_JOB_PROFILE": "CATALOG_ONLY_PILOT"}, clear=True), patch(
            "app.whatsapp.routes.enqueue_whatsapp_media_download"
        ) as enqueue:
            self.assertFalse(_handle_media_download_for_profile(Mock(), 7, message))
        enqueue.assert_not_called()
        self.assertEqual(message.status, "doubtful")
        self.assertEqual(message.processing_step, "catalog_media_excluded")

    def test_cron_returns_failure_and_records_unhealthy_heartbeat_when_jobs_are_blocked(self):
        from types import SimpleNamespace
        from app.cron.routes import jobs_cron

        request = Mock()
        request.headers = {"authorization": "Bearer cron-secret"}
        with patch("app.cron.routes.get_settings", return_value=SimpleNamespace(cron_secret="cron-secret", cron_job_batch_size=5)), patch(
            "app.cron.routes.run_worker_cycle", return_value={"processed": 0, "blocked": 1}
        ), patch("app.cron.routes._record_scheduler_event") as heartbeat:
            result = jobs_cron(request)
        self.assertEqual(result.status_code, 503)
        self.assertFalse(heartbeat.call_args.kwargs["success"])

    def test_latest_failed_or_blocked_connection_test_is_not_reported_as_success(self):
        for outcome, expected in (("failed", "FAILED"), ("blocked", "BLOCKED")):
            log = types.SimpleNamespace(message=encode_structured_message(
                "test result", metadata={"connection_id": 9, "outcome": outcome}
            ))
            db = Mock()
            db.scalars.return_value.all.return_value = [log]
            service = IntegrationReadinessService(db, company_id=42)
            status = service.connection_test_status(types.SimpleNamespace(
                id=9, health_checked_at=None, health_status="UNKNOWN", health_message=None,
                connector_key="generic_rest",
            ))
            self.assertEqual(status["status"], expected)

    def test_catalog_only_does_not_start_provisioning_or_allow_excluded_cron_routes(self):
        from fastapi import HTTPException
        from app.cron.routes import _reject_excluded_pilot_cron

        master_db = Mock()
        master_db.scalars.return_value.all.return_value = []
        with patch.dict(os.environ, {"PILOT_SCHEDULED_JOB_PROFILE": "CATALOG_ONLY_PILOT", "PILOT_PROFILE": "CATALOG_ONLY_PILOT"}, clear=False), patch("app.workers.jobs_worker.MasterSessionLocal", return_value=master_db), patch("app.workers.jobs_worker.record_heartbeat"), patch("app.workers.provisioning_worker.run_provisioning_cycle") as provisioning:
            jobs_worker.run_worker_cycle(include_provisioning=True)
            provisioning.assert_not_called()
        with patch.dict(os.environ, {"PILOT_PROFILE": "CATALOG_ONLY_PILOT"}, clear=False):
            with self.assertRaises(HTTPException) as error:
                _reject_excluded_pilot_cron()
        self.assertEqual(error.exception.status_code, 404)
        with patch.dict(os.environ, {"APP_ENV": "staging", "PILOT_PROFILE": "CATALOG_ONLY_PILOT_TYPO", "PILOT_SCHEDULED_JOB_PROFILE": ""}, clear=True), patch("app.workers.jobs_worker.MasterSessionLocal", return_value=master_db), patch("app.workers.jobs_worker.record_heartbeat"), patch("app.workers.provisioning_worker.run_provisioning_cycle") as provisioning:
            jobs_worker.run_worker_cycle(include_provisioning=True)
            provisioning.assert_not_called()

    def test_worker_shutdown_finishes_cycle_then_exits_cleanly(self):
        stop_event = threading.Event()
        calls = []

        def one_cycle(**kwargs):
            calls.append(kwargs)
            stop_event.set()
            return {"processed": 1}

        with patch("app.workers.jobs_worker.get_settings", return_value=types.SimpleNamespace(job_worker_poll_seconds=600)), patch(
            "app.workers.jobs_worker.validate_runtime_schema", return_value={"master": "current", "tenants": {}}
        ), patch("app.workers.jobs_worker.run_worker_cycle", side_effect=one_cycle):
            jobs_worker._worker_loop(stop_event)
        self.assertEqual(len(calls), 1)
        self.assertTrue(stop_event.is_set())

    def test_catalog_readiness_is_non_2xx_while_liveness_stays_up(self):
        from fastapi import Response
        from app.health import routes

        request = types.SimpleNamespace(state=types.SimpleNamespace(tenant=None, request_id="req", correlation_id="corr"))
        with patch.dict(os.environ, {"APP_ENV": "production", "PILOT_PROFILE": "CATALOG_ONLY_PILOT", "ATTACHMENT_STORAGE_BACKEND": "vercel_blob", "PILOT_CONFIGURATION_BASELINE_JSON": "invalid", "CRON_SECRET": "cron-do-not-return", "BLOB_READ_WRITE_TOKEN": "blob-do-not-return"}, clear=False), patch("app.health.routes.master_migration_report", return_value={"status": "current", "current_version": "1"}), patch("app.health.routes.check_storage_readiness", return_value={"ok": False, "backend": "vercel_blob"}), patch("app.health.routes._ping_db", return_value=True), patch("app.health.routes.operational_status", return_value={"ok": False}):
            response = Response()
            master_db = Mock()
            master_db.scalars.return_value.all.return_value = []
            ready = routes.health_ready(request, master_db, response)
            live = routes.health_live(request)
        self.assertFalse(ready["ok"])
        self.assertEqual(response.status_code, 503)
        self.assertTrue(live["ok"])
        self.assertTrue(ready["pilot_configuration"]["runtime_gates"]["cron_secret_configured"])
        self.assertTrue(ready["pilot_configuration"]["runtime_gates"]["blob_token_configured"])
        self.assertNotIn("cron-do-not-return", str(ready))
        self.assertNotIn("blob-do-not-return", str(ready))


if __name__ == "__main__":
    unittest.main()
