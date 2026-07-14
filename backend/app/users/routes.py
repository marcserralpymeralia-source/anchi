from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.templating import templates
from app.auth.dependencies import current_user
from app.master.service import TenantUser
from app.core.security import hash_password
from app.db.models import Role, User
from app.logs.service import log_action
from app.tenancy.database import get_tenant_db

router = APIRouter(prefix="/users", tags=["users"])


@router.get("")
def list_users(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    users = db.scalars(select(User).where(User.company_id == user.company_id).order_by(User.name)).all()
    roles = db.scalars(select(Role).where(Role.company_id == user.company_id).order_by(Role.name)).all()
    return templates.TemplateResponse("users/list.html", {"request": request, "user": user, "users": users, "roles": roles})


@router.post("")
def create_user(
    name: str = Form(...),
    email: str = Form(...),
    role_id: int = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    new_user = User(company_id=user.company_id, name=name, email=email, role_id=role_id, password_hash=hash_password(password))
    db.add(new_user)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="user.create", entity_type="user", entity_id=new_user.id, message=f"Usuario creado: {email}")
    return RedirectResponse("/users", status_code=303)


@router.post("/{user_id}")
def update_user(
    user_id: int,
    name: str = Form(...),
    role_id: int = Form(...),
    is_active: bool = Form(False),
    password: str = Form(""),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    target = db.get(User, user_id)
    if target and target.company_id == user.company_id:
        target.name = name
        target.role_id = role_id
        target.is_active = is_active
        if password:
            target.password_hash = hash_password(password)
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="user.update", entity_type="user", entity_id=target.id, message=f"Usuario actualizado: {target.email}")
    return RedirectResponse("/users", status_code=303)
