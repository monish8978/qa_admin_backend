"""Monthly UsageMetric upserts — mirrors apps/api/src/billing/usage-meter.service.ts.

Period boundaries follow the local calendar month (start = 1st 00:00:00,
end = last day 23:59:59.999).
"""
from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..models.master import UsageMetric


def current_period(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Single source of truth for the current monthly usage period boundaries.

    All writers/readers of UsageMetric (meter, billing display, auth login,
    eval worker) MUST use this so the (tenantId, periodStart, periodEnd) unique
    key is consistent and limit enforcement matches billing display.

    start = 1st of month 00:00:00, end = last day 23:59:59.999 (UTC).
    """
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_day = monthrange(now.year, now.month)[1]
    end = now.replace(
        day=last_day, hour=23, minute=59, second=59, microsecond=999_000
    )
    return start, end


# Backwards-compatible private alias.
_current_period = current_period


def _upsert(
    db: Session,
    tenant_id: str,
    *,
    conversations_delta: int = 0,
    tokens_delta: int = 0,
    cost_cents_delta: int = 0,
    active_users_delta: int = 0,
) -> None:
    start, end = _current_period()
    stmt = pg_insert(UsageMetric.__table__).values(
        tenantId=tenant_id,
        periodStart=start,
        periodEnd=end,
        conversationsProcessed=max(conversations_delta, 0),
        aiTokensUsed=max(tokens_delta, 0),
        aiCostCents=max(cost_cents_delta, 0),
        activeUsers=max(active_users_delta, 0),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["tenantId", "periodStart", "periodEnd"],
        set_={
            "conversationsProcessed": UsageMetric.__table__.c.conversationsProcessed
            + conversations_delta,
            "aiTokensUsed": UsageMetric.__table__.c.aiTokensUsed + tokens_delta,
            "aiCostCents": UsageMetric.__table__.c.aiCostCents + cost_cents_delta,
            "activeUsers": UsageMetric.__table__.c.activeUsers + active_users_delta,
        },
    )
    db.execute(stmt)
    db.commit()


def record_conversation(db: Session, tenant_id: str, count: int = 1) -> None:
    if count <= 0:
        return
    _upsert(db, tenant_id, conversations_delta=count)


def record_ai_usage(
    db: Session, tenant_id: str, tokens_used: int, cost_cents: int
) -> None:
    if tokens_used == 0 and cost_cents == 0:
        return
    _upsert(db, tenant_id, tokens_delta=tokens_used, cost_cents_delta=cost_cents)


def get_monthly_conversation_count(db: Session, tenant_id: str) -> int:
    start, end = _current_period()
    row = db.execute(
        UsageMetric.__table__.select().where(
            (UsageMetric.tenantId == tenant_id)
            & (UsageMetric.periodStart == start)
            & (UsageMetric.periodEnd == end)
        )
    ).mappings().first()
    if not row:
        return 0
    return int(row["conversationsProcessed"])
