from __future__ import annotations
from sqlalchemy import Integer, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .base import Base

class UserDepartment(Base):
    __table_args__ = (
        UniqueConstraint("user_id", "department_id", name="uq_user_department_user_id_department_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    department_id: Mapped[int] = mapped_column(ForeignKey("department.id", ondelete="CASCADE"), nullable=False)

    user = relationship("User", backref="user_departments")
    department = relationship("Department", backref="user_departments")
