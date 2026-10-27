from __future__ import annotations
from typing import Optional
from sqlalchemy import String, Integer, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base

class User(Base):
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    short_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    is_system_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    org_id: Mapped[Optional[int]] = mapped_column(ForeignKey("organization.id", ondelete="SET NULL"), nullable=True)
    org_role: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # ORG_ADMIN | ORG_USER

    organization = relationship("Organization", backref="users", lazy="joined")
