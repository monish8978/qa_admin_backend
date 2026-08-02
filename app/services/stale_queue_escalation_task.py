"""Celery task: eval.escalate.scan — port of stale-queue-escalation.service.ts.

Runs every 30 minutes (configured via celery_app beat schedule). For each
ACTIVE tenant, finds QA/VERIFIER queue items past their dueBy or older than
the configured stale threshold and promotes them to ESCALATION_QUEUE.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select

from ..celery_app import celery_app
from ..db import SessionLocal
from ..models.master import EscalationRule, Tenant
from ..models.tenant import AuditLog, Evaluation, WorkflowQueue
from .outbound_webhooks_service import deliver as deliver_webhook
from .tenant_pool import get_tenant_pool

log = logging.getLogger("qa.worker.stale_escalation")


def _escalate_for_tenant(master, tenant_id: str) -> int:
    pool = get_tenant_pool()
    rule = master.execute(
        select(EscalationRule).where(EscalationRule.tenantId == tenant_id)
    ).scalar_one_or_none()
    stale_hours = int(rule.staleQueueHours) if rule else 24
    now = datetime.now(timezone.utc)
    stale_threshold = now - timedelta(hours=stale_hours)
    escalated_count = 0

    with pool.session(tenant_id) as ts:
        items = list(
            ts.execute(
                select(WorkflowQueue).where(
                    (WorkflowQueue.queueType.in_(["QA_QUEUE", "VERIFIER_QUEUE"]))
                    & (
                        or_(
                            WorkflowQueue.dueBy < now,
                            (WorkflowQueue.dueBy.is_(None))
                            & (WorkflowQueue.createdAt < stale_threshold),
                        )
                    )
                )
            ).scalars()
        )
        if not items:
            return 0

        webhook_events: list[dict[str, Any]] = []

        for item in items:
            ev = ts.get(Evaluation, item.evaluationId)
            if not ev or ev.isEscalated:
                continue
            original_queue = item.queueType
            human_queue = original_queue.replace("_", " ").lower()
            item.queueType = "ESCALATION_QUEUE"
            item.priority = 1
            ev.isEscalated = True
            ev.escalationReason = (
                f"Stale in {human_queue} for {stale_hours}+ hours"
            )
            ts.add(
                AuditLog(
                    evaluationId=item.evaluationId,
                    entityType="evaluation",
                    entityId=item.evaluationId,
                    action="stale_escalation",
                    actorId="system",
                    actorRole="SYSTEM",
                    lmetadata={
                        "originalQueue": original_queue,
                        "staleHours": stale_hours,
                        "createdAt": item.createdAt.isoformat() if item.createdAt else None,
                    },
                )
            )
            webhook_events.append(
                {"evaluationId": item.evaluationId, "conversationId": ev.conversationId}
            )
            escalated_count += 1
        ts.commit()

    for ev_data in webhook_events:
        try:
            deliver_webhook(
                master,
                tenant_id=tenant_id,
                event="evaluation.escalated",
                data={
                    **ev_data,
                    "workflowState": "ESCALATION_QUEUE",
                    "finalScore": None,
                    "passFail": None,
                },
            )
        except Exception:  # noqa: BLE001
            log.warning("escalate: webhook delivery failed", exc_info=True)
    return escalated_count


@celery_app.task(name="eval.escalate.scan", acks_late=True)
def stale_queue_escalate_scan() -> dict[str, int]:
    total = 0
    tenants_scanned = 0
    with SessionLocal() as master:
        tenants = list(
            master.execute(select(Tenant.id).where(Tenant.status == "ACTIVE")).scalars()
        )
        for tenant_id in tenants:
            tenants_scanned += 1
            try:
                total += _escalate_for_tenant(master, tenant_id)
            except Exception as err:  # noqa: BLE001
                log.warning(
                    "Stale escalation failed for tenant %s: %s", tenant_id, err
                )
    return {"tenants": tenants_scanned, "escalated": total}
