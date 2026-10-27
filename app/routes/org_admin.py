from __future__ import annotations
from pathlib import Path
from typing import Optional, Dict

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from datetime import datetime, timedelta, timezone

from app.services.auth import get_db, require_org_admin, get_org_for_action, require_org_user
from app.models.organization import Organization
from app.models.department import Department
from app.models.user import User
from app.models.user_department import UserDepartment
from app.models.task import Task
from app.models.task_history import TaskHistory

router = APIRouter(prefix="/org", tags=["Org Admin"])

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# --- простейший flash через сессию ---
def _flash(request: Request, msg: str, kind: str = "success") -> None:
    request.session.setdefault("_flash", {})
    request.session["_flash"][kind] = msg

def _take_flash(request: Request) -> tuple[Optional[str], Optional[str]]:
    data = request.session.pop("_flash", None) or {}
    return data.get("success"), data.get("error")

def _back_url(request: Request, default: str) -> str:
    return request.query_params.get("back") or request.headers.get("referer") or default

def _ensure_default_department(db: Session, org_id: int) -> Department:
    dept = db.query(Department).filter(
        Department.org_id == org_id,
        Department.name == "Основное",
        Department.archived_at.is_(None)
    ).first()
    if dept:
        return dept
    dept = Department(org_id=org_id, name="Основное")
    db.add(dept); db.flush()
    return dept

def _user_department_ids(db: Session, user_id: int) -> set[int]:
    rows = db.query(UserDepartment).filter(UserDepartment.user_id == user_id).all()
    return {r.department_id for r in rows}

# --- Dashboard ---
@router.get("", response_class=HTMLResponse)
def org_home(
    request: Request,
    user: User = Depends(require_org_user),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    is_admin = user.is_system_admin or user.org_role == "ORG_ADMIN"
    my_dept_ids = _user_department_ids(db, user.id)

    # База видимости задач
    base = db.query(Task).filter(Task.org_id == org.id)
    if not is_admin:
        if my_dept_ids:
            base = base.filter(or_(
                Task.assignee_user_id == user.id,
                Task.department_id.in_(my_dept_ids),
            ))
        else:
            base = base.filter(Task.assignee_user_id == user.id)

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1) - timedelta(microseconds=1)

    # KPI
    total_open = base.filter(Task.status == "OPEN").count()
    overdue = base.filter(Task.status == "OPEN", Task.due_date.isnot(None), Task.due_date < now).count()
    due_today = base.filter(Task.status == "OPEN", Task.due_date >= today_start, Task.due_date <= today_end).count()
    closed_7d = base.filter(Task.status == "DONE", Task.updated_at >= now - timedelta(days=7)).count()

    # Диаграмма статусов (в рамках видимости)
    by_status = dict(
        base.with_entities(Task.status, func.count())
            .group_by(Task.status).all()
    )

    # Приоритеты (только открытые)
    by_priority_open = dict(
        base.filter(Task.status == "OPEN")
            .with_entities(Task.priority, func.count())
            .group_by(Task.priority).all()
    )

    # Нагрузка по отделам (только открытые, top 8)
    dept_rows = (
        base.filter(Task.status == "OPEN", Task.department_id.isnot(None))
            .with_entities(Task.department_id, func.count())
            .group_by(Task.department_id)
            .order_by(func.count().desc())
            .limit(8).all()
    )
    dept_map = {d.id: d.name for d in db.query(Department).filter(Department.org_id == org.id).all()}
    depts_labels = [dept_map.get(did, f"Отдел {did}") for did, _ in dept_rows]
    depts_values = [cnt for _, cnt in dept_rows]

    # Нагрузка по пользователям (только открытые, top 10)
    user_q = base.filter(Task.status == "OPEN", Task.assignee_user_id.isnot(None))
    if not is_admin and my_dept_ids:
        # коллеги по моим отделам + я
        col_ids = [uid for (uid,) in db.query(UserDepartment.user_id)
                                      .filter(UserDepartment.department_id.in_(my_dept_ids))
                                      .distinct().all()] + [user.id]
        user_q = user_q.filter(Task.assignee_user_id.in_(col_ids))
    user_rows = (
        user_q.with_entities(Task.assignee_user_id, func.count())
              .group_by(Task.assignee_user_id)
              .order_by(func.count().desc())
              .limit(10).all()
    )
    user_map = {u.id: (u.short_name or u.full_name or u.email) for u in db.query(User).filter(User.org_id == org.id).all()}
    users_labels = [user_map.get(uid, f"User {uid}") for uid, _ in user_rows]
    users_values = [cnt for _, cnt in user_rows]

    # Тренд закрытий за 14 дней
    trend_start = today_start - timedelta(days=13)
    d = func.date_trunc('day', Task.updated_at).label('d')  # один объект на SELECT/GROUP/ORDER
    trend_rows = (
        base.filter(Task.status == "DONE", Task.updated_at >= trend_start)
            .with_entities(d, func.count())
            .group_by(d)
            .order_by(d.asc())
            .all()
    )

    # нормализуем пустые дни
    days = [trend_start + timedelta(days=i) for i in range(14)]
    trend_map = {d_.date().isoformat(): 0 for d_ in days}
    for day, cnt in trend_rows:
        trend_map[day.date().isoformat()] = cnt

    # История (последние 15 событий в рамках видимости)
    history_q = db.query(TaskHistory).join(Task, Task.id == TaskHistory.task_id).filter(Task.org_id == org.id)
    if not is_admin:
        history_q = history_q.filter(or_(
            Task.assignee_user_id == user.id,
            Task.department_id.in_(my_dept_ids) if my_dept_ids else False,
        ))
    history = history_q.order_by(TaskHistory.id.desc()).limit(15).all()

    return templates.TemplateResponse("org/dashboard.html", {
        "request": request,
        "org": org,
        "user": user,
        "kpi": {
            "total_open": total_open,
            "overdue": overdue,
            "due_today": due_today,
            "closed_7d": closed_7d,
        },
        "by_status": by_status,
        "by_priority_open": by_priority_open,
        "depts_labels": depts_labels,
        "depts_values": depts_values,
        "users_labels": users_labels,
        "users_values": users_values,
        "trend_labels": list(trend_map.keys()),
        "trend_values": list(trend_map.values()),
        "history": history,
    })

# --- Список подразделений ---
@router.get("/departments", response_class=HTMLResponse)
def list_departments(
    request: Request,
    user: User = Depends(require_org_admin),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    depts = db.query(Department).filter(Department.org_id == org.id).order_by(Department.id.asc()).all()
    flash_success, flash_error = _take_flash(request)
    return templates.TemplateResponse(
        "org/departments.html",
        {"request": request, "org": org, "user": user, "depts": depts,
         "back_url": _back_url(request, "/org"),
         "flash_success": flash_success, "flash_error": flash_error}
    )

# --- Создать подразделение ---
@router.post("/departments/new")
def create_department(
    request: Request,
    name: str = Form(...),
    user: User = Depends(require_org_admin),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    name = (name or "").strip()
    if not name:
        _flash(request, "Название пустое", "error")
        return RedirectResponse(url=f"/org/departments", status_code=303)

    exists = db.query(Department).filter(
        Department.org_id == org.id,
        Department.name == name,
        Department.archived_at.is_(None),
    ).first()
    if exists:
        _flash(request, "Подразделение с таким названием уже есть", "error")
        return RedirectResponse(url="/org/departments", status_code=303)

    dept = Department(org_id=org.id, name=name)
    db.add(dept); db.commit()
    _flash(request, "Подразделение создано")
    return RedirectResponse(url="/org/departments", status_code=303)

# --- Переименовать подразделение ---
@router.post("/departments/{dept_id}/rename")
def rename_department(
    request: Request,
    dept_id: int,
    name: str = Form(...),
    user: User = Depends(require_org_admin),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    dept = db.query(Department).filter(Department.id == dept_id, Department.org_id == org.id).first()
    if not dept:
        _flash(request, "Подразделение не найдено", "error")
        return RedirectResponse(url="/org/departments", status_code=303)

    name = (name or "").strip()
    if not name:
        _flash(request, "Название пустое", "error")
        return RedirectResponse(url="/org/departments", status_code=303)

    dup = db.query(Department).filter(
        Department.org_id == org.id,
        Department.name == name,
        Department.archived_at.is_(None),
        Department.id != dept.id
    ).first()
    if dup:
        _flash(request, "Такое название уже занято", "error")
        return RedirectResponse(url="/org/departments", status_code=303)

    dept.name = name
    db.commit()
    _flash(request, "Название обновлено")
    return RedirectResponse(url="/org/departments", status_code=303)

# --- Архивировать (с переносом пользователей в «Основное») ---
@router.post("/departments/{dept_id}/archive")
def archive_department(
    request: Request,
    dept_id: int,
    user: User = Depends(require_org_admin),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    dept = db.query(Department).filter(Department.id == dept_id, Department.org_id == org.id).first()
    if not dept:
        _flash(request, "Подразделение не найдено", "error")
        return RedirectResponse(url="/org/departments", status_code=303)

    # нельзя архивировать последнее активное
    active_count = db.query(Department).filter(Department.org_id == org.id, Department.archived_at.is_(None)).count()
    if active_count <= 1:
        _flash(request, "Нельзя архивировать последнее подразделение", "error")
        return RedirectResponse(url="/org/departments", status_code=303)

    # перенос участников
    default_dept = _ensure_default_department(db, org.id)
    links = db.query(UserDepartment).filter(UserDepartment.department_id == dept.id).all()
    for link in links:
        # если у пользователя уже есть "Основное" — просто удалим связь
        already = db.query(UserDepartment).filter(
            UserDepartment.user_id == link.user_id,
            UserDepartment.department_id == default_dept.id
        ).first()
        if not already:
            db.add(UserDepartment(user_id=link.user_id, department_id=default_dept.id))
        db.delete(link)

    from sqlalchemy import func as sa_func
    from datetime import datetime, timezone
    dept.archived_at = datetime.now(tz=timezone.utc)

    db.commit()
    _flash(request, "Подразделение архивировано, сотрудники перенесены в «Основное»")
    return RedirectResponse(url="/org/departments", status_code=303)
