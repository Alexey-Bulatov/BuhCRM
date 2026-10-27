from __future__ import annotations
from sqlalchemy import Integer, String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base

class CompanyEmail(Base):
    __table_args__ = (
        UniqueConstraint("company_id", "email", name="uq_company_email"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id", ondelete="CASCADE"), index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)

    company = relationship("Company", back_populates="emails")
