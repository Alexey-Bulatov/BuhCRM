from __future__ import annotations
from typing import Optional
from fastapi import Depends, HTTPException, status, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.user import User
from app.core.security import verify_password
from app.models.organization import Organization

SESSION_KEY = "uid"

def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid: Optional[int] = request.session.get(SESSION_KEY)
    if not uid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    user = db.query(User).filter(User.id == uid, User.is_active == True).first()  # noqa: E712
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return user

def require_system_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_system_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="System admin only")
    return user

def authenticate(db: Session, email: str, password: str) -> Optional[User]:
    user = db.query(User).filter(User.email == email).first()
    if user and user.is_active and (user.is_system_admin or user.org_role is not None):
        if verify_password(password, user.password_hash):
            return user
    return None

def require_org_user(user: User = Depends(get_current_user)) -> User:
    if user.is_system_admin:
        # для org-роутов системному админу нужен явный org_id
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Org context required (not system admin)")
    if not user.org_id or not user.org_role:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Organization access required")
    return user

def require_org_admin(user: User = Depends(get_current_user)) -> User:
    # Сисадмин проходит
    if user.is_system_admin:
        return user
    if not user.org_id or user.org_role != "ORG_ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Organization admin required")
    return user

def get_user_org_or_404(user: User, db: Session) -> Organization:
    if user.is_system_admin:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Explicit org_id required for system admin")
    if not user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User has no organization")
    org = db.query(Organization).filter(Organization.id == user.org_id).first()
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    return org

def get_org_for_action(db: Session, user: User, org_id: int | None) -> Organization:
    """
    Универсально достаём организацию:
    - sysadmin: org_id обязателен
    - обычный: org_id None -> своя; задан чужой -> 403
    """
    if user.is_system_admin:
        if not org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="org_id is required for system admin")
        org = db.query(Organization).filter(Organization.id == org_id).first()
        if not org:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
        return org

    if not user.org_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Organization access required")
    if org_id and org_id != user.org_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden for this organization")
    org = db.query(Organization).filter(Organization.id == user.org_id).first()
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    return org
