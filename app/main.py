from __future__ import annotations

import os

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.core.config import CLONE_MODE, ENV, env_flag, required_env
from app.db import get_db
from app.models.user import User
from app.routes import (
    admin_sys, auth, org_admin, org_companies, org_companies_api,
    org_tasks, org_users, register,
)
from app.services.auth import SESSION_KEY

app = FastAPI(title="BuhCRM")
app.state.env = ENV
app.state.clone_mode = CLONE_MODE

app.add_middleware(
    SessionMiddleware,
    secret_key=required_env("SECRET_KEY"),
    same_site="lax",
    session_cookie=os.getenv(
        "SESSION_COOKIE_NAME", "buhcrm2_clone_session" if CLONE_MODE else "buhcrm2_session"
    ),
    https_only=env_flag("COOKIE_SECURE"),
)

# Required routes fail loudly at startup instead of silently disappearing.
# The organization dashboard is registered only by org_admin.
for module in (auth, admin_sys, org_admin, org_users, org_tasks, register,
               org_companies, org_companies_api):
    app.include_router(module.router)


@app.get("/", include_in_schema=False)
def root(request: Request, db: Session = Depends(get_db)):
    uid = request.session.get(SESSION_KEY)
    user = db.get(User, uid) if uid else None
    if not user or not user.is_active:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/admin" if user.is_system_admin else "/org", status_code=303)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"ok": True, "env": app.state.env}
