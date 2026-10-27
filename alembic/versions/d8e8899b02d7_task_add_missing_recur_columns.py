"""task: add missing recur columns

Revision ID: d8e8899b02d7
Revises: 81763dbcc282
Create Date: 2025-09-23 14:22:17.469451+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd8e8899b02d7'
down_revision: Union[str, Sequence[str], None] = '81763dbcc282'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    # 1) Добавляем recur_* с server_default, чтобы не упасть на существующих строках
    # Previous deployed databases sometimes had these added by hand. Fresh installs
    # already receive them from 81763dbcc282, so add only genuinely missing columns.
    columns = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('task')}
    if 'recur_freq' not in columns:
        op.add_column('task', sa.Column('recur_freq', sa.String(length=16), nullable=False, server_default='NONE'))
    if 'recur_interval' not in columns:
        op.add_column('task', sa.Column('recur_interval', sa.Integer(), nullable=False, server_default='1'))
    if 'recur_until' not in columns:
        op.add_column('task', sa.Column('recur_until', sa.Date(), nullable=True))

    # 2) На всякий случай подчистим возможные NULL-ы (если БД позволила им появиться)
    op.execute("UPDATE task SET recur_freq = 'NONE' WHERE recur_freq IS NULL;")
    op.execute("UPDATE task SET recur_interval = 1 WHERE recur_interval IS NULL;")
    # recur_until может быть NULL — это ок, ничего не трогаем

    # 3) Убираем server_default — дальше приложение будет контролировать значения
    op.alter_column('task', 'recur_freq', server_default=None)
    op.alter_column('task', 'recur_interval', server_default=None)

    # 4) Приводим priority_mode/priority к виду без серверных дефолтов (раз уж
    #    автоген это заметил и постоянно будет «мигать» в ревизиях)
    op.alter_column('task', 'priority_mode', server_default=None)
    op.alter_column('task', 'priority', server_default=None)

def downgrade():
    # These columns are part of the parent revision and must survive this downgrade.
    pass
