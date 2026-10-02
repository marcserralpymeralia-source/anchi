"""Tenant-scoped, non-destructive pilot retention decisions.

The service plans deletion eligibility but never deletes business records. Only
temporary artifacts may be removed by an explicitly supplied cleanup callback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable


POLICY_VERSION = "catalog-only-retention-v1"
BUSINESS_CATEGORIES = frozenset({"orders", "replicated_data", "source_messages", "deliveries", "audit", "attachments", "backups", "secret_references"})
TEMP_CATEGORIES = frozenset({"temporary", "previews"})


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class RetentionDecision:
    tenant_id: int
    category: str
    record_id: str
    status: str
    eligible_at: datetime | None
    reason: str

    def to_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "category": self.category,
            "record_id": self.record_id,
            "status": self.status,
            "eligible_at": self.eligible_at.isoformat() if self.eligible_at else None,
            "reason": self.reason,
            "policy_version": POLICY_VERSION,
        }


class RetentionPolicyService:
    """Calculate tenant-bound retention status and safely apply technical cleanup."""

    def __init__(self, tenant_id: int, *, now: datetime | None = None, business_deletion_enabled: bool | None = None):
        if int(tenant_id) <= 0:
            raise ValueError("tenant_id must be a positive integer")
        self.tenant_id = int(tenant_id)
        self.now = _utc(now or datetime.now(timezone.utc))
        if business_deletion_enabled is None:
            business_deletion_enabled = os.getenv("PILOT_RETENTION_DESTRUCTIVE_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
        self.business_deletion_enabled = bool(business_deletion_enabled)

    def evaluate(self, record: dict) -> RetentionDecision:
        tenant_id = int(record.get("tenant_id", -1))
        if tenant_id != self.tenant_id:
            raise ValueError("record tenant does not match retention scope")
        category = str(record.get("category", ""))
        record_id = str(record.get("record_id", ""))
        if not category or not record_id:
            raise ValueError("category and record_id are required")
        if record.get("legal_hold"):
            return RetentionDecision(self.tenant_id, category, record_id, "legal_hold", None, "legal hold prevents deletion")
        if category == "secret_references":
            if not record.get("revocation_confirmed"):
                return RetentionDecision(self.tenant_id, category, record_id, "blocked", None, "external secret revocation is not confirmed")
            return RetentionDecision(self.tenant_id, category, record_id, "eligible_for_deletion", self.now, "revocation confirmed; manual reference review required")
        if category == "backups":
            return RetentionDecision(self.tenant_id, category, record_id, "blocked", None, "backup rotation is owned by the provider, not tenant record cleanup")

        if category in {"orders", "replicated_data"}:
            if record.get("tenant_active", True):
                return RetentionDecision(self.tenant_id, category, record_id, "active", None, "tenant remains active")
            anchor = record.get("offboarded_at")
            days = 90
        elif category == "source_messages":
            anchor = record.get("processed_at") if record.get("processing_succeeded") else None
            days = 90
            if anchor is None:
                return RetentionDecision(self.tenant_id, category, record_id, "blocked", None, "successful processing timestamp is required")
        elif category in {"deliveries", "attempts", "jobs", "errors", "audit", "correlation"}:
            anchor, days = record.get("created_at"), 365
        elif category == "attachments":
            anchor = record.get("processed_at")
            days = int(record.get("retention_days", 0))
            if not 30 <= days <= 90:
                return RetentionDecision(self.tenant_id, category, record_id, "blocked", None, "attachment retention must be configured between 30 and 90 days")
        elif category in TEMP_CATEGORIES:
            anchor = record.get("created_at")
            days = int(record.get("retention_hours", 0)) / 24
            if not 24 <= days * 24 <= 72:
                return RetentionDecision(self.tenant_id, category, record_id, "blocked", None, "temporary retention must be configured between 24 and 72 hours")
        else:
            return RetentionDecision(self.tenant_id, category, record_id, "blocked", None, "unknown retention category")

        if anchor is None:
            return RetentionDecision(self.tenant_id, category, record_id, "blocked", None, "retention anchor is required")
        eligible_at = _utc(anchor) + (timedelta(hours=record["retention_hours"]) if category in TEMP_CATEGORIES else timedelta(days=days))
        if self.now < eligible_at:
            return RetentionDecision(self.tenant_id, category, record_id, "retained", eligible_at, "retention period has not expired")
        return RetentionDecision(self.tenant_id, category, record_id, "eligible_for_deletion", eligible_at, "retention period has expired")

    def dry_run(self, records: Iterable[dict]) -> list[dict]:
        return [self.evaluate(record).to_dict() for record in records]

    def apply(
        self,
        records: Iterable[dict],
        *,
        cleanup: Callable[[dict], None],
        audit: Callable[[dict], None],
        permitted_business_categories: Iterable[str] = (),
    ) -> list[dict]:
        """Apply temporary cleanup; business deletion needs both a gate and category allowlist."""
        permitted = set(permitted_business_categories) & BUSINESS_CATEGORIES
        results = []
        for record in records:
            decision = self.evaluate(record)
            result = decision.to_dict()
            apply_category = decision.status == "eligible_for_deletion" and (
                decision.category in TEMP_CATEGORIES
                or (decision.category in permitted and self.business_deletion_enabled)
            )
            if apply_category:
                audit({"tenant_id": self.tenant_id, "event": "retention.apply.started", "category": decision.category, "record_id": decision.record_id, "status": "started", "policy_version": POLICY_VERSION})
                try:
                    cleanup(record)
                    result["status"] = "deleted"
                except Exception:
                    result["status"] = "blocked"
                    result["reason"] = "cleanup failed; record was not confirmed deleted"
                    audit({"tenant_id": self.tenant_id, "event": "retention.apply.failed", "category": decision.category, "record_id": decision.record_id, "status": result["status"], "policy_version": POLICY_VERSION})
                    results.append(result)
                    continue
            elif decision.status == "eligible_for_deletion" and decision.category in BUSINESS_CATEGORIES:
                result["status"] = "blocked"
                result["reason"] = "business deletion requires the explicit destructive gate and category allowlist"
            audit({"tenant_id": self.tenant_id, "event": "retention.apply", "category": decision.category, "record_id": decision.record_id, "status": result["status"], "policy_version": POLICY_VERSION})
            results.append(result)
        return results
