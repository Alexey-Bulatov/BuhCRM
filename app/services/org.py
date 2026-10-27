from __future__ import annotations
from typing import Optional

from sqlalchemy.orm import Session

from app.models.organization import Organization
from app.models.department import Department
from app.models.user import User
from app.models.user_department import UserDepartment
from app.core.security import is_password_strong, hash_password
from app.services.dadata import get_party_by_inn

def create_organization_with_admin(
    db: Session,
    *,
    short_name: str,
    full_name: Optional[str],
    inn: str,
    ogrn: Optional[str],
    ceo_name: Optional[str],
    status: str = "TRIAL",  # для dev по умолчанию можно TRIAL
    admin_full_name: str,
    admin_short_name: Optional[str],
    admin_email: str,
    admin_password: str,
) -> tuple[Organization, User, Department]:
    # Валидация пароля
    if not is_password_strong(admin_password):
        raise ValueError("Пароль не соответствует политике сложности.")

    # Глобальная уникальность email должна соблюдаться
    existing = db.query(User).filter(User.email == admin_email).first()
    if existing:
        raise ValueError("Пользователь с таким email уже существует (глобально).")

    # Создаем организацию
    org = Organization(
        short_name=short_name,
        full_name=full_name,
        inn=inn,
        ogrn=ogrn,
        status=status,
        ceo_name=ceo_name,
    )
    db.add(org)
    db.flush()  # получить org.id

    # Создаем первого админа организации
    admin_user = User(
        full_name=admin_full_name,
        short_name=admin_short_name,
        email=admin_email,
        password_hash=hash_password(admin_password),
        is_system_admin=False,
        is_active=True,
        org_id=org.id,
        org_role="ORG_ADMIN",
    )
    db.add(admin_user)
    db.flush()

    # Создаем авто-подразделение «Основное»
    dept = Department(
        org_id=org.id,
        name="Основное",
    )
    db.add(dept)
    db.flush()

    # Включаем админа в «Основное»
    db.add(UserDepartment(user_id=admin_user.id, department_id=dept.id))

    db.commit()
    db.refresh(org)
    db.refresh(admin_user)
    db.refresh(dept)
    return org, admin_user, dept

def create_organization_with_admin_from_inn(
    db: Session,
    *,
    inn: str,
    admin_full_name: str,
    admin_short_name: Optional[str],
    admin_email: str,
    admin_password: str,
) -> tuple[Organization, User, Department]:
    # Тянем карточку из Dadata
    p = get_party_by_inn(inn)
    if not p.get("success"):
        raise ValueError(p.get("error") or "Не удалось получить данные организации")
    # Маппим в нашу модель
    short_name = p["short_name"]
    full_name = p["full_name"]
    ogrn = p["ogrn"]
    ceo_name = p["ceo_name"]

    return create_organization_with_admin(
        db,
        short_name=short_name,
        full_name=full_name,
        inn=inn,
        ogrn=ogrn,
        ceo_name=ceo_name,
        status="TRIAL",
        admin_full_name=admin_full_name,
        admin_short_name=admin_short_name,
        admin_email=admin_email,
        admin_password=admin_password,
    )
