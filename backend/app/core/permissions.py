from __future__ import annotations

PERMISSIONS = {
    "view_inbox": "Consultar bandejas y conversaciones",
    "manage_inbox": "Gestionar mensajes y conversaciones",
    "view_agent_workbench": "Ver bandeja operativa del agente",
    "review_orders": "Revisar pedidos",
    "manage_orders": "Editar pedidos",
    "confirm_orders": "Confirmar pedidos",
    "export_orders": "Exportar pedidos",
    "discard_messages": "Descartar mensajes",
    "edit_customers": "Editar clientes",
    "import_customers": "Importar clientes",
    "edit_products": "Editar productos",
    "import_products": "Importar productos",
    "approve_learning": "Aprobar aprendizaje",
    "configure_channels": "Configurar canales",
    "configure_agent": "Configurar agente",
    "configure_scoring": "Configurar scoring",
    "configure_export": "Configurar exportacion",
    "configure_settings": "Configurar la empresa",
    "view_logs": "Ver logs",
    "view_technical_logs": "Ver logs tecnicos",
    "manage_users": "Gestionar usuarios",
    "manage_jobs": "Gestionar tareas tecnicas",
    "manage_whatsapp": "Responder en WhatsApp",
    "manage_tenants": "Gestionar tenants",
    "configure_integrations": "Configurar integraciones",
    "activate_integrations": "Activar y pausar integraciones",
}

DEFAULT_ROLE_PERMISSIONS = {
    "Administrador": ",".join(key for key in PERMISSIONS if key not in {"view_technical_logs", "manage_tenants"}),
    "Supervisor": "view_inbox,manage_inbox,view_agent_workbench,review_orders,manage_orders,confirm_orders,export_orders,edit_customers,edit_products,approve_learning,view_logs,manage_whatsapp",
    "Operador": "view_inbox,manage_inbox,view_agent_workbench,review_orders,confirm_orders,discard_messages,manage_whatsapp",
    "Solo lectura": "view_inbox,view_agent_workbench",
}


def has_permission(user, permission: str) -> bool:
    """Return whether a tenant identity can perform a named capability."""

    role = getattr(user, "role", None)
    raw_permissions = getattr(role, "permissions", "") or ""
    return permission in {item.strip() for item in raw_permissions.split(",") if item.strip()}


def permission_for_request(method: str, path: str) -> str | None:
    """Map mutating tenant routes to capabilities.

    Read requests remain available to authenticated tenant members except
    integration configuration, which is restricted to tenant administrators.
    Individual handlers still retain their domain-level ownership checks.
    """

    normalized = "/" + (path or "").strip("/")
    if method.upper() in {"GET", "HEAD"}:
        return "configure_integrations" if normalized.startswith("/integrations") else None
    if method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None

    if normalized.startswith("/superadmin") or normalized in {"/login", "/logout"}:
        return None
    if normalized.startswith("/webhooks/whatsapp/") and normalized.endswith("/respond"):
        return "manage_whatsapp"
    if normalized.startswith("/alerts"):
        return "manage_inbox"
    if normalized.startswith("/entries"):
        return "manage_inbox"
    if normalized.startswith("/setup"):
        return "configure_settings"
    if normalized.startswith("/users"):
        return "manage_users"
    if normalized.startswith("/customers"):
        return "import_customers" if "/import" in normalized else "edit_customers"
    if normalized.startswith("/products"):
        return "import_products" if "/import" in normalized else "edit_products"
    if normalized.startswith("/databases"):
        if "/import/customers" in normalized:
            return "import_customers"
        if "/import/products" in normalized:
            return "import_products"
        return "edit_customers" if "/customers" in normalized else "edit_products"
    if normalized.startswith("/orders"):
        if "/confirm" in normalized:
            return "confirm_orders"
        if "/export" in normalized or "/ftp" in normalized:
            return "export_orders"
        if any(token in normalized for token in ("/discard", "/no-order", "/mark-not-order")):
            return "discard_messages"
        return "manage_orders"
    if normalized.startswith("/mail") or normalized.startswith("/workbench") or normalized.startswith("/channels"):
        return "manage_inbox"
    if normalized.startswith("/whatsapp"):
        return "manage_whatsapp"
    if normalized.startswith("/learning"):
        return "approve_learning"
    if normalized.startswith("/jobs"):
        return "manage_jobs"
    if normalized.startswith("/integrations"):
        if any(token in normalized for token in ("/activate", "/pause", "/enable", "/disable")):
            return "activate_integrations"
        if normalized.startswith("/integrations/onboarding") or normalized.startswith("/integrations/connections") or normalized.startswith("/integrations/roles") or normalized.startswith("/integrations/routes"):
            return "configure_integrations"
    if normalized.startswith("/imports"):
        return "manage_inbox"
    if normalized.startswith("/settings/channels"):
        return "configure_channels"
    if normalized.startswith("/settings/agent") or "/prompts" in normalized or "/llm" in normalized:
        return "configure_agent"
    if normalized.startswith("/settings/scoring"):
        return "configure_scoring"
    if normalized.startswith("/settings/export") or "/ftp" in normalized:
        return "configure_export"
    if normalized.startswith("/settings"):
        return "configure_settings"
    return None
