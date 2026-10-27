# app/routes/org_users.py
from __future__ import annotations
from pathlib import Path
from typing import Optional, List

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.services.auth import get_db, require_org_admin, get_org_for_action
from app.models.organization import Organization
from app.models.user import User
from app.models.department import Department
from app.models.user_department import UserDepartment
from app.core.security import is_password_strong, hash_password

router = APIRouter(prefix="/org", tags=["Org Users"])

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# --- flash helpers (как в org_admin) ---
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
    db.add(dept)
    db.flush()
    return dept


def _count_active_admins(db: Session, org_id: int) -> int:
    return db.query(User).filter(
        User.org_id == org_id,
        User.org_role == "ORG_ADMIN",
        User.is_active == True  # noqa: E712
    ).count()


# ---------- LIST ----------
@router.get("/users", response_class=HTMLResponse)
def users_list(
    request: Request,
    user: User = Depends(require_org_admin),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    users = db.query(User).filter(User.org_id == org.id).order_by(User.id.asc()).all()
    depts = db.query(Department).filter(Department.org_id == org.id, Department.archived_at.is_(None)).all()
    flash_success, flash_error = _take_flash(request)
    return templates.TemplateResponse(
        "org/users.html",
        {
            "request": request,
            "org": org,
            "user": user,
            "users": users,
            "depts": depts,
            "back_url": _back_url(request, "/org"),
            "flash_success": flash_success,
            "flash_error": flash_error,
        },
    )


# ---------- CREATE ----------
@router.get("/users/new", response_class=HTMLResponse)
def user_new_form(
    request: Request,
    user: User = Depends(require_org_admin),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    depts = db.query(Department).filter(Department.org_id == org.id, Department.archived_at.is_(None)).all()
    if not depts:
        depts = [_ensure_default_department(db, org.id)]
        db.commit()
    return templates.TemplateResponse(
        "org/user_form.html",
        {
            "request": request,
            "org": org,
            "user": user,
            "mode": "new",
            "item": None,
            "depts": depts,
            "back_url": _back_url(request, f"/org/users?org_id={org.id}"),
            "error": None,
        },
    )


@router.post("/users/new")
def user_new_submit(
    request: Request,
    full_name: str = Form(...),
    short_name: str = Form(""),
    email: str = Form(...),
    role: str = Form(...),   # ORG_USER | ORG_ADMIN
    password: str = Form(...),
    departments: List[int] = Form([]),  # множество department_id
    user: User = Depends(require_org_admin),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)

    # глобальная уникальность email (регистр игнорируем)
    exists = db.query(User).filter(func.lower(User.email) == email.strip().lower()).first()
    if exists:
        _flash(request, "Пользователь с таким email уже существует", "error")
        return RedirectResponse(url=f"/org/users/new?org_id={org.id}", status_code=303)

    if role not in ("ORG_USER", "ORG_ADMIN"):
        _flash(request, "Некорректная роль", "error")
        return RedirectResponse(url=f"/org/users/new?org_id={org.id}", status_code=303)

    if not is_password_strong(password):
        _flash(request, "Пароль не соответствует политике сложности", "error")
        return RedirectResponse(url=f"/org/users/new?org_id={org.id}", status_code=303)

    u = User(
        full_name=full_name.strip(),
        short_name=(short_name or "").strip() or None,
        email=email.strip(),
        password_hash=hash_password(password),
        is_system_admin=False,
        is_active=True,
        org_id=org.id,
        org_role=role,
    )
    db.add(u)
    db.flush()

    new_dept_ids = {int(x) for x in departments if x}

    if not new_dept_ids:
        default = _ensure_default_department(db, org.id)
        db.add(UserDepartment(user_id=u.id, department_id=default.id))
    else:
        # берём только валидные и активные департаменты этой организации
        valid_ids = {
            d.id
            for d in db.query(Department).filter(
                Department.org_id == org.id,
                Department.archived_at.is_(None),
                Department.id.in_(list(new_dept_ids)),
            )
        }
        if not valid_ids:
            default = _ensure_default_department(db, org.id)
            db.add(UserDepartment(user_id=u.id, department_id=default.id))
        else:
            # Postgres: upsert, остальные БД — безопасно поштучно
            rows = [{"user_id": u.id, "department_id": did} for did in valid_ids]
            if rows:
                try:
                    stmt = pg_insert(UserDepartment).values(rows)
                    stmt = stmt.on_conflict_do_nothing(
                        index_elements=["user_id", "department_id"]
                    )
                    db.execute(stmt)
                except Exception:
                    for did in valid_ids:
                        try:
                            db.add(UserDepartment(user_id=u.id, department_id=did))
                            db.flush()
                        except IntegrityError:
                            db.rollback()

    db.commit()
    _flash(request, "Пользователь создан")
    return RedirectResponse(url=f"/org/users?org_id={org.id}", status_code=303)


# ---------- EDIT FORM ----------
@router.get("/users/{uid}/edit", response_class=HTMLResponse)
def user_edit_form(
    uid: int,
    request: Request,
    user: User = Depends(require_org_admin),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    item = db.query(User).filter(User.id == uid, User.org_id == org.id).first()
    if not item:
        _flash(request, "Пользователь не найден", "error")
        return RedirectResponse(url=f"/org/users?org_id={org.id}", status_code=303)
    depts = db.query(Department).filter(Department.org_id == org.id, Department.archived_at.is_(None)).all()
    user_dept_ids = {ud.department_id for ud in item.user_departments}
    return templates.TemplateResponse(
        "org/user_form.html",
        {
            "request": request,
            "org": org,
            "user": user,
            "mode": "edit",
            "item": item,
            "depts": depts,
            "user_dept_ids": user_dept_ids,
            "back_url": _back_url(request, f"/org/users?org_id={org.id}"),
            "error": None,
        },
    )


# ---------- UPDATE (role/active/names/departments) ----------
@router.post("/users/{uid}/update")
def user_update_submit(
    uid: int,
    request: Request,
    full_name: str = Form(...),
    short_name: str = Form(""),
    role: str = Form(...),
    active: str = Form("on"),  # checkbox
    department_ids: List[int] = Form([]),   # <--- список чекбоксов
    user: User = Depends(require_org_admin),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    """
    Для org-админа работаем как раньше.
    Для системного админа, если org_id не передан, определяем org по самому пользователю.
    """
    # --- определяем org и пользователя ---
    if user.is_system_admin and org_id is None:
        # редактирует системный админ без явного org_id — берём org_id пользователя
        item = db.query(User).filter(User.id == uid).first()
        if not item:
            _flash(request, "Пользователь не найден", "error")
            return RedirectResponse(url="/org/users", status_code=303)
        if not item.org_id:
            _flash(request, "Пользователь не привязан к организации", "error")
            return RedirectResponse(url="/org/users", status_code=303)
        org = db.query(Organization).filter(Organization.id == item.org_id).first()
        if not org:
            _flash(request, "Организация пользователя не найдена", "error")
            return RedirectResponse(url="/org/users", status_code=303)
    else:
        org = get_org_for_action(db, user, org_id)
        item = db.query(User).filter(User.id == uid, User.org_id == org.id).first()
        if not item:
            _flash(request, "Пользователь не найден", "error")
            return RedirectResponse(url=f"/org/users?org_id={org.id}", status_code=303)

    if role not in ("ORG_USER", "ORG_ADMIN"):
        _flash(request, "Некорректная роль", "error")
        return RedirectResponse(url=f"/org/users/{uid}/edit?org_id={org.id}", status_code=303)

    new_active = (active == "on")

    # защита: нельзя оставить организацию без активных админов
    admins_before = _count_active_admins(db, org.id)
    demoting_last_admin = (item.org_role == "ORG_ADMIN" and role == "ORG_USER" and admins_before <= 1)
    deactivating_last_admin = (item.org_role == "ORG_ADMIN" and not new_active and admins_before <= 1)
    if demoting_last_admin or deactivating_last_admin:
        _flash(request, "Нельзя понизить или деактивировать последнего администратора организации", "error")
        return RedirectResponse(url=f"/org/users/{uid}/edit?org_id={org.id}", status_code=303)

    # основные поля
    item.full_name = full_name.strip()
    item.short_name = (short_name or "").strip() or None
    item.org_role = role
    item.is_active = new_active

    # ---------- департаменты: считаем дифф ----------
    new_dept_ids = {int(x) for x in department_ids if x}

    # оставляем только валидные и активные департаменты этой организации
    if new_dept_ids:
        valid_ids = {
            d.id
            for d in db.query(Department).filter(
                Department.org_id == org.id,
                Department.archived_at.is_(None),
                Department.id.in_(list(new_dept_ids)),
            )
        }
    else:
        valid_ids = set()

    # текущие связи пользователя
    existing_rows = (
        db.query(UserDepartment)
        .filter(UserDepartment.user_id == item.id)
        .all()
    )
    existing_ids = {r.department_id for r in existing_rows}

    to_add = valid_ids - existing_ids
    to_remove = existing_ids - valid_ids

    # если после изменений ничего не остаётся — добавим «Основное»
    will_have = (existing_ids | to_add) - to_remove
    if not will_have:
        default = _ensure_default_department(db, org.id)
        to_add.add(default.id)

    # удалить лишние
    if to_remove:
        (
            db.query(UserDepartment)
            .filter(
                UserDepartment.user_id == item.id,
                UserDepartment.department_id.in_(list(to_remove)),
            )
            .delete(synchronize_session=False)
        )

    # добавить недостающие (без дублей)
    if to_add:
        rows = [{"user_id": item.id, "department_id": did} for did in to_add]
        try:
            stmt = pg_insert(UserDepartment).values(rows)
            stmt = stmt.on_conflict_do_nothing(index_elements=["user_id", "department_id"])
            db.execute(stmt)
        except Exception:
            for did in to_add:
                try:
                    db.add(UserDepartment(user_id=item.id, department_id=did))
                    db.flush()
                except IntegrityError:
                    db.rollback()

    db.commit()
    _flash(request, "Изменения сохранены")
    return RedirectResponse(url=f"/org/users?org_id={org.id}", status_code=303)


# ---------- SET PASSWORD ----------
@router.post("/users/{uid}/set-password")
def user_set_password(
    uid: int,
    request: Request,
    password: str = Form(...),
    user: User = Depends(require_org_admin),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    """
    Аналогично update: для sysadmin без org_id берём org_id из пользователя.
    """
    if user.is_system_admin and org_id is None:
        item = db.query(User).filter(User.id == uid).first()
        if not item:
            _flash(request, "Пользователь не найден", "error")
            return RedirectResponse(url="/org/users", status_code=303)
        if not item.org_id:
            _flash(request, "Пользователь не привязан к организации", "error")
            return RedirectResponse(url="/org/users", status_code=303)
        org = db.query(Organization).filter(Organization.id == item.org_id).first()
        if not org:
            _flash(request, "Организация пользователя не найдена", "error")
            return RedirectResponse(url="/org/users", status_code=303)
    else:
        org: Organization = get_org_for_action(db, user, org_id)
        item = db.query(User).filter(User.id == uid, User.org_id == org.id).first()
        if not item:
            _flash(request, "Пользователь не найден", "error")
            return RedirectResponse(url=f"/org/users?org_id={org.id}", status_code=303)

    if not is_password_strong(password):
        _flash(request, "Пароль не соответствует политике сложности", "error")
        return RedirectResponse(url=f"/org/users/{uid}/edit?org_id={org.id}", status_code=303)

    item.password_hash = hash_password(password)
    db.commit()
    _flash(request, "Пароль обновлён")
    return RedirectResponse(url=f"/org/users/{uid}/edit?org_id={org.id}", status_code=303)
