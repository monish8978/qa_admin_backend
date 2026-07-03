"""message

Revision ID: 5a475445034b
Revises: 44f23c78eb2f
Create Date: 2026-06-03 23:35:32.710796

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '5a475445034b'
down_revision: str | Sequence[str] | None = '44f23c78eb2f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
