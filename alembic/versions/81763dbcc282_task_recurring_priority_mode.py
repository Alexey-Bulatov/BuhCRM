"""task: recurring + priority_mode

Revision ID: 81763dbcc282
Revises: a9d8eef7a757
Create Date: 2025-09-23 12:24:38.188082+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '81763dbcc282'
down_revision: Union[str, Sequence[str], None] = 'a9d8eef7a757'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


from alembic import op
import sqlalchemy as sa

def upgrade():
    # priority_mode / priority — добавляем с server_default, чтобы не упасть на существующих строках
    op.add_column('task', sa.Column('priority_mode', sa.String(length=16), nullable=False, server_default='AUTO'))
    # priority already exists in the initial revision; adding it again breaks fresh installs.

    # рекуррентность — все nullable, без дефолтов
    op.add_column('task', sa.Column('recur_freq', sa.String(length=16), nullable=False, server_default='NONE'))
    op.add_column('task', sa.Column('recur_interval', sa.Integer(), nullable=False, server_default='1'))
    op.add_column('task', sa.Column('recur_until', sa.Date(), nullable=True))

    # если надо индексы — можно добавить тут
    # op.create_index('ix_task_recur_freq', 'task', ['recur_freq'])

    # теперь уберём server_default (чтобы апп контролировал значения)
    op.alter_column('task', 'priority_mode', server_default=None)
    op.alter_column('task', 'priority', server_default=None)
    op.alter_column('task', 'recur_freq', server_default=None)
    op.alter_column('task', 'recur_interval', server_default=None)


def downgrade():
    # порядок обратный
    # если создавал индексы — сначала дропни их
    # op.drop_index('ix_task_recur_freq', table_name='task')
    op.drop_column('task', 'recur_until')
    op.drop_column('task', 'recur_interval')
    op.drop_column('task', 'recur_freq')
    op.drop_column('task', 'priority_mode')
