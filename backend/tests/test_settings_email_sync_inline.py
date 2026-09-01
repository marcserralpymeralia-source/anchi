from __future__ import annotations

import json
import os
import unittest
import sys
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")
os.environ.setdefault("PERFORMANCE_PROFILING_ENABLED", "true")
os.environ.setdefault("ENABLE_PERFORMANCE_PROFILING", "true")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.encryption import encrypt_secret  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db.models import BackgroundJob, Company, Email, EmailSettings  # noqa: E402
from app.jobs.service import enqueue_job  # noqa: E402
from app.master.models import EmailSyncState  # noqa: E402
from scripts.performance_data import build_performance_fixture, performance_test_client  # noqa: E402


PERFORMANCE_ENCRYPTION_KEY = "CKHCB4gFGn7kJVxowWH2pEdPucfPaZugSsMgoJU6eNE="


class FakeImapClient:
    def __init__(self, messages: dict[str, bytes]) -> None:
        self.messages = messages
        self.uid_calls: list[tuple] = []
        self.login_calls: list[tuple] = []

    def login(self, username, password):  # noqa: ANN001
        self.login_calls.append((username, password))
        return "OK", [b"logged in"]

    def select(self, *_args, **_kwargs):
        return "OK", [b"1"]

    def status(self, mailbox: str, *_args, **_kwargs):
        return "OK", [f"{mailbox} (UIDVALIDITY 777)".encode()]

    def uid(self, command, *args, **_kwargs):  # noqa: ANN001
        self.uid_calls.append((command, *args))
        if command == "search":
            return "OK", [b"140"]
        if command == "fetch":
            uid = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
            raw = self.messages[uid]
            meta = f"{uid} (UID {uid} RFC822 {{123}})".encode()
            return "OK", [(meta, raw)]
        if command == "store":
            return "OK", [b"stored"]
        return "OK", [b""]

    def logout(self):
        return "BYE", [b"logout"]


class SettingsEmailSyncInlineHttpTests(unittest.TestCase):
    def _make_backfill_inline_side_effect(self, outcomes: list[dict]):
        state = {"index": 0}

        def _side_effect(active_db, job):  # noqa: ANN001
            if state["index"] >= len(outcomes):
                raise AssertionError("No se esperaba una ejecución adicional del backfill inline.")
            outcome = outcomes[state["index"]]
            state["index"] += 1
            result = dict(outcome.get("result") or {})
            if outcome.get("continuation_payload") is not None:
                continuation = enqueue_job(
                    active_db,
                    company_id=job.company_id,
                    job_type="backfill_imap",
                    payload=outcome["continuation_payload"],
                    created_by_user_id=job.created_by_user_id,
                )
                result["continuation_job_id"] = continuation.id
            job.status = "success" if result.get("ok", True) else "failed"
            job.started_at = datetime.now(timezone.utc)
            job.finished_at = datetime.now(timezone.utc)
            job.result_json = json.dumps(result, ensure_ascii=False)
            active_db.commit()
            return result

        return _side_effect

    def test_manual_sync_runs_inline_and_imports_new_mail(self):
        fixture = build_performance_fixture("small")
        master_engine = create_engine(f"sqlite:///{fixture.master_path.as_posix()}", connect_args={"check_same_thread": False})
        tenant_engine = create_engine(f"sqlite:///{fixture.tenant_path.as_posix()}", connect_args={"check_same_thread": False})
        MasterSession = sessionmaker(bind=master_engine, autoflush=False, autocommit=False)
        TenantSession = sessionmaker(bind=tenant_engine, autoflush=False, autocommit=False)
        raw_message = (
            b"From: pedidos@example.com\r\n"
            b"To: demo@example.com\r\n"
            b"Subject: PRUEBA ANCHI 001\r\n"
            b"Message-ID: <prueba-anchi-001@example.com>\r\n"
            b"Date: Mon, 27 Jul 2026 10:00:00 +0000\r\n"
            b"\r\n"
            b"Pedido de prueba 001"
        )
        fake_client = FakeImapClient({"140": raw_message})
        try:
            with MasterSession() as master_db:
                state = master_db.scalar(select(EmailSyncState).where(EmailSyncState.company_id == fixture.company_id, EmailSyncState.channel_key == "email"))
                if not state:
                    state = EmailSyncState(company_id=fixture.company_id, channel_key="email", enabled=True, frequency_seconds=60, status="idle")
                    master_db.add(state)
                state.last_seen_uid = "139"
                state.uidvalidity = "777"
                master_db.commit()

            with TenantSession() as tenant_db:
                company = tenant_db.scalar(select(Company).where(Company.id == fixture.company_id))
                if company:
                    company.timezone = "Europe/Madrid"
                settings = tenant_db.scalar(select(EmailSettings).where(EmailSettings.company_id == fixture.company_id))
                assert settings is not None
                settings.provider = "gmail"
                settings.imap_host = "imap.gmail.com"
                settings.imap_port = 993
                settings.imap_security = "ssl_tls"
                settings.imap_use_ssl = True
                settings.imap_username = "demo.user@example.com"
                with patch.dict(os.environ, {"ENCRYPTION_KEY": PERFORMANCE_ENCRYPTION_KEY}, clear=False):
                    get_settings.cache_clear()
                    settings.imap_password_encrypted = encrypt_secret("DemoAppPassword123!")
                settings.inbox_folder = "INBOX"
                settings.auto_sync_enabled = False
                settings.auto_process_on_fetch = False
                settings.read_unread_only = False
                settings.initial_history_mode = "new"
                settings.initial_history_limit = 20
                tenant_db.commit()

            with patch("app.settings.integrations._imap_client", return_value=fake_client):
                with performance_test_client(fixture) as client:
                    response = client.post("/settings/email/read-unprocessed", follow_redirects=True)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn("internal_error", response.text.lower())
                    inbox = client.get("/mail", follow_redirects=True)
                    self.assertEqual(inbox.status_code, 200)
                    self.assertIn("Bandeja", inbox.text)

            with TenantSession() as tenant_db:
                saved_email = tenant_db.scalar(select(Email).where(Email.company_id == fixture.company_id, Email.subject == "PRUEBA ANCHI 001"))
                self.assertIsNotNone(saved_email)
                settings = tenant_db.scalar(select(EmailSettings).where(EmailSettings.company_id == fixture.company_id))
                self.assertIsNotNone(settings)
                assert settings is not None
                self.assertIsNotNone(settings.last_sync_at)
                self.assertGreaterEqual(settings.last_sync_new or 0, 1)

                job = tenant_db.scalar(select(BackgroundJob).where(BackgroundJob.company_id == fixture.company_id, BackgroundJob.job_type == "email_sync").order_by(BackgroundJob.id.desc()))
                self.assertIsNotNone(job)
                assert job is not None
                self.assertEqual(job.status, "success")
                result = json.loads(job.result_json or "{}")
                self.assertEqual(result.get("saved"), 1)
                self.assertEqual(result.get("found"), 1)

            with MasterSession() as master_db:
                state = master_db.scalar(select(EmailSyncState).where(EmailSyncState.company_id == fixture.company_id, EmailSyncState.channel_key == "email"))
                self.assertIsNotNone(state)
                assert state is not None
                self.assertEqual(state.last_seen_uid, "140")

            self.assertTrue(fake_client.uid_calls)
            self.assertEqual(fake_client.uid_calls[0][0], "search")
            self.assertIn("140:*", fake_client.uid_calls[0])
        finally:
            master_engine.dispose()
            tenant_engine.dispose()
            fixture.cleanup()

    def test_manual_backfill_runs_inline_and_returns_continuation(self):
        fixture = build_performance_fixture("small")
        master_engine = create_engine(f"sqlite:///{fixture.master_path.as_posix()}", connect_args={"check_same_thread": False})
        tenant_engine = create_engine(f"sqlite:///{fixture.tenant_path.as_posix()}", connect_args={"check_same_thread": False})
        MasterSession = sessionmaker(bind=master_engine, autoflush=False, autocommit=False)
        TenantSession = sessionmaker(bind=tenant_engine, autoflush=False, autocommit=False)
        try:
            from app.master.models import CompanyMembership

            with MasterSession() as master_db:
                membership = master_db.scalar(select(CompanyMembership).where(CompanyMembership.company_id == fixture.company_id))
                assert membership is not None
                membership.role_key = "Administrador"
                master_db.commit()

            outcome = {
                "ok": True,
                "found": 5,
                "saved": 2,
                "duplicates": 1,
                "errors": 0,
                "downloaded": 5,
                "discarded": 0,
                "batch_count": 5,
                "has_more": True,
                "remaining": 4,
                "last_uid": "105",
                "message": "Lote inicial",
                "continuation_job_id": 9999,
            }

            with performance_test_client(fixture) as client:
                with patch("app.settings.routes.execute_job_inline", side_effect=self._make_backfill_inline_side_effect([{"result": outcome}])) as inline_mock:
                    response = client.post(
                        "/settings/email/backfill",
                        data={"from_date": "2026-08-20", "to_date": "2026-08-25", "limit": "5"},
                        headers={"Accept": "application/json"},
                        follow_redirects=False,
                    )

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "success")
            self.assertIsNotNone(payload["job_id"])
            self.assertEqual(payload["continuation_job_id"], 9999)
            self.assertTrue(payload["has_more"])
            self.assertEqual(payload["remaining"], 4)
            self.assertEqual(payload["found"], 5)
            self.assertEqual(payload["saved"], 2)
            self.assertEqual(payload["duplicates"], 1)
            self.assertEqual(payload["errors"], 0)
            inline_mock.assert_called_once()

            with TenantSession() as tenant_db:
                jobs = tenant_db.scalars(
                    select(BackgroundJob)
                    .where(BackgroundJob.company_id == fixture.company_id, BackgroundJob.job_type == "backfill_imap")
                    .order_by(BackgroundJob.id)
                ).all()
                self.assertEqual(len(jobs), 1)
                self.assertEqual(jobs[0].status, "success")
        finally:
            master_engine.dispose()
            tenant_engine.dispose()
            fixture.cleanup()

    def test_manual_backfill_continue_executes_single_job(self):
        fixture = build_performance_fixture("small")
        master_engine = create_engine(f"sqlite:///{fixture.master_path.as_posix()}", connect_args={"check_same_thread": False})
        tenant_engine = create_engine(f"sqlite:///{fixture.tenant_path.as_posix()}", connect_args={"check_same_thread": False})
        MasterSession = sessionmaker(bind=master_engine, autoflush=False, autocommit=False)
        TenantSession = sessionmaker(bind=tenant_engine, autoflush=False, autocommit=False)
        try:
            from app.master.models import CompanyMembership

            with MasterSession() as master_db:
                membership = master_db.scalar(select(CompanyMembership).where(CompanyMembership.company_id == fixture.company_id))
                assert membership is not None
                membership.role_key = "Administrador"
                master_db.commit()

            with TenantSession() as tenant_db:
                continuation = enqueue_job(
                    tenant_db,
                    company_id=fixture.company_id,
                    job_type="backfill_imap",
                    payload={"from_date": "2026-08-20", "to_date": "2026-08-25", "limit": 5, "resume": True},
                    created_by_user_id=1,
                )
                job_id = continuation.id
                tenant_db.commit()

            result = {
                "ok": True,
                "found": 4,
                "saved": 2,
                "duplicates": 1,
                "errors": 0,
                "downloaded": 4,
                "discarded": 0,
                "batch_count": 4,
                "has_more": False,
                "remaining": 0,
                "last_uid": "110",
                "message": "Lote de continuación",
            }

            with performance_test_client(fixture) as client:
                with patch("app.settings.routes.execute_job_inline", side_effect=self._make_backfill_inline_side_effect([{"result": result}])) as inline_mock:
                    response = client.post(
                        f"/settings/email/backfill/continue/{job_id}",
                        headers={"Accept": "application/json"},
                        follow_redirects=False,
                    )

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["job_id"], job_id)
            self.assertEqual(payload["found"], 4)
            self.assertEqual(payload["saved"], 2)
            self.assertEqual(payload["duplicates"], 1)
            self.assertEqual(payload["errors"], 0)
            self.assertFalse(payload["has_more"])
            self.assertEqual(payload["remaining"], 0)
            inline_mock.assert_called_once()
        finally:
            master_engine.dispose()
            tenant_engine.dispose()
            fixture.cleanup()

    def test_manual_backfill_continue_rejects_wrong_tenant(self):
        fixture = build_performance_fixture("small")
        master_engine = create_engine(f"sqlite:///{fixture.master_path.as_posix()}", connect_args={"check_same_thread": False})
        tenant_engine = create_engine(f"sqlite:///{fixture.tenant_path.as_posix()}", connect_args={"check_same_thread": False})
        MasterSession = sessionmaker(bind=master_engine, autoflush=False, autocommit=False)
        TenantSession = sessionmaker(bind=tenant_engine, autoflush=False, autocommit=False)
        try:
            from app.master.models import CompanyMembership

            with MasterSession() as master_db:
                membership = master_db.scalar(select(CompanyMembership).where(CompanyMembership.company_id == fixture.company_id))
                assert membership is not None
                membership.role_key = "Administrador"
                master_db.commit()

            with TenantSession() as tenant_db:
                job = enqueue_job(
                    tenant_db,
                    company_id=999,
                    job_type="backfill_imap",
                    payload={"from_date": "2026-08-20", "to_date": "2026-08-25", "limit": 5, "resume": True},
                    created_by_user_id=1,
                )
                job_id = job.id
                tenant_db.commit()

            with performance_test_client(fixture) as client:
                response = client.post(f"/settings/email/backfill/continue/{job_id}", headers={"Accept": "application/json"}, follow_redirects=False)

            self.assertEqual(response.status_code, 404)
            payload = response.json()
            self.assertEqual(payload["error_type"], "job_not_found")
        finally:
            master_engine.dispose()
            tenant_engine.dispose()
            fixture.cleanup()

    def test_manual_backfill_continue_rejects_wrong_job_type(self):
        fixture = build_performance_fixture("small")
        master_engine = create_engine(f"sqlite:///{fixture.master_path.as_posix()}", connect_args={"check_same_thread": False})
        tenant_engine = create_engine(f"sqlite:///{fixture.tenant_path.as_posix()}", connect_args={"check_same_thread": False})
        MasterSession = sessionmaker(bind=master_engine, autoflush=False, autocommit=False)
        TenantSession = sessionmaker(bind=tenant_engine, autoflush=False, autocommit=False)
        try:
            from app.master.models import CompanyMembership

            with MasterSession() as master_db:
                membership = master_db.scalar(select(CompanyMembership).where(CompanyMembership.company_id == fixture.company_id))
                assert membership is not None
                membership.role_key = "Administrador"
                master_db.commit()

            with TenantSession() as tenant_db:
                job = enqueue_job(
                    tenant_db,
                    company_id=fixture.company_id,
                    job_type="email_sync",
                    payload={"from_date": "2026-08-20", "to_date": "2026-08-25", "limit": 5, "resume": True},
                    created_by_user_id=1,
                )
                job_id = job.id
                tenant_db.commit()

            with performance_test_client(fixture) as client:
                response = client.post(f"/settings/email/backfill/continue/{job_id}", headers={"Accept": "application/json"}, follow_redirects=False)

            self.assertEqual(response.status_code, 400)
            payload = response.json()
            self.assertEqual(payload["error_type"], "invalid_job_type")
        finally:
            master_engine.dispose()
            tenant_engine.dispose()
            fixture.cleanup()

    def test_manual_backfill_continue_rejects_invalid_status(self):
        fixture = build_performance_fixture("small")
        master_engine = create_engine(f"sqlite:///{fixture.master_path.as_posix()}", connect_args={"check_same_thread": False})
        tenant_engine = create_engine(f"sqlite:///{fixture.tenant_path.as_posix()}", connect_args={"check_same_thread": False})
        MasterSession = sessionmaker(bind=master_engine, autoflush=False, autocommit=False)
        TenantSession = sessionmaker(bind=tenant_engine, autoflush=False, autocommit=False)
        try:
            from app.master.models import CompanyMembership

            with MasterSession() as master_db:
                membership = master_db.scalar(select(CompanyMembership).where(CompanyMembership.company_id == fixture.company_id))
                assert membership is not None
                membership.role_key = "Administrador"
                master_db.commit()

            with TenantSession() as tenant_db:
                job = enqueue_job(
                    tenant_db,
                    company_id=fixture.company_id,
                    job_type="backfill_imap",
                    payload={"from_date": "2026-08-20", "to_date": "2026-08-25", "limit": 5, "resume": True},
                    created_by_user_id=1,
                )
                job_id = job.id
                job.status = "success"
                tenant_db.commit()

            with performance_test_client(fixture) as client:
                response = client.post(f"/settings/email/backfill/continue/{job_id}", headers={"Accept": "application/json"}, follow_redirects=False)

            self.assertEqual(response.status_code, 409)
            payload = response.json()
            self.assertEqual(payload["error_type"], "invalid_job_status")
        finally:
            master_engine.dispose()
            tenant_engine.dispose()
            fixture.cleanup()

    def test_settings_page_shows_local_sync_time(self):
        fixture = build_performance_fixture("small")
        master_engine = create_engine(f"sqlite:///{fixture.master_path.as_posix()}", connect_args={"check_same_thread": False})
        tenant_engine = create_engine(f"sqlite:///{fixture.tenant_path.as_posix()}", connect_args={"check_same_thread": False})
        MasterSession = sessionmaker(bind=master_engine, autoflush=False, autocommit=False)
        TenantSession = sessionmaker(bind=tenant_engine, autoflush=False, autocommit=False)
        try:
            with TenantSession() as tenant_db:
                company = tenant_db.scalar(select(Company).where(Company.id == fixture.company_id))
                if company:
                    company.timezone = "Europe/Madrid"
                settings = tenant_db.scalar(select(EmailSettings).where(EmailSettings.company_id == fixture.company_id))
                assert settings is not None
                settings.last_sync_at = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
                settings.last_sync_message = "Sincronización demo"
                tenant_db.commit()

            with performance_test_client(fixture) as client:
                response = client.get("/settings")
                self.assertEqual(response.status_code, 200)
                self.assertIn("27/07 12:00", response.text)
                self.assertNotIn("27/07 10:00", response.text)
        finally:
            master_engine.dispose()
            tenant_engine.dispose()
            fixture.cleanup()


class SettingsEmailBackfillJsTests(unittest.TestCase):
    @staticmethod
    def _run_js(script: str) -> dict:
        js_path = Path(__file__).resolve().parents[1] / "app/static/js/settings-email-backfill.js"
        node_script = f"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync({json.dumps(str(js_path))}, "utf8");
const context = {{
  console,
  setTimeout,
  clearTimeout,
  FormData: class {{
    constructor(form) {{
      this.form = form;
    }}
  }},
  document: undefined,
}};
context.globalThis = context;
context.window = context;
context.global = context;
vm.createContext(context);
vm.runInContext(source, context);
(async () => {{
  const value = await vm.runInContext(`(async () => {{
{script}
}})()`, context);
  process.stdout.write(JSON.stringify(value));
}})().catch((error) => {{
  process.stderr.write(error && error.stack ? error.stack : String(error));
  process.exit(1);
}});
"""
        completed = subprocess.run(["node", "-e", node_script], capture_output=True, text=True, check=True)
        return json.loads(completed.stdout or "{}")

    def test_backfill_js_chains_three_batches(self):
        result = self._run_js(
            """
const api = globalThis.AnchiEmailBackfill;
const form = {
  action: "/settings/email/backfill",
  dataset: {},
  querySelector: (selector) => selector === "[name='limit']" ? { value: "10" } : null,
};
const nodes = {
  buttons: [{ disabled: false, textContent: "Traer", dataset: { loadingText: "Procesando..." } }],
  state: { textContent: "" },
  found: { textContent: "" },
  saved: { textContent: "" },
  duplicates: { textContent: "" },
  errors: { textContent: "" },
  batches: { textContent: "" },
  remaining: { textContent: "" },
  message: { textContent: "" },
};
const responses = [
  { ok: true, status: 200, json: async () => ({ ok: true, job_id: 11, continuation_job_id: 22, has_more: true, remaining: 7, found: 5, saved: 2, duplicates: 1, errors: 0, message: "Lote 1" }) },
  { ok: true, status: 200, json: async () => ({ ok: true, job_id: 22, continuation_job_id: 33, has_more: true, remaining: 2, found: 4, saved: 1, duplicates: 0, errors: 0, message: "Lote 2" }) },
  { ok: true, status: 200, json: async () => ({ ok: true, job_id: 33, has_more: false, remaining: 0, found: 2, saved: 1, duplicates: 0, errors: 0, message: "Lote 3" }) },
];
let index = 0;
const result = await api.runBackfillSequence(form, async () => responses[index++], nodes);
return { result, disabled: nodes.buttons[0].disabled, buttonText: nodes.buttons[0].textContent };
"""
        )
        self.assertTrue(result["result"]["ok"])
        self.assertEqual(result["result"]["status"], "success")
        self.assertEqual(result["result"]["batches"], 3)
        self.assertEqual(result["result"]["found"], 11)
        self.assertEqual(result["result"]["saved"], 4)
        self.assertEqual(result["result"]["duplicates"], 1)
        self.assertEqual(result["result"]["errors"], 0)
        self.assertEqual(result["result"]["remaining"], 0)
        self.assertEqual(result["result"]["requests"], [
            "/settings/email/backfill",
            "/settings/email/backfill/continue/22",
            "/settings/email/backfill/continue/33",
        ])
        self.assertFalse(result["disabled"])
        self.assertEqual(result["buttonText"], "Traer")

    def test_backfill_js_detects_repeated_job_id(self):
        result = self._run_js(
            """
const api = globalThis.AnchiEmailBackfill;
const state = api.createBackfillState(10);
const first = api.applyBackfillBatch(state, { ok: true, job_id: 11, continuation_job_id: 22, has_more: true, remaining: 7, found: 5, saved: 2, duplicates: 1, errors: 0, message: "Lote 1" });
const second = api.applyBackfillBatch(state, { ok: true, job_id: 22, continuation_job_id: 22, has_more: true, remaining: 5, found: 3, saved: 1, duplicates: 0, errors: 0, message: "Lote 2" });
return { first, second };
"""
        )
        self.assertTrue(result["first"]["continue"])
        self.assertFalse(result["second"]["ok"])
        self.assertEqual(result["second"]["error_type"], "backfill_job_repeated")

    def test_backfill_js_detects_no_progress(self):
        result = self._run_js(
            """
const api = globalThis.AnchiEmailBackfill;
const state = api.createBackfillState(10);
const first = api.applyBackfillBatch(state, { ok: true, job_id: 11, continuation_job_id: 22, has_more: true, remaining: 7, found: 5, saved: 2, duplicates: 1, errors: 0, message: "Lote 1" });
const second = api.applyBackfillBatch(state, { ok: true, job_id: 22, continuation_job_id: 33, has_more: true, remaining: 7, found: 3, saved: 1, duplicates: 0, errors: 0, message: "Lote 2" });
return { first, second };
"""
        )
        self.assertTrue(result["first"]["continue"])
        self.assertFalse(result["second"]["ok"])
        self.assertEqual(result["second"]["error_type"], "backfill_no_progress")

    def test_backfill_js_stops_on_error(self):
        result = self._run_js(
            """
const api = globalThis.AnchiEmailBackfill;
const form = {
  action: "/settings/email/backfill",
  dataset: {},
  querySelector: (selector) => selector === "[name='limit']" ? { value: "10" } : null,
};
const nodes = {
  buttons: [{ disabled: false, textContent: "Traer", dataset: { loadingText: "Procesando..." } }],
  state: { textContent: "" },
  found: { textContent: "" },
  saved: { textContent: "" },
  duplicates: { textContent: "" },
  errors: { textContent: "" },
  batches: { textContent: "" },
  remaining: { textContent: "" },
  message: { textContent: "" },
};
const responses = [
  { ok: true, status: 200, json: async () => ({ ok: true, job_id: 11, continuation_job_id: 22, has_more: true, remaining: 7, found: 5, saved: 2, duplicates: 1, errors: 0, message: "Lote 1" }) },
  { ok: true, status: 200, json: async () => ({ ok: false, job_id: 22, has_more: false, remaining: 7, found: 0, saved: 0, duplicates: 0, errors: 1, error_type: "timeout", message: "Error controlado" }) },
];
let index = 0;
const result = await api.runBackfillSequence(form, async () => responses[index++], nodes);
return { result };
"""
        )
        self.assertFalse(result["result"]["ok"])
        self.assertEqual(result["result"]["status"], "failed")
        self.assertEqual(result["result"]["error_type"], "timeout")
        self.assertEqual(result["result"]["batches"], 2)

    def test_backfill_js_prevents_double_click(self):
        result = self._run_js(
            """
const api = globalThis.AnchiEmailBackfill;
const form = {
  action: "/settings/email/backfill",
  dataset: { backfillRunning: "true" },
  querySelector: () => ({ value: "10" }),
};
const result = await api.runBackfillSequence(form, async () => ({ ok: true, status: 200, json: async () => ({ ok: true }) }), { buttons: [] });
return { result };
"""
        )
        self.assertFalse(result["result"]["ok"])
        self.assertEqual(result["result"]["error_type"], "already_running")


if __name__ == "__main__":
    unittest.main()
