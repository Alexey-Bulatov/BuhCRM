from __future__ import annotations
from datetime import datetime, date
from typing import Optional

from sqlalchemy import (
    Integer, String, Text, DateTime, Date, ForeignKey, CheckConstraint, func
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
from app.core.enums import TaskStatus, TaskPriority


class Task(Base):
    __tablename__ = "task"
    __table_args__ = (
        # Запрещаем назначение одновременно и на отдел, и на пользователя.
        # При этом "без назначения" (оба NULL) допускаем — под текущую логику /org/tasks/new.
        CheckConstraint(
            "NOT (department_id IS NOT NULL AND assignee_user_id IS NOT NULL)",
            name="target_at_most_one",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    org_id: Mapped[int] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # статус — используем те же значения, что и в коде (OPEN/DONE/CANCELED)
    status: Mapped[str] = mapped_column(
        String(32),
        default=TaskStatus.OPEN.value,   # или "OPEN"
        nullable=False,
    )

    # режим приоритета: AUTO / MANUAL
    priority_mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="AUTO",          # ORM default
        server_default="AUTO",   # DB default, на будущее
    )
    priority: Mapped[str] = mapped_column(
        String(16),
        default=TaskPriority.MEDIUM.value,  # или "MEDIUM"
        nullable=False,
    )

    # дедлайн
    due_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # рекуррентность
    recur_freq: Mapped[str] = mapped_column(String(16), default="NONE", nullable=False)  # NONE/DAILY/WEEKLY/MONTHLY
    recur_interval: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    recur_until: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    # назначение
    department_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("department.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    assignee_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    # автор/время
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # клиент (опционально)
    company_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("company.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    # relations
    organization = relationship("Organization")
    department = relationship("Department")
    assignee = relationship("User", foreign_keys=[assignee_user_id])
    creator = relationship("User", foreign_keys=[created_by])
    company = relationship("Company", back_populates="tasks")

    # удобные алиасы, если используешь их где-то в коде/шаблонах
    @property
    def assignee_id(self) -> Optional[int]:
        return self.assignee_user_id

    @assignee_id.setter
    def assignee_id(self, v: Optional[int]) -> None:
        self.assignee_user_id = v

    @property
    def due_at(self) -> Optional[datetime]:
        return self.due_date

    @due_at.setter
    def due_at(self, v: Optional[datetime]) -> None:
        self.due_date = v
