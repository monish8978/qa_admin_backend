"""add featureFlags column to tenants

Revision ID: a1b2c3d4e5f6
Revises: 5a475445034b
Create Date: 2026-07-19 04:30:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = 'a1b2c3d4e5f6'
down_revision: str | Sequence[str] | None = '5a475445034b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add featureFlags JSONB column to tenants table
    # IF NOT EXISTS se safe hai agar already exist kare
    op.execute("""
        ALTER TABLE tenants
        ADD COLUMN IF NOT EXISTS "featureFlags" JSONB DEFAULT '{}'::jsonb
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE tenants
        DROP COLUMN IF EXISTS "featureFlags"
    """)
