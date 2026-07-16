from __future__ import annotations

import gc
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import os
import sys

os.environ.setdefault("APP_ENV", "test")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth.dependencies import current_user  # noqa: E402
from app.core import middleware as core_middleware  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import Company, Customer, Email, InboundMessage, InputChannel, Project, ProjectMember, Role, Task, TaskSchedule, TimeEntry, User  # noqa: E402
from app.main import app  # noqa: E402
from app.master.database import MasterBase  # noqa: E402
from app.master.models import MasterCompany, MasterTenantDatabase  # noqa: E402
from app.master.service import TenantRole, TenantUser  # noqa: E402
from app.projects.service import add_project_member, add_time_entry, build_agenda_page, build_projects_page, build_task_detail, build_tasks_page, create_project, create_task_from_source, schedule_task, start_timer, stop_timer  # noqa: E402
from app.tenancy.database import get_tenant_db  # noqa: E402


class ProjectsTasksAgendaTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        self.master_path = base / "master.sqlite"
        self.tenant_path = base / "tenant.sqlite"
        self.master_engine = create_engine(f"sqlite:///{self.master_path.as_posix()}", connect_args={"check_same_thread": False})
        self.tenant_engine = create_engine(f"sqlite:///{self.tenant_path.as_posix()}", connect_args={"check_same_thread": False})
        MasterBase.metadata.create_all(self.master_engine)
        Base.metadata.create_all(self.tenant_engine)
        self.MasterSession = sessionmaker(bind=self.master_engine, autoflush=False, autocommit=False)
        self.TenantSession = sessionmaker(bind=self.tenant_engine, autoflush=False, autocommit=False)
        self._patcher = patch.object(core_middleware, "MasterSessionLocal", self.MasterSession)
        self._patcher.start()
        self.user = TenantUser(
            id=1,
            email="admin@anchi.local",
            name="Administrador demo",
            is_active=True,
            company_id=1,
            company_name="Demo",
            company_slug="demo",
            role=TenantRole(name="Administrador", permissions=""),
            membership_id=1,
            database_url=f"sqlite:///{self.tenant_path.as_posix()}",
        )
        app.dependency_overrides[current_user] = lambda: self.user

        def override_get_tenant_db():
            db = self.TenantSession()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_tenant_db] = override_get_tenant_db
        self.client = TestClient(app)
        self._seed()

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        self._patcher.stop()
        self.master_engine.dispose()
        self.tenant_engine.dispose()
        gc.collect()
        self.tempdir.cleanup()

    def _seed(self):
        master_db = self.MasterSession()
        master_db.add_all(
            [
                MasterCompany(id=1, name="Demo", slug="demo", active=True),
                MasterTenantDatabase(company_id=1, database_key="demo", database_url=f"sqlite:///{self.tenant_path.as_posix()}", is_active=True, health_status="ok"),
            ]
        )
        master_db.commit()
        master_db.close()

        db = self.TenantSession()
        db.add_all(
            [
                Company(id=1, name="Demo", legal_name="Demo SL", active=True),
                Role(id=1, company_id=1, name="Administrador", permissions=""),
                Role(id=2, company_id=1, name="Comercial", permissions=""),
                User(id=1, company_id=1, role_id=1, email="admin@anchi.local", name="Administrador demo", password_hash="hash", is_active=True),
                User(id=2, company_id=1, role_id=2, email="user@anchi.local", name="Usuario demo", password_hash="hash", is_active=True),
                Customer(id=1, company_id=1, code="C001", fiscal_name="Cliente Demo SL", commercial_name="Cliente Demo", status="active"),
                InputChannel(id=1, company_id=1, key="email", name="Email", channel_type="email", is_active=True, is_default=True),
            ]
        )
        db.add(
            Email(
                id=1,
                company_id=1,
                external_id="mail-1",
                sender="cliente@example.com",
                subject="Pedido urgente",
                body="Necesitamos 8 unidades.",
                received_at=datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc),
                status="pending",
                agent_status="not_processed",
            )
        )
        db.add(
            InboundMessage(
                id=1,
                company_id=1,
                channel_id=1,
                provider="imap",
                source_external_id="msg-1",
                sender="cliente@example.com",
                recipient="pedidos@example.com",
                subject="Entrada manual",
                original_content="Mensaje para tarea",
                content_type="text/plain",
                received_at=datetime(2026, 7, 16, 9, 30, tzinfo=timezone.utc),
                status="received",
            )
        )
        db.commit()
        db.close()

    def test_end_to_end_project_task_agenda_flow(self):
        response = self.client.get("/projects")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Proyectos", response.text)

        create_project_response = self.client.post(
            "/projects",
            data={
                "name": "Proyecto Alfa",
                "description": "Proyecto de ejemplo",
                "client_id": 1,
                "owner_user_id": 1,
                "status": "active",
                "priority": "high",
                "start_date": "2026-07-16",
                "due_date": "2026-08-01",
                "budgeted_minutes": 480,
            },
            allow_redirects=False,
        )
        self.assertEqual(create_project_response.status_code, 303)

        db = self.TenantSession()
        project = db.scalar(select(Project).where(Project.company_id == 1).order_by(Project.id.desc()))
        self.assertIsNotNone(project)
        db.close()

        member_response = self.client.post(f"/projects/{project.id}/members", data={"user_id": 2, "role": "member"}, allow_redirects=False)
        self.assertEqual(member_response.status_code, 303)

        task_response = self.client.post(
            "/tasks",
            data={
                "title": "Revisar pedido de cliente",
                "description": "Tarea principal del proyecto",
                "project_id": project.id,
                "assigned_user_id": 1,
                "status": "todo",
                "priority": "normal",
                "estimated_minutes": 90,
                "due_date": "2026-07-20",
            },
            allow_redirects=False,
        )
        self.assertEqual(task_response.status_code, 303)
        db = self.TenantSession()
        task = db.scalar(select(Task).where(Task.company_id == 1, Task.project_id == project.id).order_by(Task.id.desc()))
        self.assertIsNotNone(task)

        schedule_response = self.client.post(
            f"/tasks/{task.id}/schedule",
            data={
                "scheduled_date": "2026-07-16",
                "start_time": "09:30",
                "planned_minutes": 60,
                "notes": "Bloque de revisión",
                "assigned_user_id": 1,
            },
            allow_redirects=False,
        )
        self.assertEqual(schedule_response.status_code, 303)

        time_response = self.client.post(
            f"/tasks/{task.id}/time-entries",
            data={
                "entry_date": "2026-07-16",
                "minutes": 45,
                "description": "Corrección inicial",
            },
            allow_redirects=False,
        )
        self.assertEqual(time_response.status_code, 303)

        start_response = self.client.post(f"/tasks/{task.id}/timer/start", data={"notes": "Bloque de foco"}, allow_redirects=False)
        self.assertEqual(start_response.status_code, 303)
        stop_response = self.client.post(f"/tasks/{task.id}/timer/stop", allow_redirects=False)
        self.assertEqual(stop_response.status_code, 303)

        detail = self.client.get(f"/tasks/{task.id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Horas", detail.text)
        self.assertIn("Programación", detail.text)

        project_detail = self.client.get(f"/projects/{project.id}")
        self.assertEqual(project_detail.status_code, 200)
        self.assertIn("Proyecto Alfa", project_detail.text)
        self.assertIn("Miembros", project_detail.text)

        agenda = self.client.get("/agenda")
        self.assertEqual(agenda.status_code, 200)
        self.assertIn("Agenda", agenda.text)
        self.assertIn("Sin planificar", agenda.text)

        tasks_page = self.client.get("/tasks")
        self.assertEqual(tasks_page.status_code, 200)
        self.assertIn("Tareas", tasks_page.text)
        self.assertIn("Revisar pedido de cliente", tasks_page.text)

        db.close()
        db = self.TenantSession()
        self.assertEqual(db.scalar(select(func.count()).select_from(ProjectMember).where(ProjectMember.project_id == project.id)) or 0, 2)
        self.assertEqual(db.scalar(select(func.count()).select_from(TimeEntry).where(TimeEntry.task_id == task.id)) or 0, 2)
        self.assertEqual(db.scalar(select(func.count()).select_from(TaskSchedule).where(TaskSchedule.task_id == task.id)) or 0, 1)
        db.close()

    def test_source_task_creation_is_deduplicated(self):
        db = self.TenantSession()
        task, created = create_task_from_source(
            db,
            company_id=1,
            source_kind="email",
            source_id=1,
            title="",
            description="",
            assigned_user_id=1,
            created_by_user_id=1,
            estimated_minutes=30,
        )
        self.assertTrue(created)
        duplicate, created_again = create_task_from_source(
            db,
            company_id=1,
            source_kind="email",
            source_id=1,
            title="",
            description="",
            assigned_user_id=1,
            created_by_user_id=1,
            estimated_minutes=30,
        )
        self.assertFalse(created_again)
        self.assertEqual(task.id, duplicate.id)
        self.assertEqual(db.scalar(select(func.count()).select_from(Task).where(Task.company_id == 1, Task.source_type == "email", Task.source_reference == "1")) or 0, 1)
        db.close()

    def test_default_views_are_scoped_to_current_user_and_company(self):
        db = self.TenantSession()
        project_a = create_project(db, company_id=1, name="Proyecto A", client_id=1, owner_user_id=1, created_by_user_id=1)
        db.add(Company(id=2, name="Otra", legal_name="Otra SL", active=True))
        project_b = Project(company_id=2, name="Proyecto B", status="active", priority="normal", budgeted_minutes=120)
        db.add(project_b)
        db.flush()
        task_mine = create_task_from_source(
            db,
            company_id=1,
            source_kind="inbound",
            source_id=1,
            title="Tarea mía",
            description="",
            project_id=project_a.id,
            assigned_user_id=1,
            created_by_user_id=1,
            estimated_minutes=60,
        )[0]
        task_other = create_task_from_source(
            db,
            company_id=1,
            source_kind="inbound",
            source_id=999,
            title="Tarea ajena",
            description="",
            project_id=project_a.id,
            assigned_user_id=2,
            created_by_user_id=1,
            estimated_minutes=60,
        )[0]
        db.add_all(
            [
                Task(company_id=2, project_id=project_b.id, title="Tarea otra compañía", status="todo", priority="normal", assigned_user_id=None, created_by_user_id=1, estimated_minutes=30),
            ]
        )
        db.commit()

        projects_page = build_projects_page(db, company_id=1)
        self.assertEqual(projects_page["summary"]["total"], 1)
        self.assertEqual(projects_page["projects"][0]["name"], "Proyecto A")

        tasks_page = build_tasks_page(db, company_id=1, user_id=1, scope="mine")
        task_ids = {task["id"] for task in tasks_page["tasks"]}
        self.assertIn(task_mine.id, task_ids)
        self.assertNotIn(task_other.id, task_ids)

        agenda = build_agenda_page(db, company_id=1, user_id=1)
        self.assertEqual(len(agenda["agenda_days"]), 7)
        db.close()
