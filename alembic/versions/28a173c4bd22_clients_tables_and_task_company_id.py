"""clients tables and task.company_id

Revision ID: 28a173c4bd22
Revises: 983e7d230376
Create Date: 2025-09-22 09:27:51.230904+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '28a173c4bd22'
down_revision: Union[str, Sequence[str], None] = '983e7d230376'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
