from fastapi import Request
from fastapi.templating import Jinja2Templates


def status_label(value: str | None) -> str:
    labels = {
        "pedido_pendiente_revision": "Pendiente de revision",
        "pending_review": "Pendiente de revision",
        "pedido_validado": "Confirmado",
        "pedido_confirmado": "Confirmado",
        "pedido_exportado": "Exportado",
        "error_exportacion": "Error de exportacion",
        "error_procesamiento": "Error de procesamiento",
        "no_pedido": "No contiene pedido",
        "descartado": "Descartado",
        "sent": "Enviado",
        "error": "Error",
        "pedido": "Pedido",
        "consulta": "Consulta",
        "incidencia": "Incidencia",
        "no_importable": "No importable",
        "dudoso": "Dudoso",
        "active": "Activo",
        "inactive": "Inactivo",
    }
    return labels.get(value or "", value or "")


def status_class(value: str | None) -> str:
    value = value or ""
    if value in {"pedido_pendiente_revision", "pending_review"}:
        return "status-pending"
    if value in {"pedido_validado", "pedido_confirmado"}:
        return "status-confirmed"
    if value == "pedido_exportado":
        return "status-exported"
    if value.startswith("error"):
        return "status-error"
    if value == "no_pedido":
        return "status-no-order"
    if value in {"dudoso", "no_importable"}:
        return "status-doubtful"
    if value == "descartado":
        return "status-discarded"
    return ""


def page_url(request: Request, page: int | None = None, page_size: int | None = None) -> str:
    params = dict(request.query_params)
    if page is not None:
        params["page"] = str(page)
    if page_size is not None:
        params["page_size"] = str(page_size)
        params["page"] = "1"
    query = "&".join(f"{key}={value}" for key, value in params.items() if value not in ("", None))
    return f"{request.url.path}?{query}" if query else request.url.path


templates = Jinja2Templates(directory="app/templates")
templates.env.filters["status_label"] = status_label
templates.env.filters["status_class"] = status_class
templates.env.globals["page_url"] = page_url
