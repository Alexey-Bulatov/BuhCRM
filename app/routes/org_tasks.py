# app/routes/org_tasks.py
from __future__ import annotations

import re
import calendar
from pathlib import Path
from typing import Optional, List
from datetime import datetime, timezone, timedelta, date

import sqlalchemy as sa
from dateutil.relativedelta import relativedelta
from fastapi import APIRouter, Depends, Form, Query, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, case, and_
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.services.auth import require_org_user, require_org_admin, get_org_for_action
from app.models.organization import Organization
from app.models.user import User
from app.models.department import Department
from app.models.user_department import UserDepartment
from app.models.task import Task
from app.models.task_history import TaskHistory
from app.core.enums import TaskPriority
from app.models.company import Company

router = APIRouter(prefix="/org", tags=["Org Tasks"])
TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# ---- дата в формате ДД.ММ.ГГГГ для вывода
def ru_date(value):
    if not value:
        return "—"
    if isinstance(value, str):
        # попробуем ISO
        try:
            dt = datetime.fromisoformat(value)
            return dt.strftime("%d.%m.%Y")
        except Exception:
            return value
    try:
        return value.strftime("%d.%m.%Y")
    except Exception:
        return str(value)

templates.env.filters["ru_date"] = ru_date  # доступно во всех шаблонах

def _company_label(c: Company) -> str:
    base = (c.short_name or getattr(c, "full_name", "") or getattr(c, "name", "") or "").strip()
    inn = getattr(c, "inn", None)
    return f"{base} ({inn})" if (base and inn) else base

# --- flash helpers ---
def _flash(request: Request, msg: str, kind: str = "success") -> None:
    request.session.setdefault("_flash", {})
    request.session["_flash"][kind] = msg

def _take_flash(request: Request) -> tuple[Optional[str], Optional[str]]:
    data = request.session.pop("_flash", None) or {}
    return data.get("success"), data.get("error")

def _back_url(request: Request, default: str) -> str:
    return request.query_params.get("back") or request.headers.get("referer") or default

def _user_department_ids(db: Session, user_id: int) -> set[int]:
    rows = db.query(UserDepartment).filter(UserDepartment.user_id == user_id).all()
    return {r.department_id for r in rows}

# --- helpers ---
def _priority_order_expr():
    return case(
        (Task.priority == "URGENT", 0),
        (Task.priority == "MEDIUM", 1),
        (Task.priority == "LONGTERM", 2),
        else_=3,
    )

def _auto_priority(due: Optional[datetime]) -> str:
    if not due:
        return TaskPriority.LONGTERM.value
    now = datetime.now(timezone.utc)
    if due <= now.replace(hour=23, minute=59, second=59, microsecond=0):
        return TaskPriority.URGENT.value
    if due <= now + timedelta(days=3):
        return TaskPriority.MEDIUM.value
    return TaskPriority.LONGTERM.value

def _is_quarter_end(dt: datetime) -> bool:
    q_last_month = ((dt.month - 1) // 3 + 1) * 3  # 3,6,9,12
    last_day = calendar.monthrange(dt.year, q_last_month)[1]
    return dt.month == q_last_month and dt.day == last_day

def _to_quarter_end_like(dt: datetime) -> datetime:
    q_last_month = ((dt.month - 1) // 3 + 1) * 3
    last_day = calendar.monthrange(dt.year, q_last_month)[1]
    return dt.replace(month=q_last_month, day=last_day)

def _is_year_end(dt: datetime) -> bool:
    return dt.month == 12 and dt.day == 31

def _to_year_end_like(dt: datetime) -> datetime:
    return dt.replace(month=12, day=31)

def _next_due_date(due: Optional[datetime], freq: str, interval: int) -> Optional[datetime]:
    """
    QUARTERLY/YEARLY — «бухгалтерские»:
      - если due == конец квартала -> следующий тоже конец квартала (+3м * interval)
      - если due == 31.12 -> следующий 31.12 (+1г * interval)
      - иначе обычный сдвиг на месяцы/годы с сохранением дня и времени
    """
    if not due:
        return None

    # Сохраняем час/мин/сек/микросек и tz
    def keep_time(base: datetime, new_like: datetime) -> datetime:
        return new_like.replace(
            hour=base.hour, minute=base.minute, second=base.second, microsecond=base.microsecond, tzinfo=base.tzinfo
        )

    if freq == "DAILY":
        return due + timedelta(days=interval)

    if freq == "WEEKLY":
        return due + timedelta(weeks=interval)

    if freq == "MONTHLY":
        return due + relativedelta(months=interval)

    if freq == "QUARTERLY":
        # «квартальный» шаг
        candidate = due + relativedelta(months=3 * interval)
        if _is_quarter_end(due):
            # привязываем к концу целевого квартала
            return keep_time(due, _to_quarter_end_like(candidate))
        return candidate

    if freq == "YEARLY":
        candidate = due + relativedelta(years=interval)
        if _is_year_end(due):
            return keep_time(due, _to_year_end_like(candidate))
        return candidate

    return None  # NONE

def _extract_company_id_from_referer(request: Request) -> Optional[int]:
    ref = request.headers.get("referer") or ""
    m = re.search(r"/org/clients/(\d+)(?:/|$)", ref)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None

def _visibility_scope_expr(db: Session, user: User):
    is_admin = bool(getattr(user, "is_system_admin", False) or getattr(user, "org_role", "") == "ORG_ADMIN")
    if is_admin:
        return sa.true()

    my_depts = _user_department_ids(db, user.id)
    if not my_depts:
        # видит только своё
        return (Task.assignee_user_id == user.id)

    assignees_in_my_depts_sq = (
        db.query(UserDepartment.user_id)
          .filter(UserDepartment.department_id.in_(my_depts))
          .subquery()
    )

    return sa.or_(
        Task.assignee_user_id == user.id,
        sa.and_(Task.department_id.isnot(None), Task.department_id.in_(my_depts)),
        Task.assignee_user_id.in_(assignees_in_my_depts_sq),
    )

# ---- универсальный парсер дат для ввода (ДД.ММ.ГГГГ[, HH:MM] | ISO)
def _parse_datetime_input(s: str, date_only_default_hour: int = 23, date_only_default_minute: int = 59) -> Optional[datetime]:
    """Return None for malformed dates, including impossible calendar dates."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        match = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})(?:\s+(\d{2}):(\d{2}))?", s)
        if match:
            day, month, year = (int(match.group(i)) for i in (1, 2, 3))
            hour = int(match.group(4)) if match.group(4) else date_only_default_hour
            minute = int(match.group(5)) if match.group(5) else date_only_default_minute
            return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            value = date.fromisoformat(s)
            return datetime(value.year, value.month, value.day,
                            date_only_default_hour, date_only_default_minute, tzinfo=timezone.utc)
        value = datetime.fromisoformat(s)
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError):
        return None

# ----------------- LIST -----------------
@router.get("/tasks/list", response_class=HTMLResponse)
def tasks_list(
    request: Request,
    user: User = Depends(require_org_user),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
    only: str | None = Query(None, description="mine|dept|all"),
    q: str | None = Query(None),
    status: str | None = Query(None),   # OPEN|DONE|CANCELED
    sort: str | None = Query("due"),    # title|status|priority|due|company|assignee|id
    dir: str | None = Query("asc"),     # asc|desc
):
    org: Organization = get_org_for_action(db, user, org_id)

    # дефолтный таб — "назначены на меня"
    if only is None:
        params = {"only": "mine"}
        if q: params["q"] = q
        if status: params["status"] = status
        if sort: params["sort"] = sort
        if dir: params["dir"] = dir
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return RedirectResponse(url=f"{request.url.path}?{qs}", status_code=303)

    base = (
        db.query(Task)
        .options(joinedload(Task.company))
        .filter(Task.org_id == org.id)
    )

    # Поиск
    if q:
        like = f"%{q.strip()}%"
        base = base.filter(or_(Task.title.ilike(like), Task.description.ilike(like)))

    # Фильтр статуса
    if status in ("OPEN", "DONE", "CANCELED"):
        base = base.filter(Task.status == status)

    # ---- ВИДИМОСТЬ (по своим подразделениям, вне зависимости от роли) ----
    my_depts = _user_department_ids(db, user.id)

    # Подзапрос: исполнители, состоящие в моих подразделениях
    assignees_in_my_depts_sq = None
    if my_depts:
        assignees_in_my_depts_sq = (
            db.query(UserDepartment.user_id)
              .filter(UserDepartment.department_id.in_(my_depts))
              .subquery()
        )

    def dept_scope_filter():
        """
        Все задачи моих подразделений:
        - неназначенные, у которых department_id ∈ мои подразделения;
        - назначенные на пользователей, которые состоят в моих подразделениях
          (даже если у задач department_id = NULL).
        """
        if not my_depts:
            # нет привязок к подразделениям — пусто для режима "dept"
            return Task.id == -1

        cond_unassigned = sa.and_(
            Task.department_id.isnot(None),
            Task.department_id.in_(my_depts),
        )
        cond_assignees = Task.assignee_user_id.in_(assignees_in_my_depts_sq)
        return or_(cond_unassigned, cond_assignees)

    if only == "mine":
        base = base.filter(Task.assignee_user_id == user.id)

    elif only == "dept":
        base = base.filter(dept_scope_filter())

    elif only == "dept_unassigned":
        if my_depts:
            base = base.filter(
                Task.assignee_user_id.is_(None),
                Task.department_id.in_(my_depts),
            )
        else:
            base = base.filter(Task.id == -1)

    elif only == "all":
        # «все видимые мне»: мои личные ИЛИ задачи моих подразделений
        base = base.filter(
            or_(
                Task.assignee_user_id == user.id,
                dept_scope_filter(),
            )
        )

    else:
        # неизвестный параметр -> дефолт
        return RedirectResponse(url=f"{request.url.path}?only=mine", status_code=303)

    # --- динамическая сортировка ---
    sort = (sort or "due").lower()
    dir = (dir or "asc").lower()
    is_desc = (dir == "desc")

    def _order_columns():
        # приоритет — «ручной» ключ
        if sort == "priority":
            primary = _priority_order_expr()
            return [primary.desc() if is_desc else primary.asc(), Task.id.desc()]
        # поле -> колонка
        colmap = {
            "title": Task.title,
            "status": Task.status,
            "due": Task.due_date,
            "company": Task.company_id,      # грубо, без join на имя
            "assignee": Task.assignee_user_id,
            "id": Task.id,
        }
        col = colmap.get(sort, Task.due_date)
        cols: list = []
        if col is Task.due_date:
            # пустые сроки вниз при asc
            cols.append(Task.due_date.is_(None))
        cols.append(col.desc() if is_desc else col.asc())
        if col is not Task.id:
            cols.append(Task.id.desc())
        return cols

    def _order(qs):
        return qs.order_by(*_order_columns())

    # Временные границы для раскладки «открытых»
    now = datetime.now(tz=timezone.utc)
    today_end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    tomorrow_end = today_end + timedelta(days=1)

    # Открытые
    open_only = base.filter(Task.status == "OPEN")
    all_open = _order(open_only).all()
    overdue = _order(open_only.filter(Task.due_date.isnot(None), Task.due_date < now)).all()
    upcoming = _order(open_only.filter(Task.due_date.isnot(None), Task.due_date >= now, Task.due_date <= tomorrow_end)).all()
    later = _order(open_only.filter(or_(Task.due_date.is_(None), Task.due_date > tomorrow_end))).all()

    # Завершённые (последние 200)
    closed = (
        base.filter(Task.status == "DONE")
        .order_by(Task.updated_at.desc(), Task.id.desc())
        .limit(200)
        .all()
    )

    counts = {
        "all": len(all_open),
        "overdue": len(overdue),
        "upcoming": len(upcoming),
        "later": len(later),
        "closed": len(closed),
    }

    flash_success, flash_error = _take_flash(request)
    return templates.TemplateResponse(
        "org/tasks.html",
        {
            "request": request,
            "org": org,
            "user": user,
            "all_open": all_open,
            "overdue": overdue,
            "upcoming": upcoming,
            "later": later,
            "closed": closed,
            "counts": counts,
            "only": only,
            "q": q or "",
            "status": status or "",
            "sort": sort,
            "dir": dir,
            "back_url": _back_url(request, "/org"),
            "flash_success": flash_success,
            "flash_error": flash_error,
        },
    )

# ===== КАЛЕНДАРЬ НЕДЕЛИ =====
@router.get("/tasks", response_class=HTMLResponse)
def tasks_main_calendar(
    request: Request,
    user: User = Depends(require_org_user),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    return templates.TemplateResponse(
        "org/tasks_calendar.html",
        {
            "request": request,
            "org": org,
            "user": user,
        },
    )

# ===== АЛИАС СТАРОГО ПУТИ КАЛЕНДАРЯ =====
@router.get("/tasks/calendar", response_class=HTMLResponse)
def tasks_calendar_alias(
    request: Request,
    user: User = Depends(require_org_user),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    return templates.TemplateResponse(
        "org/tasks_calendar.html",
        {"request": request, "org": org, "user": user},
    )

# ===== API для календаря (по диапазону дат) =====
@router.get("/tasks/api")
def tasks_api_range(
    request: Request,
    start: str = Query(..., description="ISO date/time from FullCalendar"),
    end: str = Query(..., description="ISO date/time from FullCalendar"),
    only: str | None = Query(None, description="all | dept | mine"),
    user: User = Depends(require_org_user),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    """
    Возвращает события календаря за видимый диапазон.

    Ключевое:
    - admin_full_view включается ТОЛЬКО если явно передан admin=1.
    - По умолчанию даже админ попадает под «ограниченную» видимость (restricted).
      Можно временно выключить ограничение добавив ?admin=1 (полный админ-вид).
    - Для allDay событий отдаём 'YYYY-MM-DD', чтобы не было сдвига по TZ.
    """
    from sqlalchemy.orm import joinedload
    from dateutil.parser import isoparse
    from datetime import timedelta

    # Параметры режима видимости
    is_admin_user = bool(getattr(user, "is_system_admin", False) or getattr(user, "org_role", "") == "ORG_ADMIN")
    admin_full_view = request.query_params.get("admin") in ("1", "true", "yes")
    # По умолчанию — РЕЖЕМ даже админа; только admin=1 включает «показывать всё»
    effective_is_admin = is_admin_user and admin_full_view

    # Парсим границы и приводим к DATE
    start_dt = isoparse(start)
    end_dt   = isoparse(end)
    end_inclusive = end_dt - timedelta(seconds=1)
    start_date = start_dt.date()
    end_date   = end_inclusive.date()

    org: Organization = get_org_for_action(db, user, org_id)

    q = (
        db.query(Task)
        .options(
            joinedload(Task.company),
            joinedload(Task.assignee),
        )
        .filter(Task.org_id == org.id)
        .filter(Task.status == "OPEN")
        .filter(Task.due_date.isnot(None))
        .filter(sa.func.date(Task.due_date) >= start_date)
        .filter(sa.func.date(Task.due_date) <= end_date)
    )

    # --- ЕДИНАЯ МАСКА ВИДИМОСТИ ---
    if not effective_is_admin:
        my_depts = _user_department_ids(db, user.id)
        if my_depts:
            assignees_in_my_depts_sq = (
                db.query(UserDepartment.user_id)
                  .filter(UserDepartment.department_id.in_(my_depts))
                  .subquery()
            )
            q = q.filter(
                sa.or_(
                    Task.assignee_user_id == user.id,                                   # мои задачи
                    sa.and_(Task.department_id.isnot(None), Task.department_id.in_(my_depts)),     # задачи моих подразделений (неназначенные)
                    Task.assignee_user_id.in_(assignees_in_my_depts_sq),               # назначены на людей из моих подразделений
                )
            )
        else:
            # не в подразделениях — вижу только свои
            q = q.filter(Task.assignee_user_id == user.id)
    # else: effective_is_admin=True → видит всё

    # --- Дополнительная фильтрация по only ---
    if only == "mine":
        q = q.filter(Task.assignee_user_id == user.id)

    elif only == "dept":
        my_depts = _user_department_ids(db, user.id)
        if not my_depts:
            q = q.filter(Task.id == -1)
        else:
            assignees_in_my_depts_sq = (
                db.query(UserDepartment.user_id)
                  .filter(UserDepartment.department_id.in_(my_depts))
                  .subquery()
            )
            q = q.filter(
                sa.or_(
                    sa.and_(Task.department_id.isnot(None), Task.department_id.in_(my_depts)),
                    Task.assignee_user_id.in_(assignees_in_my_depts_sq),
                )
            )
    # only == "all" или None — оставляем как есть (маска уже учла всё)

    rows = q.order_by(Task.due_date.asc(), Task.id.desc()).all()

    def _executor_name(t: Task):
        a = getattr(t, "assignee", None)
        if not a:
            return None
        return getattr(a, "full_name", None) or getattr(a, "email", None) or f"ID {getattr(a,'id', '')}"

    events = []
    for t in rows:
        pr = (t.priority or "").upper()
        if pr == "URGENT":
            color = "#ff6b6b"
        elif pr == "MEDIUM":
            color = "#ffb84d"
        elif pr in ("LOW", "LONG", "LONGTERM"):
            color = "#58b4ff"
        else:
            color = "#adb5bd"

        title_bits = [t.title]
        if t.company:
            name = t.company.short_name or t.company.full_name or t.company.name
            if name:
                title_bits.append(f"— {name}")

        # КЛЮЧ: allDay → отдаём ТОЛЬКО дату без времени/таймзоны
        start_iso = t.due_date.date().isoformat()

        events.append({
            "id": str(t.id),
            "title": " ".join(title_bits),
            "start": start_iso,
            "allDay": True,
            "backgroundColor": color,
            "borderColor": color,
            "extendedProps": {"executor": _executor_name(t)},
        })

    return events

# --- debug: убедимся, что роутер подхватился
@router.get("/tasks/ping")
def _tasks_ping():
    return {"ok": True}

# ----------------- NEW FORM -----------------
@router.get("/tasks/new", response_class=HTMLResponse)
def task_new_form(
    request: Request,
    user: User = Depends(require_org_user),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
    company_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    is_admin = getattr(user, "is_system_admin", False) or getattr(user, "org_role", "") == "ORG_ADMIN"

    # --- Компания (безопасные значения по умолчанию) ---
    selected_company_id: int | None = None
    selected_company_label: str = ""
    if company_id:
        c = (
            db.query(Company)
            .filter(Company.id == company_id, Company.org_id == org.id)
            .first()
        )
        if c:
            selected_company_id = c.id
            name = (getattr(c, "short_name", None) or getattr(c, "full_name", None) or f"Компания #{c.id}")
            inn = (getattr(c, "inn", "") or "").strip()
            selected_company_label = f"{name}{f' ({inn})' if inn else ''}"

    # --- Подразделения ---
    dept_filters = [Department.org_id == org.id]
    if hasattr(Department, "archived_at"):
        dept_filters.append(Department.archived_at.is_(None))
    departments = (
        db.query(Department)
        .filter(*dept_filters)
        .order_by(Department.name.asc())
        .all()
    )

    selected_department_id: int | None = None
    rows = (
        db.query(Department.id)
        .join(UserDepartment, UserDepartment.department_id == Department.id)
        .filter(
            UserDepartment.user_id == user.id,
            Department.org_id == org.id,
            *( [Department.archived_at.is_(None)] if hasattr(Department, "archived_at") else [] )
        )
        .all()
    )
    my_dept_ids = [r[0] for r in rows]
    if len(my_dept_ids) == 1:
        selected_department_id = my_dept_ids[0]

    # --- Пользователи (исполнитель) ---
    users_q = db.query(User).filter(User.org_id == org.id, User.is_active == True)  # noqa: E712
    if not is_admin:
        users_q = users_q.filter(User.id == user.id)  # обычному показываем только себя
        selected_assignee_id = user.id
    else:
        selected_assignee_id = None  # админ сам выберет

    users = users_q.order_by(User.full_name.asc(), User.email.asc()).all()

    return templates.TemplateResponse(
        "org/task_form.html",
        {
            "request": request,
            "org": org,
            "user": user,
            # компания
            "selected_company_id": selected_company_id,
            "selected_company_label": selected_company_label,
            # подразделения
            "departments": departments,
            "depts": departments,  # на всякий случай для старого шаблона
            "selected_department_id": selected_department_id,
            # пользователи
            "users": users,
            "assignees": users,  # на всякий случай для старого шаблона
            "selected_assignee_id": selected_assignee_id,
            # прочее
            "back_url": "/org/tasks",
        },
    )

# ----------------- CREATE -----------------
@router.post("/tasks/new")
def task_new_submit(
    request: Request,
    title: str = Form(..., max_length=255),
    description: str = Form(""),
    due_at: str = Form(""),
    assign_type: str = Form("department"),
    department_id: int = Form(0),
    assignee_id: int = Form(0),
    priority_mode: str = Form("AUTO"),
    priority: str = Form("MEDIUM"),
    recur_freq: str = Form("NONE"),
    recur_interval: int = Form(1),
    recur_until: str = Form(""),
    user: User = Depends(require_org_user),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
    company_id: int = Form(0),
):
    def back_to_form():
        q = []
        if org_id:
            q.append(f"org_id={org_id}")
        if company_id:
            q.append(f"company_id={company_id}")
        qs = ("?" + "&".join(q)) if q else ""
        return RedirectResponse(url=f"/org/tasks/new{qs}", status_code=303)

    if not title.strip():
        _flash(request, "Укажите название задачи", "error")
        return back_to_form()

    org: Organization = get_org_for_action(db, user, org_id)
    is_admin = user.is_system_admin or user.org_role == "ORG_ADMIN"
    my_depts = _user_department_ids(db, user.id)

    # дедлайн — теперь понимаем ДД.ММ.ГГГГ и ДД.ММ.ГГГГ HH:MM
    dt_due: Optional[datetime] = _parse_datetime_input(due_at)
    if (due_at or "").strip() and dt_due is None:
        _flash(request, "Неверный формат даты срока", "error")
        return back_to_form()

    # recur_until (оставляем ISO-дата)
    dt_until: Optional[date] = None
    if recur_until.strip():
        try:
            dt_until = date.fromisoformat(recur_until.strip())
        except Exception:
            _flash(request, "Неверный формат даты 'повторять до'", "error")
            return back_to_form()

    # приоритет
    pr_mode = "AUTO" if priority_mode not in ("AUTO", "MANUAL") else priority_mode
    pr_value = priority
    if pr_mode == "AUTO":
        pr_value = _auto_priority(dt_due)

    # защита по рекуррентности
    if recur_freq not in ("NONE", "DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "YEARLY"):
#    if recur_freq not in ("NONE", "DAILY", "WEEKLY", "MONTHLY"):
        recur_freq = "NONE"
    if recur_interval < 1:
        recur_interval = 1

    # назначение
    dept_id: Optional[int] = None
    asg_id: Optional[int] = None

    if assign_type == "department":
        qd = db.query(Department).filter(
            Department.org_id == org.id,
            Department.id == department_id,
            Department.archived_at.is_(None),
        )
        if not is_admin:
            if not my_depts:
                _flash(request, "Нет доступных подразделений", "error")
                return back_to_form()
            qd = qd.filter(Department.id.in_(my_depts))
        d = qd.first()
        if not d:
            _flash(request, "Укажите корректное подразделение", "error")
            return back_to_form()
        dept_id = d.id

    elif assign_type == "user":
        if not is_admin:
            assignee_id = user.id  # обычный может назначить только на себя
        u = db.query(User).filter(
            User.org_id == org.id,
            User.id == (assignee_id or user.id),
            User.is_active == True,  # noqa: E712
        ).first()
        if not u:
            _flash(request, "Укажите корректного пользователя", "error")
            return back_to_form()
        asg_id = u.id
        dept_id = None

    elif assign_type == "none":
        if is_admin:
            dept_id = None
            asg_id = None
        else:
            if not my_depts:
                _flash(request, "Выберите подразделение или назначьте на себя", "error")
                return back_to_form()
            if len(my_depts) == 1:
                dept_id = next(iter(my_depts))
            else:
                _flash(request, "Выберите подразделение или назначьте на себя", "error")
                return back_to_form()
    else:
        _flash(request, "Некорректный способ назначения", "error")
        return back_to_form()

    # клиент (опционально)
    comp_id: Optional[int] = None
    if company_id:
        c = db.query(Company).filter(
            Company.id == company_id,
            Company.org_id == org.id
        ).first()
        if c:
            comp_id = c.id

    # создаём задачу
    t = Task(
        org_id=org.id,
        title=title.strip(),
        description=(description or "").strip() or None,
        department_id=dept_id,
        assignee_user_id=asg_id,
        status="OPEN",
        due_date=dt_due,
        created_by=user.id,
        company_id=comp_id,
        priority_mode=pr_mode,
        priority=pr_value,
        recur_freq=recur_freq,
        recur_interval=recur_interval,
        recur_until=dt_until,
    )
    db.add(t)
    db.flush()
    db.add(
        TaskHistory(
            task_id=t.id,
            actor_user_id=user.id,
            action="CREATE",
            payload={
                "assign_type": assign_type,
                "priority_mode": pr_mode,
                "recur_freq": recur_freq,
            },
        )
    )
    db.commit()
    _flash(request, "Задача создана")
    return RedirectResponse(url="/org/tasks", status_code=303)

# ----------------- DETAIL -----------------
@router.get("/tasks/{task_id:int}", name="task_detail", response_class=HTMLResponse)
def task_detail(
    task_id: int,
    request: Request,
    user: User = Depends(require_org_user),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    t = db.query(Task).filter(Task.id == task_id, Task.org_id == org.id).first()
    if not t:
        _flash(request, "Задача не найдена", "error")
        return RedirectResponse(url="/org/tasks", status_code=303)

    history = (
        db.query(TaskHistory)
        .filter(TaskHistory.task_id == t.id)
        .order_by(TaskHistory.id.asc())
        .all()
    )
    depts = db.query(Department).filter(
        Department.org_id == org.id, Department.archived_at.is_(None)
    ).all()
    users = db.query(User).filter(
        User.org_id == org.id, User.is_active == True  # noqa: E712
    ).all()

    # --- право на редактирование (админ, создатель, исполнитель) ---
    is_admin = user.is_system_admin or user.org_role == "ORG_ADMIN"
    assignee_id = getattr(t, "assignee_user_id", getattr(t, "assignee_id", None))
    can_edit = is_admin or (t.created_by == user.id) or (assignee_id == user.id)

    return templates.TemplateResponse(
        "org/task_detail.html",
        {
            "request": request,
            "org": org,
            "user": user,
            "t": t,
            "history": history,
            "depts": depts,
            "users": users,
            "back_url": _back_url(request, "/org/tasks"),
            "can_edit": can_edit,
        },
    )

# ----------------- COMMENT -----------------
@router.post("/tasks/{task_id:int}/comment", name="task_comment")
def task_comment(
    task_id: int,
    request: Request,
    comment: str = Form(...),
    user: User = Depends(require_org_user),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    t = db.query(Task).filter(Task.id == task_id, Task.org_id == org.id).first()
    if not t:
        _flash(request, "Задача не найдена", "error")
        return RedirectResponse(url="/org/tasks", status_code=303)

    text = (comment or "").strip()
    if not text:
        _flash(request, "Пустой комментарий", "error")
        return RedirectResponse(url=f"/org/tasks/{task_id}", status_code=303)

    db.add(
        TaskHistory(
            task_id=t.id,
            actor_user_id=user.id,
            action="comment",
            payload={"text": text},
        )
    )
    db.commit()
    _flash(request, "Комментарий добавлен")
    return RedirectResponse(f"/org/tasks/{task_id}", status_code=303)

# ----------------- RESCHEDULE (admin or assignee) -----------------
@router.post("/tasks/{task_id:int}/reschedule", name="task_reschedule")
def task_reschedule(
    task_id: int,
    request: Request,
    new_due: str = Form(""),
    reason: str = Form(""),
    user: User = Depends(require_org_user),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    t = db.query(Task).filter(Task.id == task_id, Task.org_id == org.id).first()
    if not t:
        _flash(request, "Задача не найдена", "error")
        return RedirectResponse(url="/org/tasks", status_code=303)

    if user.org_role != "ORG_ADMIN" and t.assignee_user_id != user.id:
        raise HTTPException(status_code=403, detail="Перенос срока доступен администратору или исполнителю")

    old_due = t.due_date

    new_due_dt = _parse_datetime_input(new_due)
    if (new_due or "").strip() and new_due_dt is None:
        _flash(request, "Неверный формат даты", "error")
        return RedirectResponse(url=f"/org/tasks/{task_id}", status_code=303)

    t.due_date = new_due_dt

    if getattr(t, "priority_mode", "AUTO") == "AUTO":
        t.priority = _auto_priority(new_due_dt)

    payload = {
        "from": old_due.isoformat() if old_due else None,
        "to": new_due_dt.isoformat() if new_due_dt else None,
        "reason": (reason or "").strip() or None,
        "priority_mode": getattr(t, "priority_mode", None),
        "priority": getattr(t, "priority", None),
    }
    db.add(
        TaskHistory(
            task_id=t.id,
            actor_user_id=user.id,
            action="RESCHEDULE",
            payload=payload,
        )
    )
    db.commit()

    _flash(request, "Срок задачи обновлён")
    return RedirectResponse(url=f"/org/tasks/{task_id}", status_code=303)

# ----------------- CLAIM (перехват) -----------------
@router.post("/tasks/{task_id:int}/claim", name="task_claim")
def task_claim(
    task_id: int,
    request: Request,
    user: User = Depends(require_org_user),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    t = db.query(Task).filter(Task.id == task_id, Task.org_id == org.id, Task.status == "OPEN").first()
    if not t:
        _flash(request, "Задача не найдена или недоступна", "error")
        return RedirectResponse(url="/org/tasks", status_code=303)

    my_depts = _user_department_ids(db, user.id)
    can = (user.org_role == "ORG_ADMIN") or (t.department_id and t.department_id in my_depts)
    if not can and getattr(t, "assignee_id", None) and t.assignee_id != user.id and my_depts:
        ass_depts = _user_department_ids(db, t.assignee_id)
        can = bool(my_depts.intersection(ass_depts))

    if not can:
        _flash(request, "Перехват запрещён", "error")
        return RedirectResponse(url=f"/org/tasks/{task_id}", status_code=303)

    if hasattr(t, "assignee_id"):
        t.assignee_id = user.id
    else:
        t.assignee_user_id = user.id
    t.department_id = None

    db.add(TaskHistory(task_id=t.id, actor_user_id=user.id, action="CLAIM", payload={}))
    db.commit()
    _flash(request, "Задача перехвачена")
    return RedirectResponse(url=f"/org/tasks/{task_id}", status_code=303)

# ----------------- COMPLETE -----------------
@router.post("/tasks/{task_id:int}/complete", name="task_complete")
def task_complete(
    task_id: int,
    request: Request,
    user: User = Depends(require_org_user),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    t = db.query(Task).filter(Task.id == task_id, Task.org_id == org.id).with_for_update().first()
    if not t:
        _flash(request, "Задача не найдена", "error")
        return RedirectResponse(url="/org/tasks", status_code=303)

    if t.status != "OPEN":
        _flash(request, "Задача уже завершена или отменена")
        return RedirectResponse(url=f"/org/tasks/{task_id}", status_code=303)

    t.status = "DONE"

    # если задача рекуррентная — создаём следующую
    if t.recur_freq != "NONE":
        next_due = _next_due_date(t.due_date, t.recur_freq, t.recur_interval)
        can_spawn = True
        if t.recur_until and next_due:
            can_spawn = next_due.date() <= t.recur_until

        if can_spawn:
            t2 = Task(
                org_id=t.org_id,
                title=t.title,
                description=t.description,
                department_id=t.department_id,
                assignee_user_id=getattr(t, "assignee_user_id", getattr(t, "assignee_id", None)),
                status="OPEN",
                due_date=next_due,
                created_by=user.id,
                priority_mode=t.priority_mode,
                priority=(t.priority if t.priority_mode == "MANUAL" else _auto_priority(next_due)),
                recur_freq=t.recur_freq,
                recur_interval=t.recur_interval,
                recur_until=t.recur_until,
                company_id=getattr(t, "company_id", None),
            )
            db.add(t2)
            db.flush()
            db.add(
                TaskHistory(
                    task_id=t2.id,
                    actor_user_id=user.id,
                    action="RECUR_CREATE",
                    payload={"from": t.id},
                )
            )

    db.add(TaskHistory(task_id=t.id, actor_user_id=user.id, action="COMPLETE", payload={}))
    db.commit()
    _flash(request, "Задача завершена")
    return RedirectResponse(url=f"/org/tasks/{task_id}", status_code=303)

# ----------------- CANCEL (admin only) -----------------
@router.post("/tasks/{task_id:int}/cancel", name="task_cancel")
def task_cancel(
    task_id: int,
    request: Request,
    user: User = Depends(require_org_admin),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    t = db.query(Task).filter(Task.id == task_id, Task.org_id == org.id).with_for_update().first()
    if not t:
        _flash(request, "Задача не найдена", "error")
        return RedirectResponse(url="/org/tasks", status_code=303)

    if t.status != "OPEN":
        _flash(request, "Задача уже завершена или отменена")
        return RedirectResponse(url=f"/org/tasks/{task_id}", status_code=303)

    t.status = "CANCELED"
    db.add(TaskHistory(task_id=t.id, actor_user_id=user.id, action="CANCEL", payload={}))
    db.commit()
    _flash(request, "Задача отменена")
    return RedirectResponse(url=f"/org/tasks/{task_id}", status_code=303)

# ----------------- REASSIGN (admin only) -----------------
@router.post("/tasks/{task_id:int}/reassign", name="task_reassign")
def task_reassign(
    task_id: int,
    request: Request,
    assign_type: str = Form("department"),  # department|user|none
    department_id: int = Form(0),
    assignee_id: int = Form(0),
    user: User = Depends(require_org_admin),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    t = db.query(Task).filter(Task.id == task_id, Task.org_id == org.id).first()
    if not t:
        _flash(request, "Задача не найдена", "error")
        return RedirectResponse(url="/org/tasks", status_code=303)

    old = {
        "department_id": t.department_id,
        "assignee_id": getattr(t, "assignee_id", getattr(t, "assignee_user_id", None)),
    }

    if assign_type == "department":
        d = (
            db.query(Department)
            .filter(
                Department.org_id == org.id,
                Department.id == department_id,
                Department.archived_at.is_(None),
            )
            .first()
        )
        if not d:
            _flash(request, "Укажите подразделение", "error")
            return RedirectResponse(url=f"/org/tasks/{task_id}", status_code=303)
        t.department_id = d.id
        if hasattr(t, "assignee_id"):
            t.assignee_id = None
        else:
            t.assignee_user_id = None

    elif assign_type == "user":
        u = (
            db.query(User)
            .filter(User.org_id == org.id, User.id == assignee_id, User.is_active == True)  # noqa: E712
            .first()
        )
        if not u:
            _flash(request, "Укажите пользователя", "error")
            return RedirectResponse(url=f"/org/tasks/{task_id}", status_code=303)
        if hasattr(t, "assignee_id"):
            t.assignee_id = u.id
        else:
            t.assignee_user_id = u.id
        t.department_id = None

    elif assign_type == "none":
        if hasattr(t, "assignee_id"):
            t.assignee_id = None
        else:
            t.assignee_user_id = None
        t.department_id = None
    else:
        _flash(request, "Некорректное назначение", "error")
        return RedirectResponse(url=f"/org/tasks/{task_id}", status_code=303)

    db.add(
        TaskHistory(
            task_id=t.id,
            actor_user_id=user.id,
            action="REASSIGN",
            payload={
                "from": old,
                "to": {
                    "department_id": t.department_id,
                    "assignee_id": getattr(t, "assignee_id", getattr(t, "assignee_user_id", None)),
                },
            },
        )
    )
    db.commit()
    _flash(request, "Назначение обновлено")
    return RedirectResponse(url=f"/org/tasks/{task_id}", status_code=303)

# ----------------- DISABLE RECURRENCE -----------------
@router.post("/tasks/{task_id:int}/recur/disable", name="task_recur_disable")
def task_recur_disable(
    task_id: int,
    request: Request,
    user: User = Depends(require_org_user),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    t = db.query(Task).filter(Task.id == task_id, Task.org_id == org.id).first()
    if not t:
        _flash(request, "Задача не найдена", "error")
        return RedirectResponse(url="/org/tasks", status_code=303)

    # права: админ, создатель или исполнитель
    is_admin = user.is_system_admin or user.org_role == "ORG_ADMIN"
    assignee_id = getattr(t, "assignee_user_id", getattr(t, "assignee_id", None))
    can = is_admin or (t.created_by == user.id) or (assignee_id == user.id)
    if not can:
        _flash(request, "Недостаточно прав для изменения повторения", "error")
        return RedirectResponse(url=f"/org/tasks/{task_id}", status_code=303)

    if t.recur_freq == "NONE":
        _flash(request, "Повторение уже выключено")
        return RedirectResponse(url=f"/org/tasks/{task_id}", status_code=303)

    # выключаем повторение
    t.recur_freq = "NONE"
    t.recur_interval = 1
    t.recur_until = None

    db.add(TaskHistory(
        task_id=t.id,
        actor_user_id=user.id,
        action="RECUR_DISABLE",
        payload={}
    ))
    db.commit()

    _flash(request, "Повторение выключено")
    return RedirectResponse(url=f"/org/tasks/{task_id}", status_code=303)

# ----------------- EDIT FORM -----------------
@router.get("/tasks/{task_id:int}/edit", name="task_edit_form", response_class=HTMLResponse)
def task_edit_form(
    task_id: int,
    request: Request,
    user: User = Depends(require_org_user),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    t = db.query(Task).filter(Task.id == task_id, Task.org_id == org.id).first()
    if not t:
        _flash(request, "Задача не найдена", "error")
        return RedirectResponse(url="/org/tasks", status_code=303)

    # права: админ, создатель или исполнитель (при желании упростим)
    is_admin = user.is_system_admin or user.org_role == "ORG_ADMIN"
    assignee_id = getattr(t, "assignee_user_id", getattr(t, "assignee_id", None))
    can = is_admin or (t.created_by == user.id) or (assignee_id == user.id)
    if not can:
        _flash(request, "Недостаточно прав для редактирования", "error")
        return RedirectResponse(url=f"/org/tasks/{task_id}", status_code=303)

    # ------- наполняем те же списки, что и в new -------
    # Компания (для автодополнения)
    selected_company_id = getattr(t, "company_id", None)
    selected_company_label = ""
    if selected_company_id:
        c = db.query(Company).filter(Company.id == selected_company_id, Company.org_id == org.id).first()
        if c:
            name = (getattr(c, "short_name", None) or getattr(c, "full_name", None) or f"Компания #{c.id}")
            inn = (getattr(c, "inn", "") or "").strip()
            selected_company_label = f"{name}{f' ({inn})' if inn else ''}"

    # Подразделения
    dept_filters = [Department.org_id == org.id]
    if hasattr(Department, "archived_at"):
        dept_filters.append(Department.archived_at.is_(None))
    departments = db.query(Department).filter(*dept_filters).order_by(Department.name.asc()).all()

    # Пользователи (исполнитель)
    users = db.query(User).filter(User.org_id == org.id, User.is_active == True).order_by(
        User.full_name.asc(), User.email.asc()
    ).all()  # noqa: E712

    # Значения по умолчанию для формы
    assign_type_value = "none"
    if getattr(t, "assignee_user_id", None) or getattr(t, "assignee_id", None):
        assign_type_value = "user"
    elif t.department_id:
        assign_type_value = "department"

    # due -> только дата
    due_date_value = t.due_date.date().isoformat() if t.due_date else ""

    return templates.TemplateResponse(
        "org/task_form.html",
        {
            "request": request,
            "org": org,
            "user": user,
            # режим редактирования
            "edit_mode": True,
            "form_action": f"/org/tasks/{t.id}/edit",
            "form_title": "Редактировать задачу",
            # поля
            "title_value": t.title or "",
            "description_value": t.description or "",
            "due_date_value": due_date_value,
            "priority_mode_value": getattr(t, "priority_mode", "AUTO"),
            "priority_value": getattr(t, "priority", "MEDIUM"),
            "recur_freq_value": getattr(t, "recur_freq", "NONE"),
            "recur_interval_value": getattr(t, "recur_interval", 1),
            "recur_until_value": t.recur_until.isoformat() if getattr(t, "recur_until", None) else "",
            "assign_type_value": assign_type_value,
            "selected_department_id": t.department_id,
            "selected_assignee_id": getattr(t, "assignee_user_id", getattr(t, "assignee_id", None)),
            # списки
            "departments": departments,
            "depts": departments,
            "users": users,
            "assignees": users,
            # компания
            "selected_company_id": selected_company_id,
            "selected_company_label": selected_company_label,
            # навигация
            "back_url": _back_url(request, f"/org/tasks/{t.id}"),
        },
    )


# ----------------- EDIT SUBMIT -----------------
@router.post("/tasks/{task_id:int}/edit", name="task_edit_submit")
def task_edit_submit(
    task_id: int,
    request: Request,
    title: str = Form(..., max_length=255),
    description: str = Form(""),
    due_at: str = Form(""),
    assign_type: str = Form("department"),
    department_id: int = Form(0),
    assignee_id: int = Form(0),
    priority_mode: str = Form("AUTO"),
    priority: str = Form("MEDIUM"),
    recur_freq: str = Form("NONE"),
    recur_interval: int = Form(1),
    recur_until: str = Form(""),
    company_id: int = Form(0),
    user: User = Depends(require_org_user),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    if not title.strip():
        _flash(request, "Укажите название задачи", "error")
        return RedirectResponse(url=f"/org/tasks/{task_id}/edit", status_code=303)

    org: Organization = get_org_for_action(db, user, org_id)
    t = db.query(Task).filter(Task.id == task_id, Task.org_id == org.id).first()
    if not t:
        _flash(request, "Задача не найдена", "error")
        return RedirectResponse(url="/org/tasks", status_code=303)

    # права: админ, создатель или исполнитель
    is_admin = user.is_system_admin or user.org_role == "ORG_ADMIN"
    assignee_curr = getattr(t, "assignee_user_id", getattr(t, "assignee_id", None))
    can = is_admin or (t.created_by == user.id) or (assignee_curr == user.id)
    if not can:
        _flash(request, "Недостаточно прав для редактирования", "error")
        return RedirectResponse(url=f"/org/tasks/{task_id}", status_code=303)

    # дедлайн: понимаем ДД.ММ.ГГГГ/ДД.ММ.ГГГГ HH:MM/ISO
    dt_due: Optional[datetime] = _parse_datetime_input(due_at)
    if (due_at or "").strip() and dt_due is None:
        _flash(request, "Неверный формат даты срока", "error")
        return RedirectResponse(url=f"/org/tasks/{task_id}/edit", status_code=303)

    # recur_until
    dt_until: Optional[date] = None
    if recur_until.strip():
        try:
            dt_until = date.fromisoformat(recur_until.strip())
        except Exception:
            _flash(request, "Неверный формат даты 'повторять до'", "error")
            return RedirectResponse(url=f"/org/tasks/{task_id}/edit", status_code=303)

    # нормализуем режимы
    pr_mode = "AUTO" if priority_mode not in ("AUTO", "MANUAL") else priority_mode
    pr_value = priority if pr_mode == "MANUAL" else _auto_priority(dt_due)

    if recur_freq not in ("NONE", "DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "YEARLY"):
#    if recur_freq not in ("NONE", "DAILY", "WEEKLY", "MONTHLY"):
        recur_freq = "NONE"
    if recur_interval < 1:
        recur_interval = 1

    # назначение
    dept_id: Optional[int] = None
    asg_id: Optional[int] = None
    if assign_type == "department":
        d = db.query(Department).filter(
            Department.org_id == org.id,
            Department.id == department_id,
            Department.archived_at.is_(None),
        ).first()
        if not d:
            _flash(request, "Укажите корректное подразделение", "error")
            return RedirectResponse(url=f"/org/tasks/{task_id}/edit", status_code=303)
        dept_id = d.id
    elif assign_type == "user":
        u = db.query(User).filter(
            User.org_id == org.id,
            User.id == (assignee_id or user.id),
            User.is_active == True,  # noqa: E712
        ).first()
        if not u:
            _flash(request, "Укажите корректного пользователя", "error")
            return RedirectResponse(url=f"/org/tasks/{task_id}/edit", status_code=303)
        asg_id = u.id
        dept_id = None
    elif assign_type == "none":
        dept_id = None
        asg_id = None
    else:
        _flash(request, "Некорректный способ назначения", "error")
        return RedirectResponse(url=f"/org/tasks/{task_id}/edit", status_code=303)

    # компания
    comp_id: Optional[int] = None
    if company_id:
        c = db.query(Company).filter(Company.id == company_id, Company.org_id == org.id).first()
        if c:
            comp_id = c.id

    # применяем изменения
    old = {
        "title": t.title,
        "description": t.description,
        "due_date": t.due_date.isoformat() if t.due_date else None,
        "priority_mode": getattr(t, "priority_mode", None),
        "priority": getattr(t, "priority", None),
        "recur_freq": getattr(t, "recur_freq", None),
        "recur_interval": getattr(t, "recur_interval", None),
        "recur_until": (getattr(t, "recur_until", None).isoformat()
                        if getattr(t, "recur_until", None) else None),
        "department_id": t.department_id,
        "assignee_id": getattr(t, "assignee_user_id", getattr(t, "assignee_id", None)),
        "company_id": getattr(t, "company_id", None),
    }

    new_preview = {
        "title": title.strip(),
        "description": (description or "").strip() or None,
        "due_date": dt_due.isoformat() if dt_due else None,
        "priority_mode": pr_mode,
        "priority": pr_value,
        "recur_freq": recur_freq,
        "recur_interval": recur_interval,
        "recur_until": dt_until.isoformat() if dt_until else None,
        "department_id": dept_id,
        "assignee_id": asg_id,
        "company_id": comp_id,
    }

    diff = {}
    for k, old_v in old.items():
        new_v = new_preview[k]
        if old_v != new_v:
            diff[k] = {"from": old_v, "to": new_v}

    t.title = new_preview["title"]
    t.description = new_preview["description"]
    t.due_date = dt_due
    t.priority_mode = pr_mode
    t.priority = pr_value
    t.recur_freq = recur_freq
    t.recur_interval = recur_interval
    t.recur_until = dt_until
    t.department_id = dept_id
    if hasattr(t, "assignee_user_id"):
        t.assignee_user_id = asg_id
    else:
        t.assignee_id = asg_id
    t.company_id = comp_id

    db.add(TaskHistory(
        task_id=t.id,
        actor_user_id=user.id,
        action="UPDATE",
        payload=(diff or {"note": "no changes"})
    ))
    db.commit()

    _flash(request, "Задача обновлена")
    return RedirectResponse(url=f"/org/tasks/{task_id}", status_code=303)
