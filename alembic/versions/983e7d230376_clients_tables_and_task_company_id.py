"""clients tables and task.company_id

Revision ID: 983e7d230376
Revises: 6a98fe3cdaf8
Create Date: 2025-09-22 09:25:29.553034+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '983e7d230376'
down_revision: Union[str, Sequence[str], None] = '6a98fe3cdaf8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
