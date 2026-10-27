"""task: relax check constraint to at-most-one

Revision ID: aa8904c1e96f
Revises: f6a03add5f00
Create Date: 2025-09-23 16:21:04.195038+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'aa8904c1e96f'
down_revision: Union[str, Sequence[str], None] = 'f6a03add5f00'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
