"""create form_definitions table

Revision ID: 44f23c78eb2f
Revises: 
Create Date: 2026-06-03 23:34:48.065968

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '44f23c78eb2f'
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
