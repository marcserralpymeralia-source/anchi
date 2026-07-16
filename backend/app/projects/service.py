from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.core.pagination import paginate, normalize_page
from app.db.models import ActiveTimer, Customer, Email, InboundMessage, InputChannel, Project, ProjectMember, Task, TaskSchedule, TimeEntry, User

PROJECT_STATUS_LABELS = {
    "draft": "Borrador",
    "active": "Activo",
    "on_hold": "En pausa",
    "completed": "Completado",
    "cancelled": "Cancelado",
    "archived": "Archivado",
}

TASK_STATUS_LABELS = {
    "todo": "Por hacer",
    "in_progress": "En curso",
    "blocked": "Bloqueada",
    "done": "Hecha",
    "cancelled": "Cancelada",
    "archived": "Archivada",
}

PROJECT_PRIORITY_LABELS = {
    "low": "Baja",
    "normal": "Normal",
    "high": "Alta",
    "urgent": "Urgente",
}

TASK_PRIORITY_LABELS = PROJECT_PRIORITY_LABELS

ACTIVE_TASK_STATUSES = {"todo", "in_progress", "blocked"}
ACTIVE_PROJECT_STATUSES = {"draft", "active", "on_hold"}


def _fmt_dt(value: datetime | None) -> str:
    return value.strftime("%d/%m/%Y %H:%M") if value else "--"


def _fmt_date(value: str | None) -> str:
    if not value:
        return "--"
    return value


def _human_minutes(minutes: int | None) -> str:
    total = max(int(minutes or 0), 0)
    hours, mins = divmod(total, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def _ensure_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _label_from_map(value: str | None, labels: dict[str, str]) -> str:
    return labels.get(value or "", (value or "").replace("_", " ").title() or "--")


def _company_users(db: Session, company_id: int) -> list[User]:
    return list(
        db.scalars(
            select(User)
            .where(User.company_id == company_id, User.is_active.is_(True))
            .order_by(User.name.asc())
        ).all()
    )


def _company_customers(db: Session, company_id: int) -> list[Customer]:
    return list(
        db.scalars(
            select(Customer)
            .where(Customer.company_id == company_id, Customer.deleted_at.is_(None))
            .order_by(Customer.fiscal_name.asc())
        ).all()
    )


def _company_projects(db: Session, company_id: int, *, include_archived: bool = False) -> list[Project]:
    stmt = select(Project).where(Project.company_id == company_id)
    if not include_archived:
        stmt = stmt.where(Project.archived_at.is_(None))
    return list(db.scalars(stmt.order_by(Project.updated_at.desc(), Project.id.desc())).all())


def _project_metrics(db: Session, company_id: int, project_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not project_ids:
        return {}
    task_rows = db.execute(
        select(
            Task.project_id,
            func.count(Task.id).label("task_count"),
            func.coalesce(func.sum(case((Task.status.in_(tuple(ACTIVE_TASK_STATUSES)), 1), else_=0)), 0).label("open_tasks"),
            func.coalesce(func.sum(case((Task.status == "done", 1), else_=0)), 0).label("done_tasks"),
            func.coalesce(func.sum(Task.estimated_minutes), 0).label("estimated_minutes"),
        )
        .where(Task.company_id == company_id, Task.project_id.in_(project_ids), Task.archived_at.is_(None))
        .group_by(Task.project_id)
    ).all()
    time_rows = db.execute(
        select(
            TimeEntry.project_id,
            func.coalesce(func.sum(TimeEntry.minutes), 0).label("recorded_minutes"),
        )
        .where(TimeEntry.company_id == company_id, TimeEntry.project_id.in_(project_ids))
        .group_by(TimeEntry.project_id)
    ).all()
    metrics: dict[int, dict[str, Any]] = {}
    for row in task_rows:
        metrics[int(row.project_id)] = {
            "task_count": int(row.task_count or 0),
            "open_tasks": int(row.open_tasks or 0),
            "done_tasks": int(row.done_tasks or 0),
            "estimated_minutes": int(row.estimated_minutes or 0),
            "recorded_minutes": 0,
        }
    for row in time_rows:
        metrics.setdefault(int(row.project_id), {"task_count": 0, "open_tasks": 0, "done_tasks": 0, "estimated_minutes": 0, "recorded_minutes": 0})
        metrics[int(row.project_id)]["recorded_minutes"] = int(row.recorded_minutes or 0)
    return metrics


def _task_metrics(db: Session, company_id: int, task_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not task_ids:
        return {}
    time_rows = db.execute(
        select(
            TimeEntry.task_id,
            func.coalesce(func.sum(TimeEntry.minutes), 0).label("recorded_minutes"),
        )
        .where(TimeEntry.company_id == company_id, TimeEntry.task_id.in_(task_ids))
        .group_by(TimeEntry.task_id)
    ).all()
    schedule_rows = db.execute(
        select(
            TaskSchedule.task_id,
            func.count(TaskSchedule.id).label("schedule_count"),
            func.min(TaskSchedule.scheduled_date).label("next_date"),
        )
        .where(TaskSchedule.company_id == company_id, TaskSchedule.task_id.in_(task_ids))
        .group_by(TaskSchedule.task_id)
    ).all()
    metrics: dict[int, dict[str, Any]] = {}
    for row in time_rows:
        metrics[int(row.task_id)] = {"recorded_minutes": int(row.recorded_minutes or 0), "schedule_count": 0, "next_date": None}
    for row in schedule_rows:
        metrics.setdefault(int(row.task_id), {"recorded_minutes": 0, "schedule_count": 0, "next_date": None})
        metrics[int(row.task_id)]["schedule_count"] = int(row.schedule_count or 0)
        metrics[int(row.task_id)]["next_date"] = row.next_date
    return metrics


def _timer_for_user(db: Session, company_id: int, user_id: int) -> ActiveTimer | None:
    return db.scalar(
        select(ActiveTimer)
        .where(ActiveTimer.company_id == company_id, ActiveTimer.user_id == user_id)
    )


def _active_timer_snapshot(db: Session, company_id: int, user_id: int) -> dict[str, Any] | None:
    timer = _timer_for_user(db, company_id, user_id)
    if not timer:
        return None
    task = db.get(Task, timer.task_id)
    project = db.get(Project, task.project_id) if task and task.project_id else None
    return {
        "id": timer.id,
        "task_id": timer.task_id,
        "task_title": task.title if task else "Tarea",
        "project_name": project.name if project else (task and task.project_id and "Proyecto") or "Sin proyecto",
        "started_at": timer.started_at,
        "started_label": _fmt_dt(timer.started_at),
        "notes": timer.notes or "",
    }


def build_projects_page(db: Session, company_id: int, *, page: int = 1, page_size: int = 25, search: str = "", status: str = "", priority: str = "", customer_id: int = 0, owner_user_id: int = 0) -> dict[str, Any]:
    stmt = select(Project).where(Project.company_id == company_id, Project.archived_at.is_(None))
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(or_(Project.name.ilike(like), Project.description.ilike(like)))
    if status:
        stmt = stmt.where(Project.status == status)
    if priority:
        stmt = stmt.where(Project.priority == priority)
    if customer_id:
        stmt = stmt.where(Project.client_id == customer_id)
    if owner_user_id:
        stmt = stmt.where(Project.owner_user_id == owner_user_id)
    stmt = stmt.order_by(Project.updated_at.desc(), Project.id.desc())
    projects, pagination = paginate(db, stmt, page=page, page_size=page_size)
    project_ids = [project.id for project in projects]
    metrics = _project_metrics(db, company_id, project_ids)
    users = _company_users(db, company_id)
    customers = _company_customers(db, company_id)
    rows = []
    for project in projects:
        metric = metrics.get(project.id, {})
        recorded = int(metric.get("recorded_minutes", 0) or 0)
        estimated = int(metric.get("estimated_minutes", 0) or project.budgeted_minutes or 0)
        rows.append(
            {
                "id": project.id,
                "name": project.name,
                "description": project.description or "",
                "status": project.status,
                "status_label": _label_from_map(project.status, PROJECT_STATUS_LABELS),
                "priority": project.priority,
                "priority_label": _label_from_map(project.priority, PROJECT_PRIORITY_LABELS),
                "customer_id": project.client_id,
                "customer_name": next((customer.fiscal_name for customer in customers if customer.id == project.client_id), "Sin cliente"),
                "owner_user_id": project.owner_user_id,
                "owner_name": next((user.name for user in users if user.id == project.owner_user_id), "Sin responsable"),
                "budgeted_minutes": int(project.budgeted_minutes or 0),
                "estimated_minutes": estimated,
                "recorded_minutes": recorded,
                "task_count": int(metric.get("task_count", 0) or 0),
                "open_tasks": int(metric.get("open_tasks", 0) or 0),
                "done_tasks": int(metric.get("done_tasks", 0) or 0),
                "progress": min(100, round((recorded / estimated) * 100)) if estimated else None,
                "start_date": _fmt_date(project.start_date),
                "due_date": _fmt_date(project.due_date),
                "updated_at": _fmt_dt(project.updated_at),
            }
        )
    summary = {
        "total": len(rows),
        "active": sum(1 for row in rows if row["status"] in ACTIVE_PROJECT_STATUSES),
        "completed": sum(1 for row in rows if row["status"] == "completed"),
        "archived": db.scalar(select(func.count()).select_from(Project).where(Project.company_id == company_id, Project.archived_at.is_not(None))) or 0,
        "hours_planned": round(sum(row["estimated_minutes"] for row in rows) / 60, 1),
        "hours_recorded": round(sum(row["recorded_minutes"] for row in rows) / 60, 1),
    }
    filters = {
        "search": search,
        "status": status,
        "priority": priority,
        "customer_id": customer_id,
        "owner_user_id": owner_user_id,
    }
    return {
        "projects": rows,
        "pagination": pagination,
        "summary": summary,
        "filters": filters,
        "users": users,
        "customers": customers,
        "status_options": [(key, label) for key, label in PROJECT_STATUS_LABELS.items()],
        "priority_options": [(key, label) for key, label in PROJECT_PRIORITY_LABELS.items()],
    }


def build_project_detail(db: Session, company_id: int, project_id: int) -> dict[str, Any] | None:
    project = db.scalar(select(Project).where(Project.id == project_id, Project.company_id == company_id))
    if not project:
        return None
    users = _company_users(db, company_id)
    customers = _company_customers(db, company_id)
    metrics = _project_metrics(db, company_id, [project.id]).get(project.id, {})
    tasks = build_tasks_page(db, company_id, scope="project", project_id=project.id, page=1, page_size=100)["tasks"]
    members = list(
        db.scalars(
            select(ProjectMember)
            .where(ProjectMember.company_id == company_id, ProjectMember.project_id == project.id)
            .order_by(ProjectMember.created_at.asc())
        ).all()
    )
    member_rows = [
        {
            "id": member.id,
            "user_id": member.user_id,
            "user_name": next((user.name for user in users if user.id == member.user_id), "Usuario"),
            "role": member.role,
            "role_label": _label_from_map(member.role, {"owner": "Propietario", "lead": "Líder", "member": "Miembro"}),
        }
        for member in members
    ]
    return {
        "project": project,
        "project_metrics": {
            "task_count": int(metrics.get("task_count", 0) or 0),
            "open_tasks": int(metrics.get("open_tasks", 0) or 0),
            "done_tasks": int(metrics.get("done_tasks", 0) or 0),
            "estimated_minutes": int(metrics.get("estimated_minutes", 0) or 0),
            "recorded_minutes": int(metrics.get("recorded_minutes", 0) or 0),
        },
        "project_metrics_label": {
            "estimated_hours": _human_minutes(metrics.get("estimated_minutes", 0)),
            "recorded_hours": _human_minutes(metrics.get("recorded_minutes", 0)),
        },
        "customers": customers,
        "users": users,
        "members": member_rows,
        "tasks": tasks,
        "status_options": [(key, label) for key, label in PROJECT_STATUS_LABELS.items()],
        "priority_options": [(key, label) for key, label in PROJECT_PRIORITY_LABELS.items()],
    }


def build_tasks_page(
    db: Session,
    company_id: int,
    *,
    user_id: int | None = None,
    scope: str = "mine",
    status: str = "",
    project_id: int = 0,
    search: str = "",
    page: int = 1,
    page_size: int = 25,
) -> dict[str, Any]:
    stmt = select(Task).where(Task.company_id == company_id, Task.archived_at.is_(None))
    if scope == "mine" and user_id:
        stmt = stmt.where(Task.assigned_user_id == user_id)
    if status:
        stmt = stmt.where(Task.status == status)
    if project_id:
        stmt = stmt.where(Task.project_id == project_id)
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(or_(Task.title.ilike(like), Task.description.ilike(like), Task.source_reference.ilike(like)))
    stmt = stmt.order_by(case((Task.status == "in_progress", 0), (Task.status == "todo", 1), (Task.status == "blocked", 2), else_=3), Task.due_date.asc().nulls_last(), Task.updated_at.desc())
    tasks, pagination = paginate(db, stmt, page=page, page_size=page_size)
    task_ids = [task.id for task in tasks]
    metrics = _task_metrics(db, company_id, task_ids)
    users = _company_users(db, company_id)
    customers = _company_customers(db, company_id)
    projects = _company_projects(db, company_id)
    active_timer = _active_timer_snapshot(db, company_id, user_id or 0) if user_id else None
    project_lookup = {project.id: project for project in projects}
    rows = []
    for task in tasks:
        metric = metrics.get(task.id, {})
        project = project_lookup.get(task.project_id) if task.project_id else None
        assigned_user = next((user for user in users if user.id == task.assigned_user_id), None)
        recorded = int(metric.get("recorded_minutes", 0) or 0)
        estimated = int(task.estimated_minutes or 0)
        rows.append(
            {
                "id": task.id,
                "title": task.title,
                "description": task.description or "",
                "status": task.status,
                "status_label": _label_from_map(task.status, TASK_STATUS_LABELS),
                "priority": task.priority,
                "priority_label": _label_from_map(task.priority, TASK_PRIORITY_LABELS),
                "project_id": task.project_id,
                "project_name": project.name if project else "Sin proyecto",
                "client_name": next((customer.fiscal_name for customer in customers if customer.id == project.client_id), "Sin cliente") if project else "Sin cliente",
                "assigned_user_id": task.assigned_user_id,
                "assigned_user_name": assigned_user.name if assigned_user else ("Tú" if user_id and task.assigned_user_id == user_id else "Sin asignar"),
                "due_date": _fmt_date(task.due_date),
                "estimated_minutes": estimated,
                "recorded_minutes": recorded,
                "remaining_minutes": max(estimated - recorded, 0) if estimated else None,
                "schedule_count": int(metric.get("schedule_count", 0) or 0),
                "next_schedule_date": metric.get("next_date"),
                "next_schedule_label": _fmt_date(metric.get("next_date")),
                "updated_at": _fmt_dt(task.updated_at),
                "source_type": task.source_type or "",
                "source_reference": task.source_reference or "",
                "source_label": {"email": "Correo", "inbound": "Entrada"}.get(task.source_type or "", "Manual"),
            }
        )
    summary = {
        "total": len(rows),
        "mine": sum(1 for row in rows if row["assigned_user_id"] == user_id),
        "open": sum(1 for row in rows if row["status"] in ACTIVE_TASK_STATUSES),
        "done": sum(1 for row in rows if row["status"] == "done"),
        "planned_hours": round(sum(row["estimated_minutes"] for row in rows) / 60, 1),
        "recorded_hours": round(sum(row["recorded_minutes"] for row in rows) / 60, 1),
    }
    filters = {
        "scope": scope,
        "status": status,
        "project_id": project_id,
        "search": search,
    }
    return {
        "tasks": rows,
        "pagination": pagination,
        "summary": summary,
        "filters": filters,
        "users": users,
        "customers": customers,
        "projects": projects,
        "status_options": [(key, label) for key, label in TASK_STATUS_LABELS.items()],
        "priority_options": [(key, label) for key, label in TASK_PRIORITY_LABELS.items()],
        "active_timer": active_timer,
    }


def build_task_detail(db: Session, company_id: int, task_id: int, *, user_id: int | None = None) -> dict[str, Any] | None:
    task = db.scalar(select(Task).where(Task.id == task_id, Task.company_id == company_id))
    if not task:
        return None
    users = _company_users(db, company_id)
    customers = _company_customers(db, company_id)
    projects = _company_projects(db, company_id)
    project_lookup = {project.id: project for project in projects}
    project = project_lookup.get(task.project_id) if task.project_id else None
    metrics = _task_metrics(db, company_id, [task.id]).get(task.id, {})
    schedules = list(
        db.scalars(
            select(TaskSchedule)
            .where(TaskSchedule.company_id == company_id, TaskSchedule.task_id == task.id)
            .order_by(TaskSchedule.scheduled_date.asc(), TaskSchedule.start_time.asc().nulls_last(), TaskSchedule.position.asc(), TaskSchedule.id.asc())
        ).all()
    )
    time_entries = list(
        db.scalars(
            select(TimeEntry)
            .where(TimeEntry.company_id == company_id, TimeEntry.task_id == task.id)
            .order_by(TimeEntry.entry_date.desc(), TimeEntry.id.desc())
        ).all()
    )
    active_timer = _active_timer_snapshot(db, company_id, user_id or 0) if user_id else None
    schedule_rows = [
        {
            "id": schedule.id,
            "scheduled_date": schedule.scheduled_date,
            "scheduled_label": _fmt_date(schedule.scheduled_date),
            "start_time": schedule.start_time or "--",
            "planned_minutes": int(schedule.planned_minutes or 0),
            "planned_label": _human_minutes(schedule.planned_minutes or 0),
            "notes": schedule.notes or "",
            "assigned_user_name": next((user.name for user in users if user.id == schedule.assigned_user_id), "Sin asignar"),
        }
        for schedule in schedules
    ]
    time_rows = [
        {
            "id": entry.id,
            "entry_date": entry.entry_date,
            "entry_label": _fmt_date(entry.entry_date),
            "minutes": int(entry.minutes or 0),
            "minutes_label": _human_minutes(entry.minutes or 0),
            "description": entry.description or "",
            "source": entry.source,
        }
        for entry in time_entries
    ]
    return {
        "task": task,
        "project": project,
        "project_name": project.name if project else "Sin proyecto",
        "client_name": next((customer.fiscal_name for customer in customers if customer.id == project.client_id), "Sin cliente") if project else "Sin cliente",
        "assigned_user_name": next((user.name for user in users if user.id == task.assigned_user_id), "Sin asignar"),
        "recorded_minutes": int(metrics.get("recorded_minutes", 0) or 0),
        "schedule_count": int(metrics.get("schedule_count", 0) or 0),
        "next_schedule_date": metrics.get("next_date"),
        "next_schedule_label": _fmt_date(metrics.get("next_date")),
        "schedules": schedule_rows,
        "time_entries": time_rows,
        "users": users,
        "customers": customers,
        "projects": projects,
        "active_timer": active_timer,
        "status_options": [(key, label) for key, label in TASK_STATUS_LABELS.items()],
        "priority_options": [(key, label) for key, label in TASK_PRIORITY_LABELS.items()],
    }


def build_agenda_page(db: Session, company_id: int, *, user_id: int, days: int = 7) -> dict[str, Any]:
    today = date.today()
    day_keys = [(today + timedelta(days=index)).isoformat() for index in range(max(days, 1))]
    projects = _company_projects(db, company_id)
    project_lookup = {project.id: project for project in projects}
    users = _company_users(db, company_id)
    customer_lookup = {customer.id: customer for customer in _company_customers(db, company_id)}
    schedules = db.execute(
        select(TaskSchedule, Task, Project)
        .join(Task, Task.id == TaskSchedule.task_id)
        .outerjoin(Project, Project.id == Task.project_id)
        .where(
            TaskSchedule.company_id == company_id,
            TaskSchedule.scheduled_date.in_(day_keys),
            or_(TaskSchedule.assigned_user_id == user_id, Task.assigned_user_id == user_id),
            Task.archived_at.is_(None),
        )
        .order_by(TaskSchedule.scheduled_date.asc(), TaskSchedule.start_time.asc().nulls_last(), TaskSchedule.position.asc(), Task.id.asc())
    ).all()
    agenda_days: list[dict[str, Any]] = []
    schedule_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for schedule, task, project in schedules:
        assigned_user = next((user for user in users if user.id == (schedule.assigned_user_id or task.assigned_user_id)), None)
        schedule_by_day[schedule.scheduled_date].append(
            {
                "schedule_id": schedule.id,
                "task_id": task.id,
                "title": task.title,
                "status": task.status,
                "status_label": _label_from_map(task.status, TASK_STATUS_LABELS),
                "priority": task.priority,
                "priority_label": _label_from_map(task.priority, TASK_PRIORITY_LABELS),
                "project_name": project.name if project else "Sin proyecto",
                "client_name": customer_lookup.get(project.client_id).fiscal_name if project and project.client_id in customer_lookup else "Sin cliente",
                "start_time": schedule.start_time or "--",
                "planned_minutes": int(schedule.planned_minutes or 0),
                "planned_label": _human_minutes(schedule.planned_minutes or 0),
                "notes": schedule.notes or "",
                "assigned_user_name": assigned_user.name if assigned_user else "Sin asignar",
            }
        )
    for key in day_keys:
        agenda_days.append(
            {
                "date": key,
                "label": key,
                "items": schedule_by_day.get(key, []),
            }
        )
    unscheduled_tasks = db.scalars(
        select(Task)
        .where(
            Task.company_id == company_id,
            Task.archived_at.is_(None),
            or_(Task.assigned_user_id == user_id, Task.assigned_user_id.is_(None)),
            ~Task.id.in_(select(TaskSchedule.task_id).where(TaskSchedule.company_id == company_id)),
            Task.status.in_(tuple(ACTIVE_TASK_STATUSES)),
        )
        .order_by(Task.due_date.asc().nulls_last(), Task.updated_at.desc())
        .limit(12)
    ).all()
    unscheduled_rows = []
    for task in unscheduled_tasks:
        project = project_lookup.get(task.project_id) if task.project_id else None
        unscheduled_rows.append(
            {
                "id": task.id,
                "title": task.title,
                "status": task.status,
                "status_label": _label_from_map(task.status, TASK_STATUS_LABELS),
                "priority": task.priority,
                "priority_label": _label_from_map(task.priority, TASK_PRIORITY_LABELS),
                "project_name": project.name if project else "Sin proyecto",
                "client_name": customer_lookup.get(project.client_id).fiscal_name if project and project.client_id in customer_lookup else "Sin cliente",
                "due_date": _fmt_date(task.due_date),
                "estimated_minutes": int(task.estimated_minutes or 0),
            }
        )
    time_entries = db.scalars(
        select(TimeEntry)
        .where(TimeEntry.company_id == company_id, TimeEntry.user_id == user_id)
        .order_by(TimeEntry.created_at.desc())
        .limit(8)
    ).all()
    time_rows = [
        {
            "id": entry.id,
            "task_id": entry.task_id,
            "task_title": db.get(Task, entry.task_id).title if db.get(Task, entry.task_id) else "Tarea",
            "entry_date": entry.entry_date,
            "entry_label": _fmt_date(entry.entry_date),
            "minutes": int(entry.minutes or 0),
            "minutes_label": _human_minutes(entry.minutes or 0),
            "description": entry.description or "",
        }
        for entry in time_entries
    ]
    return {
        "agenda_days": agenda_days,
        "unscheduled_tasks": unscheduled_rows,
        "time_entries": time_rows,
        "projects": projects,
        "users": users,
        "active_timer": _active_timer_snapshot(db, company_id, user_id),
        "today": today.isoformat(),
    }


def create_project(
    db: Session,
    *,
    company_id: int,
    name: str,
    description: str = "",
    client_id: int | None = None,
    owner_user_id: int | None = None,
    status: str = "draft",
    priority: str = "normal",
    start_date: str = "",
    due_date: str = "",
    budgeted_minutes: int = 0,
    created_by_user_id: int | None = None,
) -> Project:
    project = Project(
        company_id=company_id,
        name=name.strip(),
        description=description.strip() or None,
        client_id=client_id or None,
        owner_user_id=owner_user_id or None,
        created_by_user_id=created_by_user_id,
        status=status or "draft",
        priority=priority or "normal",
        start_date=start_date or None,
        due_date=due_date or None,
        budgeted_minutes=max(int(budgeted_minutes or 0), 0),
        archived_at=None,
    )
    db.add(project)
    db.flush()
    if owner_user_id:
        db.add(ProjectMember(company_id=company_id, project_id=project.id, user_id=owner_user_id, role="owner"))
    return project


def update_project(
    db: Session,
    project: Project,
    *,
    name: str,
    description: str = "",
    client_id: int | None = None,
    owner_user_id: int | None = None,
    status: str = "",
    priority: str = "",
    start_date: str = "",
    due_date: str = "",
    budgeted_minutes: int = 0,
) -> Project:
    project.name = name.strip()
    project.description = description.strip() or None
    project.client_id = client_id or None
    project.owner_user_id = owner_user_id or None
    project.status = status or project.status
    project.priority = priority or project.priority
    project.start_date = start_date or None
    project.due_date = due_date or None
    project.budgeted_minutes = max(int(budgeted_minutes or 0), 0)
    project.updated_at = datetime.now(timezone.utc)
    if owner_user_id and not db.scalar(
        select(ProjectMember).where(ProjectMember.company_id == project.company_id, ProjectMember.project_id == project.id, ProjectMember.user_id == owner_user_id)
    ):
        db.add(ProjectMember(company_id=project.company_id, project_id=project.id, user_id=owner_user_id, role="owner"))
    return project


def archive_project(project: Project) -> Project:
    project.status = "archived"
    project.archived_at = datetime.now(timezone.utc)
    project.updated_at = datetime.now(timezone.utc)
    return project


def add_project_member(db: Session, *, company_id: int, project_id: int, user_id: int, role: str = "member") -> ProjectMember | None:
    existing = db.scalar(
        select(ProjectMember).where(ProjectMember.company_id == company_id, ProjectMember.project_id == project_id, ProjectMember.user_id == user_id)
    )
    if existing:
        return existing
    member = ProjectMember(company_id=company_id, project_id=project_id, user_id=user_id, role=role or "member")
    db.add(member)
    return member


def create_task(
    db: Session,
    *,
    company_id: int,
    title: str,
    description: str = "",
    project_id: int | None = None,
    assigned_user_id: int | None = None,
    created_by_user_id: int | None = None,
    status: str = "todo",
    priority: str = "normal",
    estimated_minutes: int = 0,
    due_date: str = "",
    source_type: str | None = None,
    source_reference: str | None = None,
    conversation_id: int | None = None,
    inbound_message_id: int | None = None,
) -> Task:
    task = Task(
        company_id=company_id,
        project_id=project_id or None,
        title=title.strip(),
        description=description.strip() or None,
        assigned_user_id=assigned_user_id or None,
        created_by_user_id=created_by_user_id,
        status=status or "todo",
        priority=priority or "normal",
        estimated_minutes=max(int(estimated_minutes or 0), 0),
        due_date=due_date or None,
        source_type=source_type,
        source_reference=source_reference,
        conversation_id=conversation_id,
        inbound_message_id=inbound_message_id,
        archived_at=None,
    )
    db.add(task)
    db.flush()
    return task


def update_task(
    task: Task,
    *,
    title: str,
    description: str = "",
    project_id: int | None = None,
    assigned_user_id: int | None = None,
    status: str = "",
    priority: str = "",
    estimated_minutes: int = 0,
    due_date: str = "",
) -> Task:
    task.title = title.strip()
    task.description = description.strip() or None
    task.project_id = project_id or None
    task.assigned_user_id = assigned_user_id or None
    task.status = status or task.status
    task.priority = priority or task.priority
    task.estimated_minutes = max(int(estimated_minutes or 0), 0)
    task.due_date = due_date or None
    task.completed_at = datetime.now(timezone.utc) if task.status == "done" else None
    task.updated_at = datetime.now(timezone.utc)
    return task


def transition_task(task: Task, status: str) -> Task:
    task.status = status
    task.completed_at = datetime.now(timezone.utc) if status == "done" else None
    task.updated_at = datetime.now(timezone.utc)
    return task


def schedule_task(
    db: Session,
    *,
    company_id: int,
    task: Task,
    scheduled_date: str,
    start_time: str = "",
    planned_minutes: int = 0,
    notes: str = "",
    assigned_user_id: int | None = None,
    created_by_user_id: int | None = None,
) -> TaskSchedule:
    schedule = TaskSchedule(
        company_id=company_id,
        task_id=task.id,
        assigned_user_id=assigned_user_id or task.assigned_user_id,
        scheduled_date=scheduled_date,
        start_time=start_time or None,
        planned_minutes=max(int(planned_minutes or 0), 0),
        notes=notes.strip() or None,
        position=db.scalar(select(func.coalesce(func.max(TaskSchedule.position), 0) + 1).where(TaskSchedule.company_id == company_id, TaskSchedule.task_id == task.id)) or 1,
        created_by_user_id=created_by_user_id,
    )
    db.add(schedule)
    db.flush()
    return schedule


def delete_schedule(schedule: TaskSchedule) -> None:
    schedule_id = schedule.id
    schedule = schedule  # noqa: PLW2901
    del schedule_id


def add_time_entry(
    db: Session,
    *,
    company_id: int,
    task: Task,
    user_id: int,
    entry_date: str,
    minutes: int,
    description: str = "",
    project_id: int | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    source: str = "manual",
    created_by_user_id: int | None = None,
) -> TimeEntry:
    entry = TimeEntry(
        company_id=company_id,
        task_id=task.id,
        project_id=project_id or task.project_id,
        user_id=user_id,
        entry_date=entry_date,
        started_at=started_at,
        ended_at=ended_at,
        minutes=max(int(minutes or 0), 0),
        description=description.strip() or None,
        source=source,
        created_by_user_id=created_by_user_id,
    )
    db.add(entry)
    db.flush()
    return entry


def update_time_entry(entry: TimeEntry, *, entry_date: str, minutes: int, description: str = "") -> TimeEntry:
    entry.entry_date = entry_date
    entry.minutes = max(int(minutes or 0), 0)
    entry.description = description.strip() or None
    entry.updated_at = datetime.now(timezone.utc)
    return entry


def start_timer(db: Session, *, company_id: int, user_id: int, task: Task, notes: str = "", created_by_user_id: int | None = None) -> ActiveTimer:
    timer = _timer_for_user(db, company_id, user_id)
    now = datetime.now(timezone.utc)
    if timer:
        timer.task_id = task.id
        timer.started_at = now
        timer.last_heartbeat_at = now
        timer.notes = notes.strip() or None
        timer.updated_at = now
        return timer
    timer = ActiveTimer(
        company_id=company_id,
        user_id=user_id,
        task_id=task.id,
        started_at=now,
        last_heartbeat_at=now,
        notes=notes.strip() or None,
        created_by_user_id=created_by_user_id,
    )
    db.add(timer)
    db.flush()
    return timer


def stop_timer(db: Session, *, company_id: int, user_id: int, created_by_user_id: int | None = None) -> TimeEntry | None:
    timer = _timer_for_user(db, company_id, user_id)
    if not timer:
        return None
    now = datetime.now(timezone.utc)
    task = db.get(Task, timer.task_id)
    if not task:
        db.delete(timer)
        return None
    started_at = _ensure_aware(timer.started_at) or now
    minutes = max(int((now - started_at).total_seconds() // 60), 1)
    entry = add_time_entry(
        db,
        company_id=company_id,
        task=task,
        user_id=user_id,
        entry_date=now.date().isoformat(),
        minutes=minutes,
        description=timer.notes or f"Tiempo imputado en {task.title}",
        project_id=task.project_id,
        started_at=started_at,
        ended_at=now,
        source="timer",
        created_by_user_id=created_by_user_id,
    )
    db.delete(timer)
    return entry


def cancel_timer(db: Session, *, company_id: int, user_id: int) -> None:
    timer = _timer_for_user(db, company_id, user_id)
    if timer:
        db.delete(timer)


def create_task_from_source(
    db: Session,
    *,
    company_id: int,
    source_kind: str,
    source_id: int,
    title: str = "",
    description: str = "",
    project_id: int | None = None,
    assigned_user_id: int | None = None,
    status: str = "todo",
    priority: str = "normal",
    estimated_minutes: int = 0,
    due_date: str = "",
    created_by_user_id: int | None = None,
) -> tuple[Task, bool]:
    conditions = [Task.company_id == company_id, Task.source_type == source_kind, Task.source_reference == str(source_id)]
    existing = db.scalar(select(Task).where(*conditions))
    if not existing and source_kind == "inbound":
        existing = db.scalar(
            select(Task).where(
                Task.company_id == company_id,
                Task.inbound_message_id == source_id,
            )
        )
    if existing:
        return existing, False
    source_title = title.strip()
    source_description = description.strip()
    conversation_id = None
    inbound_message_id = None
    if source_kind == "email":
        email = db.get(Email, source_id)
        if email and email.company_id == company_id:
            source_title = source_title or email.subject or "Tarea desde correo"
            if not source_description:
                source_description = email.body or email.extracted_text or ""
            conversation_id = email.conversation_id
    else:
        message = db.get(InboundMessage, source_id)
        if message and message.company_id == company_id:
            source_title = source_title or message.subject or "Tarea desde entrada"
            if not source_description:
                source_description = message.original_content or message.normalized_text or ""
            conversation_id = message.conversation_id
            inbound_message_id = message.id
    task = create_task(
        db,
        company_id=company_id,
        title=source_title or "Tarea nueva",
        description=source_description,
        project_id=project_id,
        assigned_user_id=assigned_user_id,
        created_by_user_id=created_by_user_id,
        status=status,
        priority=priority,
        estimated_minutes=estimated_minutes,
        due_date=due_date,
        source_type=source_kind,
        source_reference=str(source_id),
        conversation_id=conversation_id,
        inbound_message_id=inbound_message_id,
    )
    return task, True
