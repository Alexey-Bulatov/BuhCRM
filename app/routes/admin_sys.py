# app/routes/admin_sys.py
from __future__ import annotations
from pathlib import Path
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.services.auth import require_system_admin, get_db  # у тебя файл называется auth.py — оставляем так
from app.models.organization import Organization
from app.models.user import User
from app.models.department import Department
from app.services.org import create_organization_with_admin_from_inn

router = APIRouter(prefix="/admin", tags=["System Admin"])

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

def _back_url(request: Request, default: str) -> str:
    return request.query_params.get("back") or request.headers.get("referer") or default

@router.get("", response_class=HTMLResponse)
def admin_home(request: Request, user=Depends(require_system_admin), db: Session = Depends(get_db)):
    orgs_count = db.query(Organization).count()
    users_count = db.query(User).count()
    depts_count = db.query(Department).count()
    return templates.TemplateResponse(
        "admin/dashboard.html",
        {"request": request, "user": user,
         "orgs_count": orgs_count, "users_count": users_count, "depts_count": depts_count,
         "back_url": _back_url(request, "/admin")}
    )

@router.get("/orgs", response_class=HTMLResponse)
def list_orgs(request: Request, user=Depends(require_system_admin), db: Session = Depends(get_db)):
    orgs = db.query(Organization).order_by(Organization.id.desc()).all()
    return templates.TemplateResponse(
        "admin/org_list.html",
        {"request": request, "user": user, "orgs": orgs, "back_url": _back_url(request, "/admin")}
    )

@router.get("/orgs/new", response_class=HTMLResponse)
def new_org_form(request: Request, user=Depends(require_system_admin)):
    return templates.TemplateResponse(
        "admin/org_new.html",
        {"request": request, "user": user, "error": None, "back_url": _back_url(request, "/admin/orgs")}
    )

@router.post("/orgs/new")
def new_org_submit(
    request: Request,
    inn: str = Form(...),
    admin_full_name: str = Form(...),
    admin_short_name: str = Form(""),
    admin_email: str = Form(...),
    admin_password: str = Form(...),
    user=Depends(require_system_admin),
    db: Session = Depends(get_db),
):
    try:
        create_organization_with_admin_from_inn(
            db,
            inn=inn.strip(),
            admin_full_name=admin_full_name.strip(),
            admin_short_name=(admin_short_name or "").strip() or None,
            admin_email=admin_email.strip(),
            admin_password=admin_password,
        )
        return RedirectResponse(url="/admin/orgs?created=1", status_code=303)
    except Exception as e:
        return templates.TemplateResponse(
            "admin/org_new.html",
            {"request": request, "user": user, "error": str(e), "back_url": _back_url(request, "/admin/orgs")},
            status_code=400,
        )

@router.get("/orgs/{org_id}", response_class=HTMLResponse)
def org_detail(org_id: int, request: Request, user=Depends(require_system_admin), db: Session = Depends(get_db)):
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        return RedirectResponse(url="/admin/orgs", status_code=303)

    admins = [u for u in org.users if u.org_role == "ORG_ADMIN"]
    users = [u for u in org.users if u.org_role == "ORG_USER"]
    return templates.TemplateResponse(
        "admin/org_detail.html",
        {"request": request, "user": user, "org": org,
         "admins": admins, "users": users, "back_url": _back_url(request, "/admin/orgs")}
    )
