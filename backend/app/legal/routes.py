from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.core.templating import templates


router = APIRouter(tags=["legal"])


@router.get("/privacy", response_class=HTMLResponse, name="privacy_policy")
def privacy_policy(request: Request):
    """Serve the privacy policy without requiring an authenticated tenant."""
    return templates.TemplateResponse(
        "privacy.html",
        {
            "request": request,
            "title": "Política de privacidad | Anchi",
        },
    )
