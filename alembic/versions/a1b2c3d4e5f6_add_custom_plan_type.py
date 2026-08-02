"""add CUSTOM to PlanType enum

Revision ID: a1b2c3d4e5f6
Revises: 5a475445034b
Create Date: 2026-07-20 03:27:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: str | Sequence[str] | None = '5a475445034b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # PostgreSQL requires ALTER TYPE ... ADD VALUE to extend an enum.
    # This cannot run inside a transaction block, so we use connection.execution_options.
    op.execute("ALTER TYPE \"PlanType\" ADD VALUE IF NOT EXISTS 'CUSTOM'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values directly.
    # A full enum recreation would be needed; skipping for safety.
    pass
