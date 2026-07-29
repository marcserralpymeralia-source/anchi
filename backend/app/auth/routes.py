import logging

from sqlalchemy import select
from sqlalchemy.orm import Session
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app.auth.service import authenticate_user
from app.master.models import MasterCompany
from app.core.config import get_settings
from app.master.database import get_master_db
from app.settings.branding import branding_to_dict, default_branding_payload

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": None,
            "login_branding": branding_to_dict(default_branding_payload()),
        },
    )


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    master_db: Session = Depends(get_master_db),
):
    user = authenticate_user(master_db, email, password)
    if not user:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "Credenciales no validas",
                "login_branding": branding_to_dict(default_branding_payload()),
            },
            status_code=401,
        )
    request.session["user_id"] = user.id
    request.session["company_id"] = user.company_id
    request.session["membership_id"] = user.membership_id
    request.session["company_slug"] = user.company_slug
    company = master_db.get(MasterCompany, user.company_id)
    settings = get_settings()
    logger.info(
        "Login correcto: user_id=%s email=%s company_id=%s company=%s env=%s",
        user.id,
        user.email,
        user.company_id,
        company.name if company else "",
        settings.environment,
    )
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

from app.core.templating import templates
