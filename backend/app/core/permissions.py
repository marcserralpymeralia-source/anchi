from __future__ import annotations

PERMISSIONS = {
    "view_agent_workbench": "Ver bandeja operativa del agente",
    "review_orders": "Revisar pedidos",
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
    "view_logs": "Ver logs",
    "view_technical_logs": "Ver logs tecnicos",
    "manage_users": "Gestionar usuarios",
    "manage_tenants": "Gestionar tenants",
}

DEFAULT_ROLE_PERMISSIONS = {
    "Administrador": ",".join(PERMISSIONS.keys()),
    "Supervisor": "view_agent_workbench,review_orders,confirm_orders,export_orders,edit_customers,edit_products,approve_learning,view_logs",
    "Operador": "view_agent_workbench,review_orders,confirm_orders,discard_messages",
    "Solo lectura": "view_agent_workbench",
}

