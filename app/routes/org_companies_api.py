# app/routes/org_companies_api.py
from __future__ import annotations
from typing import Optional, List
import json
import re

import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy import case, or_, func

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from app.db import get_db
from app.services.auth import get_db, require_org_user, get_org_for_action, get_current_user
from app.models.organization import Organization
from app.models.user import User
from app.models.company import Company  # id, org_id, short_name, full_name, inn, email?

# Опциональная таблица с e-mail'ами (расплющенная)
try:
    from app.models.company_email import CompanyEmail  # company_id, email
    HAS_COMPANY_EMAIL = True
except Exception:
    HAS_COMPANY_EMAIL = False

router = APIRouter(prefix="/org/api/companies", tags=["Companies API"])

EMAIL_RE = re.compile(r'[\w\.\+\-]+@[\w\.\-]+\.\w+')

def _norm_email(s: str | None) -> Optional[str]:
    if not s:
        return None
    m = EMAIL_RE.search(s)
    return m.group(0).lower() if m else None

def _term_is_inn(term: str) -> bool:
    return term.isdigit() and len(term) in (10, 12)

@router.get("/search")
def search_companies(
    user: User = Depends(require_org_user),
    db: Session = Depends(get_db),
    org_id: int | None = Query(None),
    term: str = Query("", min_length=1, description="ИНН, часть названия или email"),
    dept_id: int | None = Query(None, description="необязательно — сужение по подразделению"),
    limit: int = Query(20, ge=1, le=50),
):
    """
    Поиск компаний по:
      - ИНН (точно / подстрока),
      - short_name / full_name,
      - email (Company.email и CompanyEmail.email).
    Возвращает: [{id, label, inn, email}]
    """
    term_raw = (term or "").strip()
    if not term_raw:
        return JSONResponse([])

    org: Organization = get_org_for_action(db, user, org_id)

    q = db.query(Company).filter(Company.org_id == org.id)

    if dept_id is not None and hasattr(Company, "responsible_department_id"):
        q = q.filter(Company.responsible_department_id == dept_id)

    t = term_raw.lower()
    filters = []

    # --- INN ---
    if _term_is_inn(t):
        filters.append(func.replace(Company.inn, " ", "") == t)  # точное совпадение
    filters.append(sa.cast(Company.inn, sa.Text).ilike(f"%{t}%"))

    # --- Names ---
    short_col = getattr(Company, "short_name", None)
    full_col = getattr(Company, "full_name", None)
    if short_col is not None:
        filters.append(short_col.ilike(f"%{t}%"))
    if full_col is not None:
        filters.append(full_col.ilike(f"%{t}%"))

    # --- Email in Company.email (строковая колонка) ---
    email_col = getattr(Company, "email", None)
    if email_col is not None:
        filters.append(sa.cast(email_col, sa.Text).ilike(f"%{t}%"))

    # --- Email in CompanyEmail.email (LEFT JOIN при наличии таблицы) ---
    ce = None
    if HAS_COMPANY_EMAIL:
        ce = CompanyEmail
        q = q.outerjoin(ce, ce.company_id == Company.id)
        filters.append(sa.cast(ce.email, sa.Text).ilike(f"%{t}%"))

    # Итоговый фильтр
    q = q.filter(or_(*filters)).group_by(Company.id)

    # --- Сортировка ---
    # приоритет точного ИНН
    exact_inn = case((func.replace(func.coalesce(Company.inn, ""), " ", "") == t, 0), else_=1)

    # приоритет точного e-mail (если term похоже на email)
    term_email = _norm_email(term_raw) or ""
    exact_email_cases = []
    if term_email:
        if email_col is not None:
            exact_email_cases.append(
                case((func.lower(sa.cast(email_col, sa.Text)) == func.lower(term_email), 0), else_=1)
            )
        if HAS_COMPANY_EMAIL and ce is not None:
            exact_email_cases.append(
                case((func.lower(sa.cast(ce.email, sa.Text)) == func.lower(term_email), 0), else_=1)
            )
    exact_email_rank = func.least(*exact_email_cases) if exact_email_cases else sa.literal(1)

    display_name = (
        func.coalesce(short_col, full_col)
        if short_col is not None and full_col is not None
        else (short_col or full_col or sa.literal(""))
    )

    q = q.order_by(
        exact_inn.asc(),
        exact_email_rank.asc(),
        func.length(display_name).asc(),
        Company.id.desc(),
    ).limit(limit)

    rows: List[Company] = q.all()

    # --- Ответ: подберём «первый email» для подсказки (без relationship'ов) ---
    result = []
    for c in rows:
        inn = (c.inn or "").strip()
        label = (getattr(c, "short_name", None) or getattr(c, "full_name", None) or f"Компания #{c.id}")

        primary_email: Optional[str] = None
        # 1) company.email (строка)
        if hasattr(c, "email") and isinstance(c.email, str) and c.email:
            primary_email = _norm_email(c.email)

        # 2) первый email из CompanyEmail (при наличии таблицы)
        if not primary_email and HAS_COMPANY_EMAIL:
            try:
                ce_row = (
                    db.query(CompanyEmail)
                    .filter(CompanyEmail.company_id == c.id)
                    .order_by(CompanyEmail.id.asc())
                    .first()
                )
                if ce_row and ce_row.email:
                    primary_email = _norm_email(ce_row.email)
            except Exception:
                primary_email = primary_email or None

        result.append({
            "id": c.id,
            "label": f"{label}{f' ({inn})' if inn else ''}",
            "inn": inn or "",
            "email": primary_email or "",
        })

    return JSONResponse(result)
