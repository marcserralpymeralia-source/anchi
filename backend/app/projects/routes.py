from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user
from app.core.templating import templates
from app.db.models import Project, Task, TaskSchedule, TimeEntry
from app.logs.service import log_action
from app.master.service import TenantUser
from app.projects.service import (
    add_project_member,
    add_time_entry,
    archive_project,
    build_agenda_page,
    build_project_detail,
    build_projects_page,
    build_task_detail,
    build_tasks_page,
    cancel_timer,
    create_project,
    create_task,
    create_task_from_source,
    schedule_task,
    start_timer,
    stop_timer,
    transition_task,
    update_project,
    update_task,
    update_time_entry,
)
from app.tenancy.database import get_tenant_db

router = APIRouter(tags=["projects"])


def _redirect(request: Request, fallback: str) -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)


@router.get("/agenda")
def agenda_index(request: Request, days: int = 7, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    data = build_agenda_page(db, user.company_id, user_id=user.id, days=days)
    return templates.TemplateResponse("agenda/index.html", {"request": request, "user": user, **data})


@router.get("/projects")
def projects_index(
    request: Request,
    page: int = 1,
    page_size: int = 25,
    search: str = "",
    status: str = "",
    priority: str = "",
    customer_id: int = 0,
    owner_user_id: int = 0,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    data = build_projects_page(
        db,
        user.company_id,
        page=page,
        page_size=page_size,
        search=search,
        status=status,
        priority=priority,
        customer_id=customer_id,
        owner_user_id=owner_user_id,
    )
    return templates.TemplateResponse("projects/list.html", {"request": request, "user": user, **data, "can_manage_projects": user.role.name in {"Administrador", "Superadmin"}})


@router.post("/projects")
def projects_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    client_id: int = Form(0),
    owner_user_id: int = Form(0),
    status: str = Form("draft"),
    priority: str = Form("normal"),
    start_date: str = Form(""),
    due_date: str = Form(""),
    budgeted_minutes: int = Form(0),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    project = create_project(
        db,
        company_id=user.company_id,
        name=name,
        description=description,
        client_id=client_id or None,
        owner_user_id=owner_user_id or user.id,
        status=status,
        priority=priority,
        start_date=start_date,
        due_date=due_date,
        budgeted_minutes=budgeted_minutes,
        created_by_user_id=user.id,
    )
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="project.create", entity_type="project", entity_id=project.id, message=f"Proyecto creado: {project.name}")
    return _redirect(request, f"/projects/{project.id}")


@router.get("/projects/{project_id}")
def project_detail(project_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    data = build_project_detail(db, user.company_id, project_id)
    if not data:
        return PlainTextResponse("No encontrado", status_code=404)
    return templates.TemplateResponse("projects/detail.html", {"request": request, "user": user, **data, "can_manage_projects": user.role.name in {"Administrador", "Superadmin"}})


@router.post("/projects/{project_id}/update")
def project_update(
    project_id: int,
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    client_id: int = Form(0),
    owner_user_id: int = Form(0),
    status: str = Form(""),
    priority: str = Form(""),
    start_date: str = Form(""),
    due_date: str = Form(""),
    budgeted_minutes: int = Form(0),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    project = db.scalar(select(Project).where(Project.id == project_id, Project.company_id == user.company_id))
    if not project:
        return PlainTextResponse("No encontrado", status_code=404)
    update_project(
        db,
        project,
        name=name,
        description=description,
        client_id=client_id or None,
        owner_user_id=owner_user_id or None,
        status=status,
        priority=priority,
        start_date=start_date,
        due_date=due_date,
        budgeted_minutes=budgeted_minutes,
    )
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="project.update", entity_type="project", entity_id=project.id, message=f"Proyecto actualizado: {project.name}")
    return _redirect(request, f"/projects/{project.id}")


@router.post("/projects/{project_id}/archive")
def project_archive(project_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    project = db.scalar(select(Project).where(Project.id == project_id, Project.company_id == user.company_id))
    if not project:
        return PlainTextResponse("No encontrado", status_code=404)
    archive_project(project)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="project.archive", entity_type="project", entity_id=project.id, message=f"Proyecto archivado: {project.name}")
    return _redirect(request, "/projects")


@router.post("/projects/{project_id}/members")
def project_member_add(
    project_id: int,
    request: Request,
    user_id: int = Form(...),
    role: str = Form("member"),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    member = add_project_member(db, company_id=user.company_id, project_id=project_id, user_id=user_id, role=role)
    db.commit()
    if member:
        log_action(db, company_id=user.company_id, user=user, action="project.member.add", entity_type="project_member", entity_id=member.id, message="Miembro añadido a proyecto")
    return _redirect(request, f"/projects/{project_id}")


@router.get("/tasks")
def tasks_index(
    request: Request,
    scope: str = "mine",
    status: str = "",
    project_id: int = 0,
    search: str = "",
    page: int = 1,
    page_size: int = 25,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    data = build_tasks_page(db, user.company_id, user_id=user.id, scope=scope, status=status, project_id=project_id, search=search, page=page, page_size=page_size)
    return templates.TemplateResponse("tasks/list.html", {"request": request, "user": user, **data, "can_manage_projects": user.role.name in {"Administrador", "Superadmin"}})


@router.post("/tasks")
def tasks_create(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    project_id: int = Form(0),
    assigned_user_id: int = Form(0),
    status: str = Form("todo"),
    priority: str = Form("normal"),
    estimated_minutes: int = Form(0),
    due_date: str = Form(""),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    task = create_task(
        db,
        company_id=user.company_id,
        title=title,
        description=description,
        project_id=project_id or None,
        assigned_user_id=assigned_user_id or user.id,
        created_by_user_id=user.id,
        status=status,
        priority=priority,
        estimated_minutes=estimated_minutes,
        due_date=due_date,
    )
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="task.create", entity_type="task", entity_id=task.id, message=f"Tarea creada: {task.title}")
    return _redirect(request, f"/tasks/{task.id}")


@router.post("/tasks/from-source")
def tasks_from_source(
    request: Request,
    source_kind: str = Form(...),
    source_id: int = Form(...),
    title: str = Form(""),
    description: str = Form(""),
    project_id: int = Form(0),
    assigned_user_id: int = Form(0),
    status: str = Form("todo"),
    priority: str = Form("normal"),
    estimated_minutes: int = Form(0),
    due_date: str = Form(""),
    return_url: str = Form(""),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    task, created = create_task_from_source(
        db,
        company_id=user.company_id,
        source_kind=source_kind,
        source_id=source_id,
        title=title,
        description=description,
        project_id=project_id or None,
        assigned_user_id=assigned_user_id or user.id,
        created_by_user_id=user.id,
        status=status,
        priority=priority,
        estimated_minutes=estimated_minutes,
        due_date=due_date,
    )
    db.commit()
    if created:
        log_action(db, company_id=user.company_id, user=user, action="task.create_from_source", entity_type="task", entity_id=task.id, message=f"Tarea creada desde {source_kind} {source_id}")
    else:
        log_action(db, company_id=user.company_id, user=user, action="task.create_from_source.duplicate", entity_type="task", entity_id=task.id, message="Se reutilizó una tarea ya creada desde el mismo origen")
    return RedirectResponse(return_url or request.headers.get("referer") or f"/tasks/{task.id}", status_code=303)


@router.get("/tasks/{task_id}")
def task_detail(task_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    data = build_task_detail(db, user.company_id, task_id, user_id=user.id)
    if not data:
        return PlainTextResponse("No encontrado", status_code=404)
    return templates.TemplateResponse("tasks/detail.html", {"request": request, "user": user, **data, "can_manage_projects": user.role.name in {"Administrador", "Superadmin"}})


@router.post("/tasks/{task_id}/update")
def task_update(
    task_id: int,
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    project_id: int = Form(0),
    assigned_user_id: int = Form(0),
    status: str = Form(""),
    priority: str = Form(""),
    estimated_minutes: int = Form(0),
    due_date: str = Form(""),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    task = db.scalar(select(Task).where(Task.id == task_id, Task.company_id == user.company_id))
    if not task:
        return PlainTextResponse("No encontrado", status_code=404)
    update_task(
        task,
        title=title,
        description=description,
        project_id=project_id or None,
        assigned_user_id=assigned_user_id or None,
        status=status or task.status,
        priority=priority or task.priority,
        estimated_minutes=estimated_minutes,
        due_date=due_date,
    )
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="task.update", entity_type="task", entity_id=task.id, message=f"Tarea actualizada: {task.title}")
    return _redirect(request, f"/tasks/{task.id}")


@router.post("/tasks/{task_id}/transition")
def task_transition(task_id: int, request: Request, status: str = Form(...), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    task = db.scalar(select(Task).where(Task.id == task_id, Task.company_id == user.company_id))
    if not task:
        return PlainTextResponse("No encontrado", status_code=404)
    transition_task(task, status)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="task.transition", entity_type="task", entity_id=task.id, message=f"Tarea movida a {status}")
    return _redirect(request, f"/tasks/{task.id}")


@router.post("/tasks/{task_id}/schedule")
def task_schedule(
    task_id: int,
    request: Request,
    scheduled_date: str = Form(...),
    start_time: str = Form(""),
    planned_minutes: int = Form(0),
    notes: str = Form(""),
    assigned_user_id: int = Form(0),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    task = db.scalar(select(Task).where(Task.id == task_id, Task.company_id == user.company_id))
    if not task:
        return PlainTextResponse("No encontrado", status_code=404)
    schedule = schedule_task(
        db,
        company_id=user.company_id,
        task=task,
        scheduled_date=scheduled_date,
        start_time=start_time,
        planned_minutes=planned_minutes,
        notes=notes,
        assigned_user_id=assigned_user_id or task.assigned_user_id,
        created_by_user_id=user.id,
    )
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="task.schedule.create", entity_type="task_schedule", entity_id=schedule.id, message="Tarea programada")
    return _redirect(request, f"/tasks/{task.id}")


@router.post("/tasks/{task_id}/schedules/{schedule_id}/delete")
def task_schedule_delete(task_id: int, schedule_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    schedule = db.scalar(select(TaskSchedule).where(TaskSchedule.id == schedule_id, TaskSchedule.company_id == user.company_id, TaskSchedule.task_id == task_id))
    if not schedule:
        return PlainTextResponse("No encontrado", status_code=404)
    db.delete(schedule)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="task.schedule.delete", entity_type="task_schedule", entity_id=schedule.id, message="Programación eliminada")
    return _redirect(request, f"/tasks/{task_id}")


@router.post("/tasks/{task_id}/time-entries")
def task_time_entry_create(
    task_id: int,
    request: Request,
    entry_date: str = Form(""),
    minutes: int = Form(0),
    description: str = Form(""),
    project_id: int = Form(0),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    task = db.scalar(select(Task).where(Task.id == task_id, Task.company_id == user.company_id))
    if not task:
        return PlainTextResponse("No encontrado", status_code=404)
    entry = add_time_entry(
        db,
        company_id=user.company_id,
        task=task,
        user_id=user.id,
        entry_date=entry_date or datetime.now(timezone.utc).date().isoformat(),
        minutes=minutes,
        description=description,
        project_id=project_id or task.project_id,
        created_by_user_id=user.id,
    )
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="task.time_entry.create", entity_type="time_entry", entity_id=entry.id, message="Horas registradas manualmente")
    return _redirect(request, f"/tasks/{task.id}")


@router.post("/tasks/{task_id}/time-entries/{entry_id}/update")
def task_time_entry_update(
    task_id: int,
    entry_id: int,
    request: Request,
    entry_date: str = Form(...),
    minutes: int = Form(0),
    description: str = Form(""),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    entry = db.scalar(select(TimeEntry).where(TimeEntry.id == entry_id, TimeEntry.company_id == user.company_id, TimeEntry.task_id == task_id))
    if not entry:
        return PlainTextResponse("No encontrado", status_code=404)
    update_time_entry(entry, entry_date=entry_date, minutes=minutes, description=description)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="task.time_entry.update", entity_type="time_entry", entity_id=entry.id, message="Imputación actualizada")
    return _redirect(request, f"/tasks/{task_id}")


@router.post("/tasks/{task_id}/time-entries/{entry_id}/delete")
def task_time_entry_delete(task_id: int, entry_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    entry = db.scalar(select(TimeEntry).where(TimeEntry.id == entry_id, TimeEntry.company_id == user.company_id, TimeEntry.task_id == task_id))
    if not entry:
        return PlainTextResponse("No encontrado", status_code=404)
    db.delete(entry)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="task.time_entry.delete", entity_type="time_entry", entity_id=entry.id, message="Imputación eliminada")
    return _redirect(request, f"/tasks/{task_id}")


@router.post("/tasks/{task_id}/timer/start")
def task_timer_start(task_id: int, request: Request, notes: str = Form(""), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    task = db.scalar(select(Task).where(Task.id == task_id, Task.company_id == user.company_id))
    if not task:
        return PlainTextResponse("No encontrado", status_code=404)
    timer = start_timer(db, company_id=user.company_id, user_id=user.id, task=task, notes=notes, created_by_user_id=user.id)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="task.timer.start", entity_type="active_timer", entity_id=timer.id, message="Temporizador iniciado")
    return _redirect(request, f"/tasks/{task.id}")


@router.post("/tasks/{task_id}/timer/stop")
def task_timer_stop(task_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    entry = stop_timer(db, company_id=user.company_id, user_id=user.id, created_by_user_id=user.id)
    db.commit()
    if entry:
        log_action(db, company_id=user.company_id, user=user, action="task.timer.stop", entity_type="time_entry", entity_id=entry.id, message="Temporizador detenido e imputado")
    return _redirect(request, f"/tasks/{task_id}")


@router.post("/tasks/{task_id}/timer/cancel")
def task_timer_cancel(task_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    cancel_timer(db, company_id=user.company_id, user_id=user.id)
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="task.timer.cancel", entity_type="active_timer", entity_id=task_id, message="Temporizador cancelado")
    return _redirect(request, f"/tasks/{task_id}")


@router.post("/tasks/{task_id}/archive")
def task_archive(task_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    task = db.scalar(select(Task).where(Task.id == task_id, Task.company_id == user.company_id))
    if not task:
        return PlainTextResponse("No encontrado", status_code=404)
    transition_task(task, "archived")
    db.commit()
    log_action(db, company_id=user.company_id, user=user, action="task.archive", entity_type="task", entity_id=task.id, message="Tarea archivada")
    return _redirect(request, "/tasks")
