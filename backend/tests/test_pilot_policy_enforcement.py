from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import HTTPException
from app.auth.dependencies import require_superadmin
from app.core.permissions import DEFAULT_ROLE_PERMISSIONS, has_permission, permission_for_request
from app.core.retention import RetentionPolicyService
from app.master.service import TenantRole, TenantUser
from app.orders.service import validate_confirmation


NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def tenant_user(role: str) -> TenantUser:
    return TenantUser(1, "pilot@example.test", "Pilot", True, 11, "Pilot", "pilot", TenantRole(role, DEFAULT_ROLE_PERMISSIONS.get(role, "")), 1)


class PilotPolicyEnforcementTests(unittest.TestCase):
    def test_role_permissions_follow_approved_pilot_matrix(self):
        operator, supervisor, administrator = (tenant_user(role) for role in ("Operador", "Supervisor", "Administrador"))
        platform = TenantUser(90, "platform@example.test", "Platform", True, None, "Platform", "platform", TenantRole("Superadmin", "manage_tenants,manage_users,view_technical_logs"), None, platform_role_key="superadmin")

        self.assertFalse(has_permission(operator, "configure_integrations"))
        self.assertFalse(has_permission(operator, permission_for_request("GET", "/integrations/connections")))
        self.assertTrue(has_permission(administrator, permission_for_request("GET", "/integrations/connections")))
        self.assertFalse(has_permission(operator, "manage_jobs"))
        self.assertFalse(has_permission(operator, "view_technical_logs"))
        self.assertTrue(has_permission(operator, "confirm_orders"))
        self.assertFalse(has_permission(operator, permission_for_request("POST", "/orders/deliveries/7/retry")))
        self.assertTrue(has_permission(supervisor, permission_for_request("POST", "/orders/deliveries/7/retry")))
        self.assertTrue(has_permission(supervisor, "view_logs"))
        self.assertFalse(has_permission(supervisor, "configure_integrations"))
        self.assertTrue(has_permission(administrator, "view_logs"))
        self.assertFalse(has_permission(administrator, "view_technical_logs"))
        self.assertFalse(has_permission(administrator, "manage_tenants"))
        self.assertTrue(has_permission(platform, "view_technical_logs"))
        self.assertTrue(has_permission(platform, "manage_tenants"))
        self.assertFalse(has_permission(platform, "manage_orders"))
        self.assertIs(require_superadmin(platform), platform)
        with self.assertRaises(HTTPException):
            require_superadmin(administrator)

    def test_confirm_requires_review_status_and_all_domain_guards(self):
        scoring = SimpleNamespace(
            block_without_customer=True,
            block_without_reference=True,
            block_without_quantity=True,
            block_below_threshold=True,
            doubtful_threshold=40,
        )
        good_line = SimpleNamespace(validation_status="validated", validated_product_id=2, doubt_reason=None, quantity=1)
        valid = SimpleNamespace(status="pedido_validado", validated_customer_id=3, lines=[good_line], score=90, source_mode="structured")
        self.assertEqual(validate_confirmation(valid, scoring), [])
        for changes, expected in (
            ({"status": "pending_review"}, "superar la revisión"),
            ({"validated_customer_id": None}, "cliente validado"),
            ({"lines": [SimpleNamespace(validation_status="pending", validated_product_id=None, doubt_reason="unresolved", quantity=1)]}, "lineas sin referencia"),
            ({"lines": [SimpleNamespace(validation_status="validated", validated_product_id=2, doubt_reason=None, quantity=0)]}, "cantidad valida"),
            ({"score": None, "source_mode": "catalog"}, "Confianza baja"),
        ):
            candidate = SimpleNamespace(**{**vars(valid), **changes})
            self.assertTrue(any(expected.lower() in error.lower() for error in validate_confirmation(candidate, scoring)))
        permissive_settings = SimpleNamespace(**{**vars(scoring), "block_without_customer": False, "block_without_reference": False, "block_without_quantity": False})
        invalid_line_order = SimpleNamespace(**{**vars(valid), "lines": [SimpleNamespace(validation_status="pending", validated_product_id=None, doubt_reason="unresolved", quantity=0)]})
        self.assertTrue(validate_confirmation(invalid_line_order, permissive_settings))

    def test_retention_is_tenant_scoped_legal_hold_safe_and_dry_run_only_for_business_data(self):
        service = RetentionPolicyService(tenant_id=11, now=NOW)
        old = NOW - timedelta(days=400)
        audit = {"tenant_id": 11, "category": "audit", "record_id": "a-1", "created_at": old}
        self.assertEqual(service.evaluate(audit).status, "eligible_for_deletion")
        self.assertEqual(service.evaluate({**audit, "legal_hold": True}).status, "legal_hold")
        with self.assertRaises(ValueError):
            service.evaluate({**audit, "tenant_id": 12})
        deleted, events = [], []
        results = service.apply([audit], cleanup=deleted.append, audit=events.append)
        self.assertEqual(results[0]["status"], "blocked")
        self.assertEqual(deleted, [])
        self.assertEqual(events[0]["tenant_id"], 11)

    def test_retention_only_applies_expired_temporary_records_and_blocks_backup_or_unrevoked_secret(self):
        service = RetentionPolicyService(tenant_id=11, now=NOW)
        temp = {"tenant_id": 11, "category": "previews", "record_id": "p-1", "created_at": NOW - timedelta(hours=80), "retention_hours": 72}
        backup = {"tenant_id": 11, "category": "backups", "record_id": "b-1"}
        secret = {"tenant_id": 11, "category": "secret_references", "record_id": "s-1", "revocation_confirmed": False}
        deleted, events = [], []
        results = service.apply([temp, backup, secret], cleanup=deleted.append, audit=events.append)
        self.assertEqual([item["status"] for item in results], ["deleted", "blocked", "blocked"])
        self.assertEqual(deleted, [temp])
        self.assertEqual(events[0]["event"], "retention.apply.started")
        self.assertEqual(events[-1]["event"], "retention.apply")

    def test_expiry_anchors_match_policy_for_offboarding_whatsapp_delivery_and_audit(self):
        service = RetentionPolicyService(tenant_id=11, now=NOW)
        offboarded = NOW - timedelta(days=89)
        order = service.evaluate({"tenant_id": 11, "category": "orders", "record_id": "o-2", "tenant_active": False, "offboarded_at": offboarded})
        self.assertEqual(order.status, "retained")
        self.assertEqual(order.eligible_at, offboarded + timedelta(days=90))
        message = service.evaluate({"tenant_id": 11, "category": "source_messages", "record_id": "m-1", "processing_succeeded": True, "processed_at": NOW - timedelta(days=90)})
        delivery = service.evaluate({"tenant_id": 11, "category": "deliveries", "record_id": "d-1", "created_at": NOW - timedelta(days=365)})
        audit = service.evaluate({"tenant_id": 11, "category": "audit", "record_id": "a-2", "created_at": NOW - timedelta(days=365)})
        self.assertEqual(message.status, "eligible_for_deletion")
        self.assertEqual(delivery.status, "eligible_for_deletion")
        self.assertEqual(audit.status, "eligible_for_deletion")

    def test_attachment_and_temporary_retention_bounds_are_enforced(self):
        service = RetentionPolicyService(tenant_id=11, now=NOW)
        attachment = {"tenant_id": 11, "category": "attachments", "record_id": "att-1", "processed_at": NOW - timedelta(days=29), "retention_days": 30}
        self.assertEqual(service.evaluate(attachment).status, "retained")
        self.assertEqual(service.evaluate({**attachment, "processed_at": NOW - timedelta(days=30)}).status, "eligible_for_deletion")
        self.assertEqual(service.evaluate({**attachment, "retention_days": 91}).status, "blocked")
        temp = {"tenant_id": 11, "category": "temporary", "record_id": "tmp-1", "created_at": NOW, "retention_hours": 23}
        self.assertEqual(service.evaluate(temp).status, "blocked")

    def test_business_retention_requires_explicit_gate_and_category_allowlist(self):
        service = RetentionPolicyService(tenant_id=11, now=NOW, business_deletion_enabled=True)
        order = {"tenant_id": 11, "category": "orders", "record_id": "o-1", "tenant_active": False, "offboarded_at": NOW - timedelta(days=100)}
        deleted, events = [], []
        result = service.apply([order], cleanup=deleted.append, audit=events.append, permitted_business_categories={"orders"})
        self.assertEqual(result[0]["status"], "deleted")
        self.assertEqual(deleted, [order])

    def test_audit_failure_prevents_cleanup(self):
        service = RetentionPolicyService(tenant_id=11, now=NOW)
        temp = {"tenant_id": 11, "category": "temporary", "record_id": "t-1", "created_at": NOW - timedelta(hours=30), "retention_hours": 24}
        deleted = []
        with self.assertRaises(RuntimeError):
            service.apply([temp], cleanup=deleted.append, audit=lambda _: (_ for _ in ()).throw(RuntimeError("audit unavailable")))
        self.assertEqual(deleted, [])


if __name__ == "__main__":
    unittest.main()
