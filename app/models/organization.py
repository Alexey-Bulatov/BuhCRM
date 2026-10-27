from __future__ import annotations
from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base

class Organization(Base):
    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Короткое и полное названия (из Dadata)
    short_name: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)

    inn: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    ogrn: Mapped[Optional[str]] = mapped_column(String(15), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", nullable=False)

    ceo_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
