import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import BrandingSettings, utcnow


DEFAULT_THEME: dict[str, Any] = {
    "colors": {
        "primary": "#123A32",
        "primary_dark": "#0B2924",
        "primary_light": "#EEF6EF",
        "accent_green": "#6BAA45",
        "accent_red": "#D61F2C",
        "text": "#1B1F22",
        "muted": "#5F6B73",
        "background": "#F5F7F6",
        "surface": "#FFFFFF",
        "border": "#DDE5E2",
        "kraft": "#F3EFE5",
        "table_alt": "#F8FAF9",
    },
    "sidebar": {
        "background": "#123A32",
        "text": "#FFFFFF",
        "muted": "#D7E5DF",
        "active_background": "#FFFFFF",
        "active_text": "#123A32",
        "hover": "#1E4A40",
        "width": 260,
    },
    "buttons": {
        "primary": "#123A32",
        "primary_hover": "#0B2924",
        "primary_text": "#FFFFFF",
        "secondary": "#FFFFFF",
        "secondary_text": "#334155",
        "danger": "#D61F2C",
        "success": "#6BAA45",
        "radius": 8,
        "font_size": 14,
    },
    "cards": {
        "background": "#FFFFFF",
        "border": "#DDE5E2",
        "radius": 14,
        "shadow": "0 8px 24px rgba(18,58,50,.08)",
        "padding": 16,
        "page_background": "#F5F7F6",
    },
    "tables": {
        "header_background": "#EEF3F1",
        "header_text": "#31443F",
        "row_odd": "#FFFFFF",
        "row_even": "#F8FAF9",
        "row_hover": "#EEF6EF",
        "border": "#DDE5E2",
        "vertical_padding": 14,
        "alternate_rows": True,
        "scoring_left_border": True,
    },
    "scoring": {
        "safe": "#2E8B57",
        "reviewable": "#D6A700",
        "doubtful": "#E67E22",
        "not_importable": "#D61F2C",
        "without_score": "#8A969D",
        "show_bar": True,
        "show_label": True,
        "show_percentage": True,
    },
    "status_badges": {
        "pending_review_bg": "#EAF0EE",
        "pending_review_text": "#1B1F22",
        "confirmed_bg": "#EEF6EF",
        "confirmed_text": "#2E8B57",
        "exported_bg": "#DFF3E5",
        "exported_text": "#1F6B43",
        "error_bg": "#FDECEC",
        "error_text": "#D61F2C",
        "no_order_bg": "#ECEFF1",
        "no_order_text": "#5F6B73",
        "doubtful_bg": "#FFF3E0",
        "doubtful_text": "#E67E22",
        "discarded_bg": "#F2F2F2",
        "discarded_text": "#5F6B73",
    },
    "login": {
        "show_logo": True,
        "title": "Anchi",
        "subtitle": "Gestion inteligente de pedidos",
        "background": "#F5F7F6",
        "card": "#FFFFFF",
        "button": "#123A32",
    },
    "typography": {"font_family": "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"},
    "layout": {"border_radius": 8},
}


DEFAULT_MICROCOPY = {
    "create_test_order": "Crear pedido de prueba",
    "review_order": "Revisar pedido",
    "confirm_order": "Confirmar pedido",
    "generate_file": "Generar archivo",
    "send_to_management": "Enviar a gestion",
    "recalculate_score": "Recalcular scoring",
    "save_changes": "Guardar cambios",
    "empty_orders": "No hay pedidos para los filtros seleccionados.",
    "empty_results": "No se han encontrado resultados.",
    "generic_error": "Ha ocurrido un error. Revisa los detalles o intentalo de nuevo.",
    "save_success": "Cambios guardados correctamente.",
    "export_success": "Pedido enviado correctamente al sistema de gestion.",
}

BRANDING_UPLOAD_DIR = Path(__file__).resolve().parents[1] / "static" / "uploads" / "branding"
ALLOWED_LOGO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".svg", ".webp"}


def deep_merge(default: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(default)
    for key, value in current.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def default_branding_payload() -> dict[str, Any]:
    settings = get_settings()
    company_name = settings.default_company_name
    app_name = settings.branding_app_name or settings.app_name
    return {
        "company_name": company_name,
        "app_name": app_name,
        "primary_claim": settings.branding_primary_claim,
        "secondary_claim": settings.branding_secondary_claim,
        "short_description": "Aplicacion para la revision, validacion y exportacion de pedidos recibidos por correo electronico.",
        "logo_url": settings.branding_logo_url,
        "dark_logo_url": settings.branding_dark_logo_url,
        "favicon_url": settings.branding_favicon_url,
        "show_logo_sidebar": True,
        "show_app_name_sidebar": True,
        "show_claim_sidebar": True,
        "show_claim_login": True,
        "theme": deepcopy(DEFAULT_THEME),
        "microcopy": deepcopy(DEFAULT_MICROCOPY),
    }


def get_or_create_branding(db: Session, company_id: int) -> BrandingSettings:
    branding = db.query(BrandingSettings).filter(BrandingSettings.company_id == company_id).one_or_none()
    if branding:
        return branding
    payload = default_branding_payload()
    branding = BrandingSettings(
        company_id=company_id,
        app_name=payload["app_name"],
        company_name=payload["company_name"],
        primary_claim=payload["primary_claim"],
        secondary_claim=payload["secondary_claim"],
        short_description=payload["short_description"],
        logo_url=payload["logo_url"],
        dark_logo_url=payload["dark_logo_url"],
        favicon_url=payload["favicon_url"],
        show_logo_sidebar=True,
        show_app_name_sidebar=True,
        show_claim_sidebar=True,
        show_claim_login=True,
        theme_json=json.dumps(payload["theme"]),
        microcopy_json=json.dumps(payload["microcopy"]),
    )
    db.add(branding)
    db.commit()
    db.refresh(branding)
    return branding


def branding_to_dict(branding: BrandingSettings) -> dict[str, Any]:
    if isinstance(branding, dict):
        theme = deep_merge(DEFAULT_THEME, deepcopy(branding.get("theme") or {}))
        microcopy = deep_merge(DEFAULT_MICROCOPY, deepcopy(branding.get("microcopy") or {}))
        return {
            "company_name": branding.get("company_name", ""),
            "app_name": branding.get("app_name", ""),
            "primary_claim": branding.get("primary_claim", ""),
            "secondary_claim": branding.get("secondary_claim", ""),
            "short_description": branding.get("short_description", ""),
            "logo_url": branding.get("logo_url") or "",
            "dark_logo_url": branding.get("dark_logo_url") or "",
            "favicon_url": branding.get("favicon_url") or "",
            "show_logo_sidebar": branding.get("show_logo_sidebar", True),
            "show_app_name_sidebar": branding.get("show_app_name_sidebar", True),
            "show_claim_sidebar": branding.get("show_claim_sidebar", True),
            "show_claim_login": branding.get("show_claim_login", True),
            "theme": theme,
            "microcopy": microcopy,
        }
    theme = deep_merge(DEFAULT_THEME, json.loads(branding.theme_json or "{}"))
    microcopy = deep_merge(DEFAULT_MICROCOPY, json.loads(branding.microcopy_json or "{}"))
    return {
        "company_name": branding.company_name,
        "app_name": branding.app_name,
        "primary_claim": branding.primary_claim,
        "secondary_claim": branding.secondary_claim,
        "short_description": branding.short_description,
        "logo_url": branding.logo_url or "",
        "dark_logo_url": branding.dark_logo_url or "",
        "favicon_url": branding.favicon_url or "",
        "show_logo_sidebar": branding.show_logo_sidebar,
        "show_app_name_sidebar": branding.show_app_name_sidebar,
        "show_claim_sidebar": branding.show_claim_sidebar,
        "show_claim_login": branding.show_claim_login,
        "theme": theme,
        "microcopy": microcopy,
    }


def set_nested(data: dict[str, Any], path: str, value: Any) -> None:
    current = data
    parts = path.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def parse_form_value(value: str) -> Any:
    if value in {"on", "true", "True"}:
        return True
    if value in {"off", "false", "False"}:
        return False
    if value.isdigit():
        return int(value)
    return value


def update_branding_from_form(branding: BrandingSettings, form: dict[str, str], user_id: int | None) -> None:
    for field in [
        "company_name",
        "app_name",
        "primary_claim",
        "secondary_claim",
        "short_description",
        "logo_url",
        "dark_logo_url",
        "favicon_url",
    ]:
        if field in form:
            setattr(branding, field, form[field])
    for field in ["show_logo_sidebar", "show_app_name_sidebar", "show_claim_sidebar", "show_claim_login"]:
        setattr(branding, field, form.get(field) == "on")
    theme = deep_merge(DEFAULT_THEME, json.loads(branding.theme_json or "{}"))
    microcopy = deep_merge(DEFAULT_MICROCOPY, json.loads(branding.microcopy_json or "{}"))
    for key, value in form.items():
        if key.startswith("theme."):
            set_nested(theme, key.removeprefix("theme."), parse_form_value(value))
        elif key.startswith("microcopy."):
            set_nested(microcopy, key.removeprefix("microcopy."), value)
    branding.theme_json = json.dumps(theme)
    branding.microcopy_json = json.dumps(microcopy)
    branding.updated_by = user_id
    branding.updated_at = utcnow()


def is_internal_brand_asset(value: str | None) -> bool:
    return bool(value) and value.startswith("/static/uploads/branding/")


def delete_brand_asset(value: str | None) -> None:
    if not is_internal_brand_asset(value):
        return
    relative = value.removeprefix("/static/")
    path = Path(__file__).resolve().parents[1] / "static" / relative
    if path.exists():
        path.unlink()


async def store_brand_asset(company_id: int, upload: UploadFile, prefix: str) -> str:
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in ALLOWED_LOGO_EXTENSIONS:
        raise ValueError("Formato de archivo no permitido. Usa PNG, JPG, SVG o WEBP.")
    BRANDING_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{company_id}-{prefix}-{uuid4().hex}{suffix}"
    path = BRANDING_UPLOAD_DIR / filename
    path.write_bytes(await upload.read())
    return f"/static/uploads/branding/{filename}"


def reset_branding(branding: BrandingSettings, user_id: int | None) -> None:
    payload = default_branding_payload()
    for key in ["company_name", "app_name", "primary_claim", "secondary_claim", "short_description", "logo_url", "dark_logo_url", "favicon_url"]:
        setattr(branding, key, payload[key])
    branding.show_logo_sidebar = True
    branding.show_app_name_sidebar = True
    branding.show_claim_sidebar = True
    branding.show_claim_login = True
    branding.theme_json = json.dumps(payload["theme"])
    branding.microcopy_json = json.dumps(payload["microcopy"])
    branding.updated_by = user_id
    branding.updated_at = utcnow()


def branding_css_vars(payload: dict[str, Any] | None) -> str:
    default_payload = default_branding_payload()
    if not payload or not isinstance(payload, dict):
        payload = default_payload
    theme = payload.get("theme")
    if not theme or not isinstance(theme, dict):
        theme = default_payload["theme"]
    else:
        theme = deep_merge(DEFAULT_THEME, theme)

    def _get(section: str, key: str, default_val: Any = "") -> Any:
        sec_dict = theme.get(section)
        if isinstance(sec_dict, dict) and key in sec_dict:
            return sec_dict[key]
        return DEFAULT_THEME.get(section, {}).get(key, default_val)

    vars_map = {
        "--bg": _get("colors", "background", "#f4f6f8"),
        "--panel": _get("colors", "surface", "#ffffff"),
        "--text": _get("colors", "text", "#111827"),
        "--muted": _get("colors", "muted", "#6b7280"),
        "--line": _get("colors", "border", "#e5e7eb"),
        "--accent": _get("buttons", "primary", "#0f766e"),
        "--accent-dark": _get("buttons", "primary_hover", "#115e59"),
        "--danger": _get("buttons", "danger", "#dc2626"),
        "--sidebar-bg": _get("sidebar", "background", "#0f172a"),
        "--sidebar-text": _get("sidebar", "text", "#e2e8f0"),
        "--sidebar-muted": _get("sidebar", "muted", "#94a3b8"),
        "--sidebar-hover": _get("sidebar", "hover", "rgba(255,255,255,0.06)"),
        "--sidebar-active-bg": _get("sidebar", "active_background", "#1e293b"),
        "--sidebar-active-text": _get("sidebar", "active_text", "#ffffff"),
        "--sidebar-width": f"{_get('sidebar', 'width', 224)}px",
        "--button-primary-text": _get("buttons", "primary_text", "#ffffff"),
        "--button-secondary-bg": _get("buttons", "secondary", "#ffffff"),
        "--button-secondary-text": _get("buttons", "secondary_text", "#374151"),
        "--button-radius": f"{_get('buttons', 'radius', 6)}px",
        "--button-font-size": f"{_get('buttons', 'font_size', 14)}px",
        "--card-bg": _get("cards", "background", "#ffffff"),
        "--card-border": _get("cards", "border", "#e5e7eb"),
        "--card-radius": f"{_get('cards', 'radius', 8)}px",
        "--card-shadow": _get("cards", "shadow", "0 1px 3px rgba(0,0,0,0.08)"),
        "--card-padding": f"{_get('cards', 'padding', 16)}px",
        "--table-head-bg": _get("tables", "header_background", "#f8fafc"),
        "--table-head-text": _get("tables", "header_text", "#475569"),
        "--table-row-odd": _get("tables", "row_odd", "#ffffff"),
        "--table-row-even": _get("tables", "row_even", "#ffffff"),
        "--table-row-hover": _get("tables", "row_hover", "#f1f5f9"),
        "--table-border": _get("tables", "border", "#e2e8f0"),
        "--table-padding-y": f"{_get('tables', 'vertical_padding', 10)}px",
        "--score-safe": _get("scoring", "safe", "#15803d"),
        "--score-reviewable": _get("scoring", "reviewable", "#b45309"),
        "--score-doubtful": _get("scoring", "doubtful", "#c2410c"),
        "--score-not-importable": _get("scoring", "not_importable", "#b91c1c"),
        "--score-without": _get("scoring", "without_score", "#64748b"),
        "--login-bg": _get("login", "background", "#0f172a"),
        "--login-card": _get("login", "card", "#ffffff"),
        "--login-button": _get("login", "button", "#0f766e"),
        "--font-family": _get("typography", "font_family", "system-ui, -apple-system, sans-serif"),
        "--status-pending-bg": _get("status_badges", "pending_review_bg", "#fffbeb"),
        "--status-pending-text": _get("status_badges", "pending_review_text", "#b45309"),
        "--status-confirmed-bg": _get("status_badges", "confirmed_bg", "#f0fdf4"),
        "--status-confirmed-text": _get("status_badges", "confirmed_text", "#15803d"),
        "--status-exported-bg": _get("status_badges", "exported_bg", "#f0fdfa"),
        "--status-exported-text": _get("status_badges", "exported_text", "#0f766e"),
        "--status-error-bg": _get("status_badges", "error_bg", "#fef2f2"),
        "--status-error-text": _get("status_badges", "error_text", "#b91c1c"),
        "--status-no-order-bg": _get("status_badges", "no_order_bg", "#f3f4f6"),
        "--status-no-order-text": _get("status_badges", "no_order_text", "#4b5563"),
        "--status-doubtful-bg": _get("status_badges", "doubtful_bg", "#fff7ed"),
        "--status-doubtful-text": _get("status_badges", "doubtful_text", "#c2410c"),
        "--status-discarded-bg": _get("status_badges", "discarded_bg", "#f3f4f6"),
        "--status-discarded-text": _get("status_badges", "discarded_text", "#6b7280"),
    }
    return ":root{" + "".join(f"{key}:{value};" for key, value in vars_map.items()) + "}"
