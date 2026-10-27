# alembic/env.py
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

# --- гарантируем, что корень проекта в PYTHONPATH ---
# (на случай запуска alembic из подкаталога)
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# --- импортируем metadata и ВСЕ модели, чтобы Alembic их «видел» ---
from app.models.base import Base  # Base.metadata — цель миграций
# ВАЖНО: просто импортируем модули моделей, чтобы они зарегистрировались в Base.metadata
from app.models import (
    organization,
    user,
    department,
    user_department,
    task,
    task_history,
    company,
    company_email,
)  # noqa: F401

# Берём ГОТОВЫЙ engine из приложения (тот же DSN/пароль, что у uvicorn)
from app.db import engine as app_engine

# Alembic Config object
config = context.config

# Логирование Alembic (берётся из alembic.ini)
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Можно прописать url в конфиг чисто для информации (не обязательно)
# config.set_main_option("sqlalchemy.url", str(app_engine.url))

# Метадата для автогенерации
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Запуск в offline-режиме (генерация SQL без подключения к БД)."""
    url = app_engine.url.render_as_string(hide_password=False)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Запуск в online-режиме (с реальным подключением)."""
    connectable = app_engine  # используем уже созданный engine приложения
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
