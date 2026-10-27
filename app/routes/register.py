# app/routes/register.py
from __future__ import annotations

import re
from typing import Optional
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy.orm import Session
from starlette.templating import Jinja2Templates

from app.db import get_db
from app.core.security import hash_password, is_password_strong
from app.services.dadata import get_company_by_inn
from app.models.organization import Organization
from app.models.user import User
from app.models.department import Department
from app.models.user_department import UserDepartment
from app.services.auth import SESSION_KEY  # "uid"

templates = Jinja2Templates(directory="app/templates")

router = APIRouter(prefix="", tags=["Регистрация"])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    return templates.TemplateResponse("register.html", {
        "request": request,
        "errors": [],
        "form": {},
    })

@router.post("/register", response_class=HTMLResponse)
def register_submit(
    request: Request,
    db: Session = Depends(get_db),
    inn: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    full_name: str = Form(...),
    position: Optional[str] = Form(None),  # кладём в short_name
):
    errors: list[str] = []
    form = {"inn": inn, "email": email, "full_name": full_name, "position": position or ""}

    # нормализация
    inn = (inn or "").strip()
    email = (email or "").strip().lower()
    full_name = (full_name or "").strip()
    position = (position or "").strip() if position else None

    # базовая валидация
    if not inn.isdigit() or len(inn) not in (10, 12):
        errors.append("ИНН должен состоять из 10 или 12 цифр")
    if not EMAIL_RE.match(email):
        errors.append("Некорректный email")
    if password != password2:
        errors.append("Пароли не совпадают")
    if not is_password_strong(password):
        errors.append("Слабый пароль: минимум 10 символов, буквы разного регистра, цифры и спецсимвол")

    # email уже существует?
    if db.query(User).filter(User.email == email).first():
        errors.append("Пользователь с таким email уже существует")

    # организация уже существует?
    existing_org = db.query(Organization).filter(Organization.inn == inn).first()
    if existing_org:
        errors.append("Организация с таким ИНН уже зарегистрирована. Запросите доступ у администратора.")

    # тянем из Dadata
    dadata = None
    if not errors:
        dadata = get_company_by_inn(inn)
        if not dadata.get("success"):
            errors.append(dadata.get("error") or "Не удалось получить данные из Dadata")

    if errors:
        return templates.TemplateResponse("register.html", {
            "request": request,
            "errors": errors,
            "form": form,
        })

    # создаём организацию
    org = Organization(
        short_name=dadata.get("short_name") or "Организация",
        full_name=dadata.get("full_name"),
        inn=inn,                          # из формы
        ogrn=dadata.get("ogrn"),
#        kpp=dadata.get("kpp"),
        ceo_name=dadata.get("ceo_name"),
        status="ACTIVE",
    )
    db.add(org)
    db.flush()  # нужен org.id

    # дефолтный отдел "Основное"
    dept = db.query(Department).filter(
        Department.org_id == org.id,
        Department.name == "Основное",
        Department.archived_at.is_(None),
    ).first()
    if not dept:
        dept = Department(org_id=org.id, name="Основное")
        db.add(dept)
        db.flush()

    # создаём пользователя — первого администратора
    user = User(
        email=email,
        full_name=full_name,
        short_name=position or None,      # <= вместо position=...
        password_hash=hash_password(password),
        is_system_admin=False,
        is_active=True,
        org_id=org.id,
        org_role="ORG_ADMIN",
    )
    db.add(user)
    db.flush()

    # привязка к отделу
    db.add(UserDepartment(user_id=user.id, department_id=dept.id))

    db.commit()

    # логиним и редиректим
    request.session[SESSION_KEY] = user.id
    return RedirectResponse("/org", status_code=303)  # или "/org"
