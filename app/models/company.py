from __future__ import annotations
from datetime import datetime
from typing import Optional

from sqlalchemy import Integer, String, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class Company(Base):
    __tablename__ = "company"
    __table_args__ = (
        UniqueConstraint("org_id", "inn", name="uq_company_org_inn"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    org_id: Mapped[int] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    department_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("department.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    # ключевые поля
    inn: Mapped[str] = mapped_column(String(12), nullable=False)  # 10=юрлицо, 12=ИП
    ogrn: Mapped[Optional[str]] = mapped_column(String(15))
    kpp: Mapped[Optional[str]] = mapped_column(String(9))
    short_name: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String(500))
    ceo_name: Mapped[Optional[str]] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="LEGAL")  # LEGAL | IP

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # relations
    organization = relationship("Organization")
    department = relationship("Department")
    emails = relationship("CompanyEmail", back_populates="company", cascade="all, delete-orphan")
    tasks = relationship("Task", back_populates="company")  # FK на task.company_id
