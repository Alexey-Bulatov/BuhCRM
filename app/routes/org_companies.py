from __future__ import annotations
from pathlib import Path
from typing import List, Optional
from fastapi import APIRouter, Depends, Form, Query, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.services.auth import get_db, require_org_user, get_org_for_action
from app.models.user import User
from app.models.organization import Organization
from app.models.department import Department
from app.models.user_department import UserDepartment
from app.models.company import Company
from app.models.company_email import CompanyEmail
from app.services.dadata import get_company_by_inn

router = APIRouter(prefix="/org/clients", tags=["Clients"])
TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

def _flash(request: Request, msg: str, kind: str = "success") -> None:
    request.session.setdefault("_flash", {})
    request.session["_flash"][kind] = msg

def _take_flash(request: Request):
    data = request.session.pop("_flash", None) or {}
    return data.get("success"), data.get("error")

def _user_department_ids(db: Session, user_id: int) -> set[int]:
    return {r.department_id for r in db.query(UserDepartment).filter(UserDepartment.user_id == user_id).all()}

# LIST
@router.get("", response_class=HTMLResponse)
def clients_list(
    request: Request,
    user: User = Depends(require_org_user),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
    q: str | None = Query(None, description="строка поиска по названию/ИНН"),
    only: str | None = Query(None, description="mine|all"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=5, le=100),
):
    """
    Список клиентов с фильтром по q и опцией only=mine, плюс пагинация.
    """
    org: Organization = get_org_for_action(db, user, org_id)
    my_depts = _user_department_ids(db, user.id)

    qry = db.query(Company).filter(Company.org_id == org.id)

    if q:
        like = f"%{q.strip()}%"
        qry = qry.filter(or_(Company.short_name.ilike(like),
                             Company.full_name.ilike(like),
                             Company.inn.ilike(like)))

    if only == "mine":
        qry = qry.filter(Company.department_id.in_(my_depts) if my_depts else Company.id == -1)

    # --- Пагинация ---
    total = qry.count()
    pages_total = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages_total)  # на всякий случай если page слишком большой
    offset = (page - 1) * per_page

    items = (
        qry.order_by(Company.id.desc())
           .offset(offset)
           .limit(per_page)
           .all()
    )

    ok, err = _take_flash(request)
    return templates.TemplateResponse("org/clients.html", {
        "request": request,
        "org": org,
        "user": user,
        "items": items,
        "q": q or "",
        "only": only or "",
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages_total": pages_total,
        "flash_success": ok,
        "flash_error": err,
    })

# NEW FORM
@router.get("/new", response_class=HTMLResponse)
def client_new_form(
    request: Request,
    user: User = Depends(require_org_user),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    # можно добавлять только в своё подразделение
    my_depts = _user_department_ids(db, user.id)
    depts = db.query(Department).filter(Department.org_id == org.id, Department.id.in_(my_depts)).all()
    return templates.TemplateResponse("org/client_form.html", {
        "request": request, "org": org, "user": user, "depts": depts, "item": None
    })

# CREATE
@router.post("/new")
def client_new_submit(
    request: Request,
    inn: str = Form(...),
    department_id: int = Form(...),
    emails: str = Form(""),
    user: User = Depends(require_org_user),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    # проверка департамента — должен быть одним из моих
    my_depts = _user_department_ids(db, user.id)
    if department_id not in my_depts:
        _flash(request, "Можно добавлять клиентов только в свои подразделения", "error")
        return RedirectResponse("/org/clients/new", status_code=303)

    inn = (inn or "").strip()
    if not inn.isdigit() or len(inn) not in (10, 12):
        _flash(request, "ИНН должен быть 10 или 12 цифр", "error")
        return RedirectResponse("/org/clients/new", status_code=303)

    # уникальность в рамках организации
    if db.query(Company).filter(Company.org_id == org.id, Company.inn == inn).first():
        _flash(request, "Клиент с таким ИНН уже существует в вашей организации", "error")
        return RedirectResponse("/org/clients", status_code=303)

    d = get_company_by_inn(inn)
    if not d.get("success"):
        _flash(request, d.get("error") or "Не удалось получить данные из Dadata", "error")
        return RedirectResponse("/org/clients/new", status_code=303)

    kind = "LEGAL" if len(inn) == 10 else "IP"
    comp = Company(
        org_id=org.id,
        department_id=department_id,
        inn=inn,
        ogrn=d.get("ogrn"),
        kpp=d.get("kpp"),
        short_name=d.get("short_name") or "Компания",
        full_name=d.get("full_name"),
        ceo_name=d.get("ceo_name"),
        kind=kind,
    )
    db.add(comp); db.flush()

    # emails: разделитель — запятая/пробел/точка с запятой/перевод строки
    raw = emails or ""
    parts = [e.strip() for chunk in raw.replace(";", ",").split(",") for e in chunk.split() if e.strip()]
    uniq = []
    for e in parts:
        if e not in uniq:
            uniq.append(e)
    for e in uniq:
        db.add(CompanyEmail(company_id=comp.id, email=e))

    db.commit()
    _flash(request, "Клиент создан")
    return RedirectResponse(f"/org/clients/{comp.id}", status_code=303)

# DETAIL
@router.get("/{cid}", response_class=HTMLResponse)
def client_detail(
    cid: int,
    request: Request,
    user: User = Depends(require_org_user),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    comp = db.query(Company).filter(Company.id == cid, Company.org_id == org.id).first()
    if not comp:
        _flash(request, "Клиент не найден", "error")
        return RedirectResponse("/org/clients", status_code=303)

    ok, err = _take_flash(request)
    return templates.TemplateResponse("org/client_detail.html", {
        "request": request, "org": org, "user": user, "item": comp,
        "flash_success": ok, "flash_error": err
    })

# EMAILS
@router.post("/{cid}/emails/add")
def client_add_email(
    cid: int,
    email: str = Form(...),
    user: User = Depends(require_org_user),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    comp = db.query(Company).filter(Company.id == cid, Company.org_id == org.id).first()
    if not comp:
        _flash(request, "Клиент не найден", "error")
        return RedirectResponse("/org/clients", status_code=303)

    e = (email or "").strip()
    if not e:
        _flash(request, "Email пуст", "error")
        return RedirectResponse(f"/org/clients/{cid}", status_code=303)

    # уникальность внутри компании
    if db.query(CompanyEmail).filter(CompanyEmail.company_id == comp.id, CompanyEmail.email == e).first():
        _flash(request, "Такой email уже добавлен", "error")
        return RedirectResponse(f"/org/clients/{cid}", status_code=303)

    db.add(CompanyEmail(company_id=comp.id, email=e))
    db.commit()
    _flash(request, "Email добавлен")
    return RedirectResponse(f"/org/clients/{cid}", status_code=303)

@router.post("/{cid}/emails/{eid}/delete")
def client_delete_email(
    cid: int, eid: int,
    user: User = Depends(require_org_user),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    comp = db.query(Company).filter(Company.id == cid, Company.org_id == org.id).first()
    if not comp:
        _flash(request, "Клиент не найден", "error")
        return RedirectResponse("/org/clients", status_code=303)

    row = db.query(CompanyEmail).filter(CompanyEmail.id == eid, CompanyEmail.company_id == comp.id).first()
    if row:
        db.delete(row); db.commit()
        _flash(request, "Email удалён")
    return RedirectResponse(f"/org/clients/{cid}", status_code=303)

# QUICK: создать задачу из карточки
@router.post("/{cid}/new-task")
def client_new_task_from_card(
    cid: int,
    user: User = Depends(require_org_user),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
):
    org: Organization = get_org_for_action(db, user, org_id)
    comp = db.query(Company).filter(Company.id == cid, Company.org_id == org.id).first()
    if not comp:
        return RedirectResponse("/org/clients", status_code=303)
    # передадим company_id в форму создания задачи
    return RedirectResponse(f"/org/tasks/new?company_id={comp.id}", status_code=303)
