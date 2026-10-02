"""Deterministic, secret-free pilot configuration drift manifests."""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

from app.core.permissions import DEFAULT_ROLE_PERMISSIONS
from app.core.retention import POLICY_VERSION as RETENTION_POLICY_VERSION


MANIFEST_VERSION = "pilot-configuration-v1"
RBAC_POLICY_VERSION = "pilot-rbac-v1"
SECRET_KEY_PATTERN = re.compile(r"(secret|password|token|credential|private.?key|api.?key)", re.I)
CRITICAL_FIELDS = frozenset({
    "profile",
    "enabled_modules",
    "disabled_modules",
    "connectors",
    "connection_fingerprints",
    "routes",
    "master_roles",
    "runtime_gates",
    "retention_policy_version",
    "rbac_policy_version",
})


def _safe(value: Any, key: str = "") -> Any:
    if key.lower() not in {"secret_provider_kind", "secret_provider_ref_fingerprint"} and SECRET_KEY_PATTERN.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _safe(v, str(k)) for k, v in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple, set)):
        return sorted((_safe(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True, default=str))
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def safe_configuration_fingerprint(raw_config: str | dict | None) -> str:
    """Hash connector config only after recursive secret-key redaction."""
    try:
        parsed = json.loads(raw_config) if isinstance(raw_config, str) else (raw_config or {})
    except (TypeError, json.JSONDecodeError):
        parsed = {"invalid_config": True}
    canonical = json.dumps(_safe(parsed), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PilotConfigurationManifest:
    def __init__(self, *, tenant_id: int | None = None):
        self.tenant_id = tenant_id

    def build(
        self,
        *,
        commit_sha: str = "unknown",
        schema_version: str = "unknown",
        profile: str = "catalog-only",
        enabled_modules: list[str] | tuple[str, ...] = ("whatsapp_catalog", "generic_rest_mock"),
        disabled_modules: list[str] | tuple[str, ...] = ("email_autodiscovery", "audio", "sage_real", "odoo_real"),
        connectors: list[dict] | tuple[dict, ...] = (),
        connection_fingerprints: list[dict] | tuple[dict, ...] = (),
        routes: list[dict] | tuple[dict, ...] = (),
        runtime_gates: dict | None = None,
        retention_policy_version: str = RETENTION_POLICY_VERSION,
        rbac_policy_version: str = RBAC_POLICY_VERSION,
    ) -> dict:
        content = _safe({
            "manifest_version": MANIFEST_VERSION,
            "tenant_id": self.tenant_id,
            "commit_sha": commit_sha,
            "schema_version": schema_version,
            "profile": profile,
            "enabled_modules": list(enabled_modules),
            "disabled_modules": list(disabled_modules),
            "connectors": list(connectors),
            "connection_fingerprints": list(connection_fingerprints),
            "routes": list(routes),
            "master_roles": {
                role: sorted(permission for permission in permissions.split(",") if permission)
                for role, permissions in sorted(DEFAULT_ROLE_PERMISSIONS.items())
            } | {"Superadmin": ["manage_tenants", "manage_users", "view_technical_logs"]},
            "runtime_gates": runtime_gates or {},
            "retention_policy_version": retention_policy_version,
            "rbac_policy_version": rbac_policy_version,
        })
        encoded = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return {**content, "hash": hashlib.sha256(encoded.encode("utf-8")).hexdigest()}

    def compare(self, current: dict, approved: dict | None) -> dict:
        if not approved:
            return {"status": "NOT_CONFIGURED", "critical": [], "informational": [], "approved_hash": None, "runtime_hash": current.get("hash")}
        fields = sorted((set(current) | set(approved)) - {"hash"})
        diffs = []
        for field in fields:
            if current.get(field) != approved.get(field):
                diffs.append({"field": field, "severity": "critical" if field in CRITICAL_FIELDS else "informational", "approved": _safe(approved.get(field), field), "runtime": _safe(current.get(field), field)})
        return {
            "status": "DRIFT" if diffs else "MATCH",
            "critical": [item for item in diffs if item["severity"] == "critical"],
            "informational": [item for item in diffs if item["severity"] == "informational"],
            "approved_hash": approved.get("hash"),
            "runtime_hash": current.get("hash"),
        }

    @classmethod
    def from_runtime(cls, *, tenant_id: int | None = None) -> dict:
        from app.connectors.registry import IntegrationRegistry

        enabled = os.getenv("PILOT_ENABLED_MODULES", "whatsapp_catalog,generic_rest_mock").split(",")
        disabled = os.getenv("PILOT_DISABLED_MODULES", "email_autodiscovery,audio,sage_real,odoo_real").split(",")
        boolean_gates = ("PILOT_HTTP_CONNECTOR_ENABLED", "RUN_INTERNAL_JOB_WORKER", "PILOT_RETENTION_DESTRUCTIVE_ENABLED")
        truthy = {"1", "true", "yes", "on"}
        gates = {name: os.getenv(name, "").strip().lower() in truthy for name in boolean_gates}
        gates.update({name: bool(os.getenv(name, "").strip()) for name in ("CRON_SECRET", "BLOB_READ_WRITE_TOKEN")})
        gates["secret_provider_kind"] = os.getenv("PILOT_SECRET_PROVIDER", "environment")
        definitions = IntegrationRegistry().list_definitions(environment=os.getenv("APP_ENV", "development"))
        connectors = [{"key": item.key, "version": getattr(item, "version", "connector-definition-v1"), "runtime_enabled": item.runtime_enabled, "mock_only": item.mock_only} for item in definitions]
        return cls(tenant_id=tenant_id).build(
            commit_sha=os.getenv("PILOT_COMMIT_SHA") or os.getenv("VERCEL_GIT_COMMIT_SHA") or os.getenv("RENDER_GIT_COMMIT") or os.getenv("GITHUB_SHA", "unknown"),
            schema_version=os.getenv("PILOT_SCHEMA_VERSION", "unknown"),
            profile=os.getenv("PILOT_PROFILE", "catalog-only"),
            enabled_modules=[item.strip() for item in enabled if item.strip()],
            disabled_modules=[item.strip() for item in disabled if item.strip()],
            connectors=connectors,
            runtime_gates=gates,
        )


def compare_runtime_to_approved(current: dict, raw_approved: str | None) -> dict:
    if not raw_approved:
        return PilotConfigurationManifest().compare(current, None)
    try:
        approved = json.loads(raw_approved)
    except (TypeError, json.JSONDecodeError):
        return {"status": "INVALID_BASELINE", "critical": [{"field": "baseline", "severity": "critical", "approved": "valid JSON required", "runtime": "invalid baseline"}], "informational": [], "approved_hash": None, "runtime_hash": current.get("hash")}
    if not isinstance(approved, dict):
        return {"status": "INVALID_BASELINE", "critical": [{"field": "baseline", "severity": "critical", "approved": "JSON object required", "runtime": "invalid baseline"}], "informational": [], "approved_hash": None, "runtime_hash": current.get("hash")}
    return PilotConfigurationManifest(tenant_id=current.get("tenant_id")).compare(current, approved)


def check_pilot_configuration_drift(current: dict, approved: dict | None) -> dict:
    """Compare a sanitized runtime manifest with an approved manifest object."""
    return PilotConfigurationManifest(tenant_id=current.get("tenant_id")).compare(current, approved)
