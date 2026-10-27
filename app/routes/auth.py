from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from urllib.parse import urlsplit

from app.services.auth import get_db, authenticate, SESSION_KEY

router = APIRouter(tags=["Auth"])
templates = Jinja2Templates(directory="app/templates")

@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse("auth/login.html", {"request": request, "error": None})

@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = authenticate(db, email, password)
    if not user:
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "error": "Неверный логин или пароль"},
            status_code=401,
        )

    request.session.clear()
    request.session[SESSION_KEY] = user.id

    # опционально поддержим ?next=...
    next_url = request.query_params.get("next")
    if _safe_next_url(next_url):
        return RedirectResponse(next_url, status_code=303)

    # ✅ система → /admin, орг-пользователь → /org
    if user.is_system_admin:
        return RedirectResponse("/admin", status_code=303)
    return RedirectResponse("/org", status_code=303)


def _safe_next_url(value: str | None) -> bool:
    if not value or not value.startswith("/") or value.startswith("//"):
        return False
    if "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    parsed = urlsplit(value)
    return not parsed.scheme and not parsed.netloc

@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)

@router.get("/not-implemented", response_class=HTMLResponse)
def not_impl(request: Request):
    return templates.TemplateResponse("plain.html", {"request": request, "title": "В разработке", "text": "Орг-админка скоро будет."})
