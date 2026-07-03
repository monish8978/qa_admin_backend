"""Evaluations workflow service.

Ports apps/api/src/evaluations/evaluations.service.ts (1909 LOC). Handles:
  - queue listings (qa, verifier, escalation, audit)
  - single-evaluation read with blind-review anonymization
  - QA start/submit → VERIFIER start/approve/modify/reject lifecycle
  - retry / re-audit flows (AI requeue via Celery)
  - manual assign / round-robin / reassign (admin)
  - audit-case resolve, prompt-audit JSONL read, CSV export
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import String, cast, func, or_, select, text
from sqlalchemy.orm import Session

from ..common.enums import DeviationType, UserRole, WorkflowState
from ..common.exceptions import bad_request, conflict, forbidden, not_found
from ..config import get_settings
from ..models.master import (
    BlindReviewSettings,
    EscalationRule,
    User,
)
from ..models.tenant import (
    AuditCase,
    AuditLog,
    Conversation,
    DeviationRecord,
    Evaluation,
    FormDefinition,
    WorkflowQueue,
)
from . import scoring_service
from .department_routing import select_least_loaded_user
from .outbound_webhooks_service import deliver as deliver_webhook
from .tenant_pool import get_tenant_pool

log = logging.getLogger("qa.evaluations")

_LIST_CONVERSATION_FIELDS = (
    "id",
    "channel",
    "agentName",
    "customerRef",
    "receivedAt",
    "externalId",
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _deterministic_alias(tenant_id: str, kind: str, source: str) -> str:
    settings = get_settings()
    salt = settings.MASTER_ENCRYPTION_KEY or settings.JWT_SECRET or "qa-platform"
    digest = hashlib.sha256(f"{salt}:{tenant_id}:{kind}:{source}".encode()).hexdigest()[:12]
    return f"agent_{digest}" if kind == "agent" else f"qa_{digest}"


def _derive_pass_fail(
    score: float | None, pass_mark: float, fallback: bool | None
) -> bool | None:
    if isinstance(score, (int, float)):
        return score >= pass_mark
    return fallback


def _has_critical(layer: Any) -> bool:
    return isinstance(layer, dict) and layer.get("criticalFailure") is True


def _has_ai_answer_value(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    value = entry.get("value")
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _normalize_layer(layer: Any, score: float | None, pass_mark: float) -> None:
    if not isinstance(layer, dict):
        return
    critical = layer.get("criticalFailure") is True
    layer_score = layer.get("overallScore") if isinstance(layer.get("overallScore"), (int, float)) else None
    fallback = layer.get("passFail") if isinstance(layer.get("passFail"), bool) else None
    if critical:
        layer["passFail"] = False
    else:
        layer["passFail"] = _derive_pass_fail(score if score is not None else layer_score, pass_mark, fallback)


def _conversation_dict(c: Conversation | None) -> dict[str, Any] | None:
    if not c:
        return None
    return {f: getattr(c, f, None) for f in _LIST_CONVERSATION_FIELDS}


def _queue_dict(q: WorkflowQueue | None) -> dict[str, Any] | None:
    if not q:
        return None
    return {
        "id": q.id,
        "queueType": q.queueType,
        "departmentId": q.departmentId,
        "priority": q.priority,
        "assignedTo": q.assignedTo,
        "dueBy": q.dueBy,
        "createdAt": q.createdAt,
        "updatedAt": q.updatedAt,
    }


def _eval_for_queue(row: Evaluation, conv: Conversation | None) -> dict[str, Any]:
    return {
        "id": row.id,
        "workflowState": row.workflowState,
        "aiScore": row.aiScore,
        "qaScore": row.qaScore,
        "verifierRejectReason": row.verifierRejectReason,
        "verifierRejectedAt": row.verifierRejectedAt,
        "formDefinitionId": row.formDefinitionId,
        "formVersion": row.formVersion,
        "conversation": _conversation_dict(conv),
    }


def _ensure_eval(ts: Session, evaluation_id: str, *, lock: bool = False) -> Evaluation:
    if lock:
        # Row-level lock to serialise concurrent claim/submit/approve operations
        # on the same evaluation (prevents two reviewers clobbering ownership).
        ev = ts.execute(
            select(Evaluation).where(Evaluation.id == evaluation_id).with_for_update()
        ).scalar_one_or_none()
    else:
        ev = ts.get(Evaluation, evaluation_id)
    if not ev:
        raise not_found("EVALUATION_NOT_FOUND", "Evaluation not found")
    return ev


_QA_UNASSIGNED_READ_STATES = frozenset(
    {WorkflowState.QA_PENDING.value, WorkflowState.QA_IN_PROGRESS.value}
)
_VERIFIER_UNASSIGNED_READ_STATES = frozenset(
    {
        WorkflowState.QA_COMPLETED.value,
        WorkflowState.VERIFIER_PENDING.value,
        WorkflowState.VERIFIER_IN_PROGRESS.value,
    }
)


def _assert_evaluation_read_access(
    ev: Evaluation,
    actor_role: str | None,
    actor_id: str | None,
) -> None:
    """Enforce queue-scoped read access for non-admin roles."""
    role = actor_role or ""
    if role == UserRole.ADMIN.value:
        return
    if not actor_id:
        raise forbidden("FORBIDDEN", "Access denied")
    if role == UserRole.QA.value:
        if ev.qaUserId == actor_id:
            return
        if ev.qaUserId is None and ev.workflowState in _QA_UNASSIGNED_READ_STATES:
            return
        raise forbidden("FORBIDDEN", "You do not have access to this evaluation")
    if role == UserRole.VERIFIER.value:
        if ev.verifierUserId == actor_id:
            return
        if ev.verifierUserId is None and ev.workflowState in _VERIFIER_UNASSIGNED_READ_STATES:
            return
        raise forbidden("FORBIDDEN", "You do not have access to this evaluation")
    raise forbidden("FORBIDDEN", "Access denied")


def _get_queue_for(ts: Session, evaluation_id: str) -> WorkflowQueue | None:
    return ts.execute(
        select(WorkflowQueue).where(WorkflowQueue.evaluationId == evaluation_id)
    ).scalar_one_or_none()


def _upsert_queue(
    ts: Session,
    *,
    evaluation_id: str,
    queue_type: str,
    department_id: str | None = None,
    assigned_to: str | None = None,
    priority: int = 5,
) -> None:
    existing = _get_queue_for(ts, evaluation_id)
    if existing:
        existing.queueType = queue_type
        if department_id is not None:
            existing.departmentId = department_id
        existing.assignedTo = assigned_to
        existing.priority = priority
    else:
        ts.add(
            WorkflowQueue(
                evaluationId=evaluation_id,
                queueType=queue_type,
                departmentId=department_id,
                assignedTo=assigned_to,
                priority=priority,
            )
        )


def _add_audit(
    ts: Session,
    *,
    evaluation_id: str | None,
    entity_type: str,
    entity_id: str,
    action: str,
    actor_id: str,
    actor_role: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    ts.add(
        AuditLog(
            evaluationId=evaluation_id,
            entityType=entity_type,
            entityId=entity_id,
            action=action,
            actorId=actor_id,
            actorRole=actor_role,
            lmetadata=metadata,
        )
    )


def _ev_to_dict(ev: Evaluation) -> dict[str, Any]:
    """Serialize an Evaluation row (for response payloads)."""
    cols = [c.key for c in Evaluation.__table__.columns]
    out: dict[str, Any] = {}
    for c in cols:
        attr = c
        # JSONB columns mapped to python-name attributes
        if c == "metadata":
            attr = "lmetadata"
        out[c] = getattr(ev, attr, None)
    out["id"] = ev.id
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Lists / queues
# ──────────────────────────────────────────────────────────────────────────────


def list_evaluations(tenant_id: str, query: dict[str, Any], role: str | None = None, user_id: str | None = None) -> dict[str, Any]:
    page = max(int(query.get("page") or 1), 1)
    limit = max(min(int(query.get("limit") or 20), 100), 1)
    skip = (page - 1) * limit
    workflow_state = query.get("workflowState")

    pool = get_tenant_pool()
    with pool.session(tenant_id) as ts:
        q = select(Evaluation)
        if workflow_state:
            q = q.where(Evaluation.workflowState == workflow_state)
        if role == "QA" and user_id:
            q = q.where(Evaluation.qaUserId == user_id)
        elif role == "VERIFIER" and user_id:
            q = q.where(Evaluation.verifierUserId == user_id)

        total = ts.execute(
            select(func.count()).select_from(q.subquery())
        ).scalar_one()
        rows = list(
            ts.execute(
                q.order_by(Evaluation.createdAt.desc()).offset(skip).limit(limit)
            ).scalars()
        )
        items: list[dict[str, Any]] = []
        for ev in rows:
            conv = ts.get(Conversation, ev.conversationId)
            queue = _get_queue_for(ts, ev.id)
            d = _ev_to_dict(ev)
            d["conversation"] = _conversation_dict(conv)
            d["workflowQueue"] = (
                {"priority": queue.priority, "dueBy": queue.dueBy, "queueType": queue.queueType}
                if queue
                else None
            )
            items.append(d)

    return {
        "items": items,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": int(total),
            "totalPages": math.ceil(int(total) / limit) if limit else 0,
        },
    }


def _conversation_search_clause(search: str):
    s = f"%{search.strip()}%"
    return or_(
        cast(Conversation.externalId, String).ilike(s),
        cast(Conversation.channel, String).ilike(s),
        cast(Conversation.agentName, String).ilike(s),
        cast(Conversation.customerRef, String).ilike(s),
    )


def _queue_listing(
    tenant_id: str,
    *,
    workflow_states: list[str],
    page: int,
    limit: int,
    search: str | None,
    department_id: str | None,
    queue_type: str,
    order_field: str,
    role: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    skip = (page - 1) * limit
    pool = get_tenant_pool()
    with pool.session(tenant_id) as ts:
        q = select(Evaluation).where(Evaluation.workflowState.in_(workflow_states))
        if role == "QA" and user_id:
            q = q.where(or_(Evaluation.qaUserId == user_id, Evaluation.qaUserId.is_(None)))
        elif role == "VERIFIER" and user_id:
            q = q.where(or_(Evaluation.verifierUserId == user_id, Evaluation.verifierUserId.is_(None)))

        if department_id:
            q = q.where(Evaluation.departmentId == department_id)
        if search and search.strip():
            q = q.join(Conversation, Evaluation.conversationId == Conversation.id).where(
                _conversation_search_clause(search)
            )
        total = ts.execute(select(func.count()).select_from(q.subquery())).scalar_one()
        order_col = getattr(Evaluation, order_field)
        rows = list(
            ts.execute(
                q.order_by(order_col.desc().nullslast(), Evaluation.createdAt.desc())
                .offset(skip)
                .limit(limit)
            ).scalars()
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            conv = ts.get(Conversation, row.conversationId)
            queue = _get_queue_for(ts, row.id)
            items.append(
                {
                    "id": queue.id if queue else f"{queue_type.lower()}-{row.id}",
                    "evaluationId": row.id,
                    "queueType": queue.queueType if queue else queue_type,
                    "priority": queue.priority if queue else 5,
                    "departmentId": (queue.departmentId if queue else None) or row.departmentId,
                    "assignedTo": (queue.assignedTo if queue else None)
                    or (row.qaUserId if queue_type == "QA_QUEUE" else row.verifierUserId),
                    "dueBy": queue.dueBy if queue else None,
                    "createdAt": queue.createdAt if queue else row.createdAt,
                    "updatedAt": queue.updatedAt if queue else row.updatedAt,
                    "evaluation": {
                        "id": row.id,
                        "workflowState": row.workflowState,
                        "aiScore": row.aiScore,
                        "qaScore": row.qaScore,
                        **(
                            {
                                "verifierRejectReason": row.verifierRejectReason,
                                "verifierRejectedAt": row.verifierRejectedAt,
                            }
                            if queue_type == "QA_QUEUE"
                            else {}
                        ),
                        "formDefinitionId": row.formDefinitionId,
                        "formVersion": row.formVersion,
                        "conversation": _conversation_dict(conv),
                    },
                }
            )
    return {
        "items": items,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": int(total),
            "totalPages": math.ceil(int(total) / limit) if limit else 0,
        },
    }


def get_qa_queue(
    tenant_id: str,
    page: int = 1,
    limit: int = 20,
    search: str | None = None,
    department_id: str | None = None,
    role: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    return _queue_listing(
        tenant_id,
        workflow_states=[WorkflowState.QA_PENDING.value, WorkflowState.QA_IN_PROGRESS.value],
        page=page,
        limit=limit,
        search=search,
        department_id=department_id,
        queue_type="QA_QUEUE",
        order_field="qaStartedAt",
        role=role,
        user_id=user_id,
    )


def get_verifier_queue(
    tenant_id: str,
    page: int = 1,
    limit: int = 20,
    search: str | None = None,
    department_id: str | None = None,
    role: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    return _queue_listing(
        tenant_id,
        workflow_states=[
            WorkflowState.QA_COMPLETED.value,
            WorkflowState.VERIFIER_PENDING.value,
            WorkflowState.VERIFIER_IN_PROGRESS.value,
        ],
        page=page,
        limit=limit,
        search=search,
        department_id=department_id,
        queue_type="VERIFIER_QUEUE",
        order_field="verifierStartedAt",
        role=role,
        user_id=user_id,
    )



def _direct_queue_listing(
    tenant_id: str,
    *,
    queue_type: str,
    page: int,
    limit: int,
    search: str | None,
    include_audit_case: bool = False,
    role: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    skip = (page - 1) * limit
    pool = get_tenant_pool()
    with pool.session(tenant_id) as ts:
        q = select(WorkflowQueue).where(WorkflowQueue.queueType == queue_type)
        joined_evaluation = False
        if role == "QA" and user_id:
            q = q.join(Evaluation, WorkflowQueue.evaluationId == Evaluation.id).where(Evaluation.qaUserId == user_id)
            joined_evaluation = True
        elif role == "VERIFIER" and user_id:
            q = q.join(Evaluation, WorkflowQueue.evaluationId == Evaluation.id).where(Evaluation.verifierUserId == user_id)
            joined_evaluation = True

        if search and search.strip():
            if not joined_evaluation:
                q = q.join(Evaluation, WorkflowQueue.evaluationId == Evaluation.id)
            q = q.join(Conversation, Evaluation.conversationId == Conversation.id).where(_conversation_search_clause(search))

        total = ts.execute(select(func.count()).select_from(q.subquery())).scalar_one()
        rows = list(
            ts.execute(
                q.order_by(WorkflowQueue.priority.asc(), WorkflowQueue.createdAt.asc())
                .offset(skip)
                .limit(limit)
            ).scalars()
        )
        items: list[dict[str, Any]] = []
        for queue in rows:
            ev = ts.get(Evaluation, queue.evaluationId)
            if not ev:
                continue
            conv = ts.get(Conversation, ev.conversationId)
            evaluation_dict: dict[str, Any]
            if include_audit_case:
                evaluation_dict = _ev_to_dict(ev)
                evaluation_dict["conversation"] = _conversation_dict(conv)
                evaluation_dict["auditCase"] = _audit_case_dict(
                    ts.execute(
                        select(AuditCase).where(AuditCase.evaluationId == ev.id)
                    ).scalar_one_or_none()
                )
            else:
                evaluation_dict = {
                    "id": ev.id,
                    "workflowState": ev.workflowState,
                    "aiScore": ev.aiScore,
                    "qaScore": ev.qaScore,
                    "isEscalated": ev.isEscalated,
                    "escalationReason": ev.escalationReason,
                    "formDefinitionId": ev.formDefinitionId,
                    "formVersion": ev.formVersion,
                    "conversation": _conversation_dict(conv),
                }
            items.append({**_queue_dict(queue), "evaluation": evaluation_dict})
    return {
        "items": items,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": int(total),
            "totalPages": math.ceil(int(total) / limit) if limit else 0,
        },
    }


def _audit_case_dict(ac: AuditCase | None) -> dict[str, Any] | None:
    if not ac:
        return None
    return {
        "id": ac.id,
        "evaluationId": ac.evaluationId,
        "status": ac.status,
        "deviation": ac.deviation,
        "threshold": ac.threshold,
        "reason": ac.reason,
        "resolutionNote": ac.resolutionNote,
        "resolvedBy": ac.resolvedBy,
        "resolvedAt": ac.resolvedAt,
        "createdAt": ac.createdAt,
        "updatedAt": ac.updatedAt,
    }


def get_escalation_queue(
    tenant_id: str,
    page: int = 1,
    limit: int = 20,
    search: str | None = None,
    role: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    return _direct_queue_listing(
        tenant_id,
        queue_type="ESCALATION_QUEUE",
        page=page,
        limit=limit,
        search=search,
        role=role,
        user_id=user_id,
    )


def get_audit_queue(
    tenant_id: str,
    page: int = 1,
    limit: int = 20,
    search: str | None = None,
    role: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    return _direct_queue_listing(
        tenant_id,
        queue_type="AUDIT_QUEUE",
        page=page,
        limit=limit,
        search=search,
        include_audit_case=True,
        role=role,
        user_id=user_id,
    )



# ──────────────────────────────────────────────────────────────────────────────
# Single evaluation read
# ──────────────────────────────────────────────────────────────────────────────


def _normalize_evaluation_pass_fail(payload: dict[str, Any]) -> None:
    form_def = payload.get("formDefinition") if isinstance(payload.get("formDefinition"), dict) else None
    strategy = (form_def or {}).get("scoringStrategy") if form_def else None
    pass_mark = (
        strategy.get("passMark")
        if isinstance(strategy, dict) and isinstance(strategy.get("passMark"), (int, float))
        else 70
    )
    ai_score = payload.get("aiScore") if isinstance(payload.get("aiScore"), (int, float)) else None
    qa_score = payload.get("qaScore") if isinstance(payload.get("qaScore"), (int, float)) else None
    verifier_score = payload.get("verifierScore") if isinstance(payload.get("verifierScore"), (int, float)) else None
    final_score = payload.get("finalScore") if isinstance(payload.get("finalScore"), (int, float)) else None
    pass_fail = payload.get("passFail") if isinstance(payload.get("passFail"), bool) else None

    _normalize_layer(payload.get("aiResponseData"), ai_score, pass_mark)
    _normalize_layer(payload.get("qaAdjustedData"), qa_score, pass_mark)
    _normalize_layer(
        payload.get("verifierFinalData"),
        verifier_score if verifier_score is not None else final_score,
        pass_mark,
    )
    _normalize_layer(payload.get("finalResponseData"), final_score, pass_mark)

    critical = (
        _has_critical(payload.get("finalResponseData"))
        or _has_critical(payload.get("verifierFinalData"))
        or _has_critical(payload.get("qaAdjustedData"))
        or _has_critical(payload.get("aiResponseData"))
    )
    payload["passFail"] = False if critical else _derive_pass_fail(final_score, pass_mark, pass_fail)


def _form_def_dict(fd: FormDefinition | None) -> dict[str, Any] | None:
    if not fd:
        return None
    return {
        "id": fd.id,
        "formKey": fd.formKey,
        "version": fd.version,
        "name": fd.name,
        "status": fd.status,
        "channels": fd.channels,
        "scoringStrategy": fd.scoringStrategy,
        "sections": fd.sections,
        "questions": fd.questions,
        "departmentId": fd.departmentId,
    }


def get_evaluation(
    master: Session,
    tenant_id: str,
    evaluation_id: str,
    actor_role: str,
    actor_id: str | None = None,
) -> dict[str, Any]:
    pool = get_tenant_pool()
    with pool.session(tenant_id) as ts:
        ev = _ensure_eval(ts, evaluation_id)
        _assert_evaluation_read_access(ev, actor_role, actor_id)
        conv = ts.get(Conversation, ev.conversationId)
        form_def = ts.get(FormDefinition, ev.formDefinitionId)
        deviations = list(
            ts.execute(
                select(DeviationRecord).where(DeviationRecord.evaluationId == ev.id)
            ).scalars()
        )
        audit_logs = list(
            ts.execute(
                select(AuditLog)
                .where(AuditLog.evaluationId == ev.id)
                .order_by(AuditLog.createdAt.desc())
                .limit(50)
            ).scalars()
        )
        queue = _get_queue_for(ts, ev.id)

        payload = _ev_to_dict(ev)
        conv_dict: dict[str, Any] | None = None
        if conv:
            conv_dict = {
                **{c.key: getattr(conv, "cmetadata" if c.key == "metadata" else c.key) for c in Conversation.__table__.columns},
                "id": conv.id,
            }
        payload["conversation"] = conv_dict
        payload["formDefinition"] = _form_def_dict(form_def)
        payload["deviationRecords"] = [
            {
                "id": d.id,
                "type": d.type,
                "scoreA": d.scoreA,
                "scoreB": d.scoreB,
                "deviation": d.deviation,
                "questionKey": d.questionKey,
                "sectionId": d.sectionId,
                "createdAt": d.createdAt,
            }
            for d in deviations
        ]
        payload["auditLogs"] = [
            {
                "id": a.id,
                "action": a.action,
                "actorId": a.actorId,
                "actorRole": a.actorRole,
                "entityType": a.entityType,
                "entityId": a.entityId,
                "metadata": a.lmetadata,
                "createdAt": a.createdAt,
            }
            for a in audit_logs
        ]
        payload["workflowQueue"] = _queue_dict(queue)

    blind = master.execute(
        select(BlindReviewSettings).where(BlindReviewSettings.tenantId == tenant_id)
    ).scalar_one_or_none()
    if blind and conv_dict:
        if getattr(blind, "hideAgentFromQA", False) and actor_role == "QA":
            source = conv_dict.get("agentId") or conv_dict.get("agentName") or conv_dict.get("id")
            alias = _deterministic_alias(tenant_id, "agent", str(source))
            conv_dict["agentId"] = alias
            conv_dict["agentName"] = alias
        if getattr(blind, "hideQAFromVerifier", False) and actor_role == "VERIFIER":
            qa_source = payload.get("qaUserId") or payload.get("id")
            payload["qaUserId"] = _deterministic_alias(tenant_id, "qa", str(qa_source))

    _normalize_evaluation_pass_fail(payload)
    return payload


def get_audit_log(
    tenant_id: str,
    evaluation_id: str,
    actor_role: str,
    actor_id: str | None = None,
) -> list[dict[str, Any]]:
    pool = get_tenant_pool()
    with pool.session(tenant_id) as ts:
        ev = _ensure_eval(ts, evaluation_id)
        _assert_evaluation_read_access(ev, actor_role, actor_id)
        rows = list(
            ts.execute(
                select(AuditLog)
                .where(AuditLog.evaluationId == evaluation_id)
                .order_by(AuditLog.createdAt.desc())
            ).scalars()
        )
        return [
            {
                "id": a.id,
                "action": a.action,
                "actorId": a.actorId,
                "actorRole": a.actorRole,
                "entityType": a.entityType,
                "entityId": a.entityId,
                "metadata": a.lmetadata,
                "before": a.before,
                "after": a.after,
                "createdAt": a.createdAt,
                "evaluationId": a.evaluationId,
            }
            for a in rows
        ]


# ──────────────────────────────────────────────────────────────────────────────
# QA workflow
# ──────────────────────────────────────────────────────────────────────────────


def qa_start(tenant_id: str, evaluation_id: str, user_id: str, actor_role: str) -> dict[str, Any]:
    pool = get_tenant_pool()
    with pool.session(tenant_id) as ts:
        ev = _ensure_eval(ts, evaluation_id, lock=True)
        if ev.workflowState not in (
            WorkflowState.QA_PENDING.value,
            WorkflowState.QA_IN_PROGRESS.value,
        ):
            raise conflict(
                "INVALID_STATE", f"Cannot claim evaluation in {ev.workflowState} state"
            )
        # If already in progress and claimed by someone else, do not let a second
        # reviewer steal/overwrite the claim.
        if (
            ev.workflowState == WorkflowState.QA_IN_PROGRESS.value
            and ev.qaUserId
            and ev.qaUserId != user_id
        ):
            raise forbidden(
                "NOT_CLAIMED_BY_YOU", "This evaluation is already claimed by another reviewer"
            )
        previous_qa_user_id = ev.qaUserId
        from_state = ev.workflowState
        now = datetime.now(timezone.utc)
        ev.workflowState = WorkflowState.QA_IN_PROGRESS.value
        ev.qaUserId = user_id
        ev.qaStartedAt = now
        _upsert_queue(
            ts, evaluation_id=evaluation_id, queue_type="QA_QUEUE", assigned_to=user_id, priority=5
        )
        _add_audit(
            ts,
            evaluation_id=evaluation_id,
            entity_type="evaluation",
            entity_id=evaluation_id,
            action="qa_start",
            actor_id=user_id,
            actor_role=actor_role,
            metadata={
                "workflowState": {"from": from_state, "to": "QA_IN_PROGRESS"},
                "previousQaUserId": previous_qa_user_id,
            },
        )
        ts.commit()
        return {"workflowState": WorkflowState.QA_IN_PROGRESS.value}


def qa_submit(
    master: Session,
    tenant_id: str,
    evaluation_id: str,
    user_id: str,
    actor_role: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    pool = get_tenant_pool()
    deviations_out: list[dict[str, Any]] = []

    with pool.session(tenant_id) as ts:
        ev = _ensure_eval(ts, evaluation_id)
        if ev.workflowState != WorkflowState.QA_IN_PROGRESS.value:
            raise conflict("ALREADY_SUBMITTED", "Evaluation is not in QA_IN_PROGRESS state")
        if ev.qaUserId != user_id:
            raise forbidden("NOT_CLAIMED_BY_YOU", "This evaluation was not claimed by you")
        form = ts.get(FormDefinition, ev.formDefinitionId)
        if not form:
            raise not_found("FORM_NOT_FOUND", "Form definition missing")

        ai_layer = ev.aiResponseData or {}
        ai_answers: dict[str, Any] = ai_layer.get("answers") or {} if isinstance(ai_layer, dict) else {}
        # When QA re-opens an evaluation after verifier rejection, preserve the
        # previously submitted QA answers as baseline. Falling back to AI answers
        # would silently drop earlier QA adjustments when only a subset is edited.
        qa_layer = ev.qaAdjustedData or {}
        qa_answers: dict[str, Any] = qa_layer.get("answers") or {} if isinstance(qa_layer, dict) else {}
        baseline_answers: dict[str, Any] = qa_answers or ai_answers
        adjusted_answers: dict[str, Any] = {**baseline_answers}
        raw_adjustments: dict[str, dict[str, Any]] = body.get("adjustedAnswers") or {}
        for key, adj in raw_adjustments.items():
            reason = (adj.get("overrideReason") or "").strip() or None
            ai_entry = ai_answers.get(key) if isinstance(ai_answers, dict) else None
            baseline_entry = baseline_answers.get(key) if isinstance(baseline_answers, dict) else None
            ai_value = ai_entry.get("value") if isinstance(ai_entry, dict) else None
            baseline_value = baseline_entry.get("value") if isinstance(baseline_entry, dict) else None
            new_value = adj.get("value")
            baseline_reason = (
                (baseline_entry.get("overrideReason") or "").strip()
                if isinstance(baseline_entry, dict)
                else None
            )
            effective_reason = reason or baseline_reason
            if (
                _has_ai_answer_value(ai_entry)
                and str(new_value) != str(ai_value)
                and not effective_reason
            ):
                raise bad_request(
                    "MISSING_OVERRIDE_REASON",
                    f'Question "{key}" value changed without overrideReason',
                )
            if (
                _has_ai_answer_value(ai_entry)
                and str(new_value) == str(ai_value)
            ):
                adjusted_answers[key] = {"value": adj.get("value"), "overrideReason": None}
            elif (
                baseline_reason
                and str(new_value) == str(baseline_value)
            ):
                adjusted_answers[key] = {"value": adj.get("value"), "overrideReason": baseline_reason}
            else:
                adjusted_answers[key] = {"value": adj.get("value"), "overrideReason": effective_reason}

        result = scoring_service.score(
            adjusted_answers,
            list(form.questions or []),
            list(form.sections or []),
            dict(form.scoringStrategy or {}),
        )

        now = datetime.now(timezone.utc)

        overridden_keys = [
            k
            for k, adj in raw_adjustments.items()
            if _has_ai_answer_value(ai_answers.get(k))
            and str(adj.get("value")) != str((ai_answers.get(k) or {}).get("value"))
        ]
        section_map: dict[str, str] = {q["key"]: q["sectionId"] for q in (form.questions or [])}

        deviation_drafts: list[dict[str, Any]] = []
        ai_qa_deviation = 0.0
        if ev.aiScore is not None:
            ai_qa_deviation = abs(result["overallScore"] - ev.aiScore)
            deviation_drafts.append(
                {
                    "type": DeviationType.AI_VS_QA.value,
                    "evaluationId": evaluation_id,
                    "scoreA": ev.aiScore,
                    "scoreB": result["overallScore"],
                    "deviation": ai_qa_deviation,
                }
            )
        for key in overridden_keys:
            ai_value = (ai_answers.get(key) or {}).get("value") or 0
            qa_value = (adjusted_answers.get(key) or {}).get("value") or 0
            deviation_drafts.append(
                {
                    "type": DeviationType.AI_VS_QA.value,
                    "evaluationId": evaluation_id,
                    "questionKey": key,
                    "sectionId": section_map.get(key),
                    "scoreA": float(ai_value) if isinstance(ai_value, (int, float)) else 0,
                    "scoreB": float(qa_value) if isinstance(qa_value, (int, float)) else 0,
                    "deviation": 1.0,
                }
            )

        rule = master.execute(
            select(EscalationRule).where(EscalationRule.tenantId == tenant_id)
        ).scalar_one_or_none()
        escalation_threshold = rule.qaDeviationThreshold if rule else 15
        should_escalate = ai_qa_deviation > escalation_threshold

        vmin_s = rule.verifierMinRangeStart if rule else 0
        vmin_e = rule.verifierMinRangeEnd if rule else 40
        vmax_s = rule.verifierMaxRangeStart if rule else 90
        vmax_e = rule.verifierMaxRangeEnd if rule else 100

        def _in_verifier_range(score_val: float | None) -> bool:
            if not isinstance(score_val, (int, float)):
                return False
            return (vmin_s <= score_val <= vmin_e) or (vmax_s <= score_val <= vmax_e)

        if ev.aiScore is None:
            should_send_to_verifier = True
        else:
            should_send_to_verifier = _in_verifier_range(result["overallScore"]) or _in_verifier_range(ev.aiScore)
            
        should_auto_complete = not should_escalate and not should_send_to_verifier

        verifier_assignee: dict[str, Any] | None = None
        if not should_escalate and should_send_to_verifier and ev.departmentId:
            verifier_assignee = select_least_loaded_user(
                master,
                ts,
                tenant_id=tenant_id,
                department_id=ev.departmentId,
                queue_type="VERIFIER_QUEUE",
            )

        qa_layer = {
            "answers": result["answers"],
            "sectionScores": result["sectionScores"],
            "overallScore": result["overallScore"],
            "passFail": result["passFail"],
        }

        target_state = (
            WorkflowState.LOCKED.value
            if should_auto_complete
            else (
                WorkflowState.VERIFIER_IN_PROGRESS.value
                if verifier_assignee
                else WorkflowState.QA_COMPLETED.value
            )
        )

        ev.workflowState = target_state
        ev.qaAdjustedData = qa_layer
        ev.qaScore = result["overallScore"]
        if should_auto_complete:
            ev.verifierFinalData = qa_layer
            ev.finalResponseData = qa_layer
            ev.finalScore = result["overallScore"]
            ev.passFail = result["passFail"]
            ev.lockedAt = now
        ev.qaCompletedAt = now
        if verifier_assignee:
            ev.verifierUserId = verifier_assignee["id"]
            ev.verifierStartedAt = now
        ev.feedback = body.get("feedback")
        flags = body.get("flags") or []
        ev.flags = list(flags) if flags else None
        if should_escalate:
            ev.isEscalated = True
            ev.escalationReason = (
                f"AI↔QA deviation {ai_qa_deviation:.1f}% exceeds threshold {escalation_threshold}%"
            )

        for d in deviation_drafts:
            ts.add(DeviationRecord(**d))

        if should_auto_complete:
            ts.execute(
                text('DELETE FROM workflow_queues WHERE "evaluationId" = :eid'),
                {"eid": evaluation_id},
            )
            conv = ts.get(Conversation, ev.conversationId)
            if conv:
                conv.status = "COMPLETED"
        else:
            _upsert_queue(
                ts,
                evaluation_id=evaluation_id,
                queue_type="ESCALATION_QUEUE" if should_escalate else "VERIFIER_QUEUE",
                department_id=ev.departmentId,
                priority=1 if should_escalate else 5,
                assigned_to=verifier_assignee["id"] if verifier_assignee else None,
            )
            # This submission addresses any prior verifier rejection feedback, so
            # clear it — the next verifier round should start from a clean slate
            # rather than seeing the previous cycle's per-question comments.
            ev.verifierFinalData = None
            # Keep the conversation status in sync with the workflow: once QA
            # submits and the evaluation moves to the verifier/escalation track,
            # the conversation is in verifier review (not QA review anymore).
            conv = ts.get(Conversation, ev.conversationId)
            if conv:
                conv.status = "VERIFIER_REVIEW"

        _add_audit(
            ts,
            evaluation_id=evaluation_id,
            entity_type="evaluation",
            entity_id=evaluation_id,
            action="qa_submit",
            actor_id=user_id,
            actor_role=actor_role,
            metadata={
                "workflowState": {"from": "QA_IN_PROGRESS", "to": target_state},
                "qaScore": result["overallScore"],
                "aiQaDeviation": ai_qa_deviation,
                "escalated": should_escalate,
                "routedToVerifier": not should_escalate and should_send_to_verifier,
                "verifierAssignedTo": verifier_assignee["id"] if verifier_assignee else None,
                "autoCompletedAfterQa": should_auto_complete,
            },
        )
        deviations_out = list(deviation_drafts)
        conv_id_for_webhook = ev.conversationId
        score_out = result["overallScore"]
        pass_fail_out = result["passFail"]
        ts.commit()

    # Fire-and-forget webhooks (use master DB for hook config)
    try:
        if should_escalate:
            deliver_webhook(
                master,
                tenant_id=tenant_id,
                event="evaluation.escalated",
                data={
                    "evaluationId": evaluation_id,
                    "conversationId": conv_id_for_webhook,
                    "workflowState": "ESCALATION_QUEUE",
                    "finalScore": None,
                    "passFail": None,
                },
            )
        elif should_auto_complete:
            deliver_webhook(
                master,
                tenant_id=tenant_id,
                event="evaluation.completed",
                data={
                    "evaluationId": evaluation_id,
                    "conversationId": conv_id_for_webhook,
                    "workflowState": WorkflowState.LOCKED.value,
                    "finalScore": score_out,
                    "passFail": pass_fail_out,
                },
            )
    except Exception:  # noqa: BLE001
        log.warning("qa_submit: webhook delivery failed", exc_info=True)

    return {
        "workflowState": target_state,
        "qaScore": score_out,
        "passFail": pass_fail_out,
        "deviations": deviations_out,
        "escalated": should_escalate,
        "routedToVerifier": not should_escalate and should_send_to_verifier,
        "autoCompletedAfterQa": should_auto_complete,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Verifier workflow
# ──────────────────────────────────────────────────────────────────────────────


def verifier_start(
    tenant_id: str, evaluation_id: str, user_id: str, actor_role: str
) -> dict[str, Any]:
    pool = get_tenant_pool()
    with pool.session(tenant_id) as ts:
        # Lock the row so two verifiers can't both claim from QA_COMPLETED and
        # clobber each other's ownership (last-writer-wins race).
        ev = _ensure_eval(ts, evaluation_id, lock=True)
        if ev.workflowState not in (
            WorkflowState.VERIFIER_PENDING.value,
            WorkflowState.QA_COMPLETED.value,
        ):
            raise conflict(
                "INVALID_STATE", f"Cannot claim evaluation in {ev.workflowState} state"
            )
        from_state = ev.workflowState
        now = datetime.now(timezone.utc)
        ev.workflowState = WorkflowState.VERIFIER_IN_PROGRESS.value
        ev.verifierUserId = user_id
        ev.verifierStartedAt = now
        _upsert_queue(
            ts,
            evaluation_id=evaluation_id,
            queue_type="VERIFIER_QUEUE",
            assigned_to=user_id,
            priority=5,
        )
        _add_audit(
            ts,
            evaluation_id=evaluation_id,
            entity_type="evaluation",
            entity_id=evaluation_id,
            action="verifier_start",
            actor_id=user_id,
            actor_role=actor_role,
            metadata={"workflowState": {"from": from_state, "to": "VERIFIER_IN_PROGRESS"}},
        )
        ts.commit()
        return {"workflowState": WorkflowState.VERIFIER_IN_PROGRESS.value}


def verifier_approve(
    master: Session,
    tenant_id: str,
    evaluation_id: str,
    user_id: str,
    actor_role: str,
) -> dict[str, Any]:
    pool = get_tenant_pool()
    with pool.session(tenant_id) as ts:
        ev = _ensure_eval(ts, evaluation_id)
        if ev.workflowState != WorkflowState.VERIFIER_IN_PROGRESS.value:
            raise conflict(
                "INVALID_STATE", "Evaluation is not in VERIFIER_IN_PROGRESS state"
            )
        if ev.verifierUserId != user_id:
            raise forbidden("NOT_CLAIMED_BY_YOU", "Not claimed by you")

        form = ts.get(FormDefinition, ev.formDefinitionId)
        strategy = (form.scoringStrategy if form else None) or {}
        pass_mark = strategy.get("passMark", 70) if isinstance(strategy, dict) else 70

        qa_layer = ev.qaAdjustedData if isinstance(ev.qaAdjustedData, dict) else None
        critical = bool(qa_layer and qa_layer.get("criticalFailure") is True)
        final_score = ev.qaScore if ev.qaScore is not None else (qa_layer.get("overallScore") if qa_layer else None)
        pass_fail = (final_score is not None) and (not critical) and final_score >= pass_mark

        normalized_layer = None
        if qa_layer:
            normalized_layer = {
                **qa_layer,
                "overallScore": final_score if final_score is not None else qa_layer.get("overallScore"),
                "passFail": pass_fail,
                "criticalFailure": critical,
            }

        now = datetime.now(timezone.utc)
        ev.workflowState = WorkflowState.LOCKED.value
        ev.verifierFinalData = normalized_layer
        ev.finalResponseData = normalized_layer
        ev.verifierScore = final_score
        ev.finalScore = final_score
        ev.passFail = pass_fail
        ev.verifierCompletedAt = now
        ev.lockedAt = now

        ts.execute(
            text('DELETE FROM workflow_queues WHERE "evaluationId" = :eid'),
            {"eid": evaluation_id},
        )
        conv = ts.get(Conversation, ev.conversationId)
        if conv:
            conv.status = "COMPLETED"
        _add_audit(
            ts,
            evaluation_id=evaluation_id,
            entity_type="evaluation",
            entity_id=evaluation_id,
            action="verifier_approve",
            actor_id=user_id,
            actor_role=actor_role,
            metadata={
                "workflowState": {"from": "VERIFIER_IN_PROGRESS", "to": "LOCKED"},
                "finalScore": final_score,
            },
        )
        conv_id_for_webhook = ev.conversationId
        ts.commit()

    try:
        deliver_webhook(
            master,
            tenant_id=tenant_id,
            event="evaluation.completed",
            data={
                "evaluationId": evaluation_id,
                "conversationId": conv_id_for_webhook,
                "workflowState": WorkflowState.LOCKED.value,
                "finalScore": final_score,
                "passFail": pass_fail,
            },
        )
    except Exception:  # noqa: BLE001
        log.warning("verifier_approve: webhook delivery failed", exc_info=True)

    return {
        "workflowState": WorkflowState.LOCKED.value,
        "finalScore": final_score,
        "passFail": pass_fail,
    }


def verifier_modify(
    master: Session,
    tenant_id: str,
    evaluation_id: str,
    user_id: str,
    actor_role: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    pool = get_tenant_pool()
    with pool.session(tenant_id) as ts:
        ev = _ensure_eval(ts, evaluation_id)
        if ev.workflowState != WorkflowState.VERIFIER_IN_PROGRESS.value:
            raise conflict("INVALID_STATE", "Evaluation is not in VERIFIER_IN_PROGRESS")
        if ev.verifierUserId != user_id:
            raise forbidden("NOT_CLAIMED_BY_YOU", "Not claimed by you")
        form = ts.get(FormDefinition, ev.formDefinitionId)
        if not form:
            raise not_found("FORM_NOT_FOUND", "Form definition missing")

        qa_layer = ev.qaAdjustedData if isinstance(ev.qaAdjustedData, dict) else None
        merged: dict[str, Any] = {**((qa_layer or {}).get("answers") or {})}
        modified_answers: dict[str, dict[str, Any]] = body.get("modifiedAnswers") or {}
        for key, mod in modified_answers.items():
            if not (mod.get("overrideReason") or "").strip():
                raise bad_request(
                    "MISSING_OVERRIDE_REASON", f'overrideReason required for "{key}"'
                )
            merged[key] = {"value": mod["value"], "overrideReason": mod["overrideReason"]}

        result = scoring_service.score(
            merged,
            list(form.questions or []),
            list(form.sections or []),
            dict(form.scoringStrategy or {}),
        )

        now = datetime.now(timezone.utc)
        verifier_layer = {
            "answers": merged,
            "sectionScores": result["sectionScores"],
            "overallScore": result["overallScore"],
            "passFail": result["passFail"],
        }

        rule = master.execute(
            select(EscalationRule).where(EscalationRule.tenantId == tenant_id)
        ).scalar_one_or_none()
        threshold_raw = (
            rule.verifierDeviationThreshold if rule else None
        )
        threshold = (
            float(threshold_raw)
            if isinstance(threshold_raw, (int, float))
            else 10.0
        )

        deviation_drafts: list[dict[str, Any]] = []
        qa_verifier_deviation = 0.0
        if ev.qaScore is not None:
            qa_verifier_deviation = abs(result["overallScore"] - ev.qaScore)
            deviation_drafts.append(
                {
                    "type": DeviationType.QA_VS_VERIFIER.value,
                    "evaluationId": evaluation_id,
                    "scoreA": ev.qaScore,
                    "scoreB": result["overallScore"],
                    "deviation": qa_verifier_deviation,
                }
            )
        section_map: dict[str, str] = {q["key"]: q["sectionId"] for q in (form.questions or [])}
        for key, mod in modified_answers.items():
            qa_val = ((qa_layer or {}).get("answers", {}).get(key) or {}).get("value")
            v_val = mod.get("value")
            if qa_val is None or str(v_val) == str(qa_val):
                continue
            deviation_drafts.append(
                {
                    "type": DeviationType.QA_VS_VERIFIER.value,
                    "evaluationId": evaluation_id,
                    "questionKey": key,
                    "sectionId": section_map.get(key),
                    "scoreA": float(qa_val) if isinstance(qa_val, (int, float)) else 0,
                    "scoreB": float(v_val) if isinstance(v_val, (int, float)) else 0,
                    "deviation": 1.0,
                }
            )

        should_create_audit = qa_verifier_deviation >= threshold

        ev.workflowState = WorkflowState.LOCKED.value
        ev.verifierFinalData = verifier_layer
        ev.finalResponseData = verifier_layer
        ev.verifierScore = result["overallScore"]
        ev.finalScore = result["overallScore"]
        ev.passFail = result["passFail"]
        ev.verifierCompletedAt = now
        ev.lockedAt = now
        if body.get("feedback") is not None:
            ev.feedback = body["feedback"]

        for d in deviation_drafts:
            ts.add(DeviationRecord(**d))

        if should_create_audit:
            existing_ac = ts.execute(
                select(AuditCase).where(AuditCase.evaluationId == evaluation_id)
            ).scalar_one_or_none()
            reason = (
                f"Verifier deviation {qa_verifier_deviation:.2f} exceeds threshold {threshold:.2f}"
            )
            if existing_ac:
                existing_ac.deviation = qa_verifier_deviation
                existing_ac.threshold = float(threshold)
                existing_ac.reason = reason
                existing_ac.status = "OPEN"
                existing_ac.resolvedAt = None
                existing_ac.resolvedBy = None
                existing_ac.resolutionNote = None
            else:
                ts.add(
                    AuditCase(
                        evaluationId=evaluation_id,
                        deviation=qa_verifier_deviation,
                        threshold=float(threshold),
                        reason=reason,
                        status="OPEN",
                    )
                )
            _upsert_queue(
                ts,
                evaluation_id=evaluation_id,
                queue_type="AUDIT_QUEUE",
                priority=1,
                assigned_to=None,
            )
        else:
            ts.execute(
                text('DELETE FROM workflow_queues WHERE "evaluationId" = :eid'),
                {"eid": evaluation_id},
            )

        conv = ts.get(Conversation, ev.conversationId)
        if conv:
            conv.status = "COMPLETED"

        _add_audit(
            ts,
            evaluation_id=evaluation_id,
            entity_type="evaluation",
            entity_id=evaluation_id,
            action="verifier_modify",
            actor_id=user_id,
            actor_role=actor_role,
            metadata={
                "finalScore": result["overallScore"],
                "qaVerifierDeviation": qa_verifier_deviation,
                "auditCaseCreated": should_create_audit,
            },
        )
        conv_id_for_webhook = ev.conversationId
        ts.commit()

    try:
        deliver_webhook(
            master,
            tenant_id=tenant_id,
            event="evaluation.completed",
            data={
                "evaluationId": evaluation_id,
                "conversationId": conv_id_for_webhook,
                "workflowState": WorkflowState.LOCKED.value,
                "finalScore": result["overallScore"],
                "passFail": result["passFail"],
            },
        )
    except Exception:  # noqa: BLE001
        log.warning("verifier_modify: webhook delivery failed", exc_info=True)

    return {
        "workflowState": WorkflowState.LOCKED.value,
        "finalScore": result["overallScore"],
        "passFail": result["passFail"],
    }


def verifier_reject(
    tenant_id: str,
    evaluation_id: str,
    user_id: str,
    actor_role: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    pool = get_tenant_pool()
    with pool.session(tenant_id) as ts:
        ev = _ensure_eval(ts, evaluation_id, lock=True)
        if ev.workflowState != WorkflowState.VERIFIER_IN_PROGRESS.value:
            raise conflict("INVALID_STATE", "Evaluation is not in VERIFIER_IN_PROGRESS")
        # Only the verifier who claimed it may reject (parity with approve/modify).
        if ev.verifierUserId != user_id:
            raise forbidden("NOT_CLAIMED_BY_YOU", "Not claimed by you")
        reason = body.get("reason")
        now = datetime.now(timezone.utc)
        ev.workflowState = WorkflowState.QA_PENDING.value
        ev.verifierRejectedAt = now
        ev.verifierRejectReason = reason
        ev.verifierUserId = None
        ev.verifierStartedAt = None

        # Persist any per-question changes/comments the verifier made so QA can
        # see them, question-wise, during re-review. We build a verifier layer on
        # top of the QA answers and store it in verifierFinalData (the field the
        # review UI already reads for "Verifier override reason / answer").
        modified: dict[str, Any] = body.get("modifiedAnswers") or {}
        if modified:
            qa_layer = ev.qaAdjustedData if isinstance(ev.qaAdjustedData, dict) else {}
            base_answers = qa_layer.get("answers") if isinstance(qa_layer, dict) else {}
            merged_answers: dict[str, Any] = {
                **(base_answers if isinstance(base_answers, dict) else {})
            }
            for key, mod in modified.items():
                comment = (mod.get("overrideReason") or "").strip()
                if not comment:
                    raise bad_request(
                        "MISSING_OVERRIDE_REASON",
                        f'A comment (overrideReason) is required for "{key}"',
                    )
                merged_answers[key] = {
                    "value": mod.get("value"),
                    "overrideReason": comment,
                }
            ev.verifierFinalData = {
                **(qa_layer if isinstance(qa_layer, dict) else {}),
                "answers": merged_answers,
                "isRejectionFeedback": True,
            }

        ts.execute(
            text(
                'UPDATE workflow_queues SET "queueType" = :qt, "assignedTo" = NULL '
                'WHERE "evaluationId" = :eid'
            ),
            {"qt": "QA_QUEUE", "eid": evaluation_id},
        )
        # Bounce the conversation status back to QA review so it no longer shows
        # as "VERIFIER_REVIEW" after being sent back down the workflow.
        conv = ts.get(Conversation, ev.conversationId)
        if conv:
            conv.status = "QA_REVIEW"
        _add_audit(
            ts,
            evaluation_id=evaluation_id,
            entity_type="evaluation",
            entity_id=evaluation_id,
            action="verifier_reject",
            actor_id=user_id,
            actor_role=actor_role,
            metadata={
                "reason": reason,
                "workflowState": {"from": "VERIFIER_IN_PROGRESS", "to": "QA_PENDING"},
            },
        )
        ts.commit()
    return {"workflowState": WorkflowState.QA_PENDING.value}


# ──────────────────────────────────────────────────────────────────────────────
# Preview, audit log export, prompt audit
# ──────────────────────────────────────────────────────────────────────────────


def preview_score(tenant_id: str, form_id: str, raw_answers: dict[str, Any]) -> dict[str, Any]:
    pool = get_tenant_pool()
    with pool.session(tenant_id) as ts:
        form = ts.get(FormDefinition, form_id)
        if not form:
            raise not_found("FORM_NOT_FOUND", "Form not found")
        answers = {k: {"value": v} for k, v in raw_answers.items()}
        return scoring_service.score(
            answers,
            list(form.questions or []),
            list(form.sections or []),
            dict(form.scoringStrategy or {}),
        )


def export_audit_log_csv(
    tenant_id: str,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    evaluation_id: str | None = None,
) -> str:
    pool = get_tenant_pool()
    with pool.session(tenant_id) as ts:
        q = select(AuditLog)
        if evaluation_id:
            q = q.where(AuditLog.evaluationId == evaluation_id)
        if from_date:
            q = q.where(AuditLog.createdAt >= from_date)
        if to_date:
            q = q.where(AuditLog.createdAt <= to_date)
        q = q.order_by(AuditLog.createdAt.desc()).limit(10_000)
        rows = list(ts.execute(q).scalars())

    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL)
    writer.writerow(
        [
            "id",
            "createdAt",
            "evaluationId",
            "entityType",
            "entityId",
            "action",
            "actorId",
            "actorRole",
            "metadata",
            "before",
            "after",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r.id,
                r.createdAt.isoformat() if r.createdAt else "",
                r.evaluationId or "",
                r.entityType,
                r.entityId,
                r.action,
                r.actorId,
                r.actorRole,
                json.dumps(r.lmetadata) if r.lmetadata is not None else "",
                json.dumps(r.before) if r.before is not None else "",
                json.dumps(r.after) if r.after is not None else "",
            ]
        )
    return buf.getvalue()


def _prompt_log_path() -> Path:
    configured = (os.environ.get("LLM_PROMPT_LOG_PATH") or "").strip()
    if configured:
        return Path(configured).resolve()
    return (Path.cwd() / "apps" / "api" / "logs" / "llm-prompt-audit.jsonl").resolve()


def get_prompt_audit_log(tenant_id: str, query: dict[str, Any]) -> list[dict[str, Any]]:
    limit = max(1, min(int(query.get("limit") or 20), 200))
    eval_filter = query.get("evaluationId")
    path = _prompt_log_path()
    if not path.exists():
        return []
    results: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        all_lines = [ln.strip() for ln in fh if ln.strip()]
    for raw in reversed(all_lines):
        if len(results) >= limit:
            break
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        if parsed.get("tenantId") != tenant_id:
            continue
        if eval_filter and parsed.get("evaluationId") != eval_filter:
            continue
        required = (
            "timestamp",
            "tenantId",
            "evaluationId",
            "conversationId",
            "formDefinitionId",
            "provider",
            "model",
            "promptHash",
            "contentHash",
            "prompt",
        )
        if any(not isinstance(parsed.get(k), str) for k in required):
            continue
        results.append(
            {
                "timestamp": parsed["timestamp"],
                "tenantId": parsed["tenantId"],
                "evaluationId": parsed["evaluationId"],
                "conversationId": parsed["conversationId"],
                "formDefinitionId": parsed["formDefinitionId"],
                "provider": parsed["provider"],
                "model": parsed["model"],
                "promptHash": parsed["promptHash"],
                "contentHash": parsed["contentHash"],
                "prompt": parsed["prompt"],
                "responseContent": parsed.get("responseContent")
                if isinstance(parsed.get("responseContent"), str)
                else None,
                "answersHash": parsed.get("answersHash")
                if isinstance(parsed.get("answersHash"), str)
                else None,
                "aiScore": parsed.get("aiScore")
                if isinstance(parsed.get("aiScore"), (int, float))
                else None,
            }
        )
    return results


# ──────────────────────────────────────────────────────────────────────────────
# AI retry / re-audit
# ──────────────────────────────────────────────────────────────────────────────


def _enqueue_eval_process(
    *,
    tenant_id: str,
    conversation_id: str,
    evaluation_id: str,
    form_definition_id: str,
    form_version: int,
) -> None:
    settings = get_settings()
    if not settings.redis_enabled:
        raise bad_request(
            "QUEUE_UNAVAILABLE", "Redis queue is unavailable. Cannot requeue AI processing."
        )
    from ..celery_app import celery_app

    celery_app.send_task(
        "eval.process",
        kwargs={
            "tenantId": tenant_id,
            "conversationId": conversation_id,
            "evaluationId": evaluation_id,
            "formDefinitionId": form_definition_id,
            "formVersion": form_version,
        },
        retry=True,
        retry_policy={"max_retries": 3, "interval_start": 5, "interval_step": 5},
    )


def retry_ai_failed(
    tenant_id: str, evaluation_id: str, actor_id: str, actor_role: str
) -> dict[str, Any]:
    pool = get_tenant_pool()
    with pool.session(tenant_id) as ts:
        ev = _ensure_eval(ts, evaluation_id)
        if ev.workflowState != WorkflowState.AI_FAILED.value:
            raise conflict(
                "INVALID_STATE",
                f"Only AI_FAILED evaluations can be retried (current: {ev.workflowState})",
            )
        ev.workflowState = WorkflowState.AI_PENDING.value
        ev.escalationReason = None
        ev.isEscalated = False
        conv = ts.get(Conversation, ev.conversationId)
        if conv:
            conv.status = "PENDING"
        ts.execute(
            text('DELETE FROM workflow_queues WHERE "evaluationId" = :eid'),
            {"eid": evaluation_id},
        )
        _add_audit(
            ts,
            evaluation_id=evaluation_id,
            entity_type="evaluation",
            entity_id=evaluation_id,
            action="ai_retry_requested",
            actor_id=actor_id,
            actor_role=actor_role,
            metadata={"workflowState": {"from": "AI_FAILED", "to": "AI_PENDING"}},
        )
        cid = ev.conversationId
        fdid = ev.formDefinitionId
        fver = ev.formVersion
        ts.commit()

    _enqueue_eval_process(
        tenant_id=tenant_id,
        conversation_id=cid,
        evaluation_id=evaluation_id,
        form_definition_id=fdid,
        form_version=fver,
    )
    return {"workflowState": WorkflowState.AI_PENDING.value, "queued": True}


def re_audit_ai(
    tenant_id: str,
    evaluation_id: str,
    actor_id: str,
    actor_role: str,
    reason: str | None = None,
) -> dict[str, Any]:
    pool = get_tenant_pool()
    with pool.session(tenant_id) as ts:
        ev = _ensure_eval(ts, evaluation_id)
        if ev.workflowState == WorkflowState.AI_IN_PROGRESS.value:
            raise conflict(
                "INVALID_STATE",
                "Cannot re-audit while AI processing is currently in progress.",
            )
        from_state = ev.workflowState
        ev.workflowState = WorkflowState.AI_PENDING.value
        for attr in (
            "aiResponseData",
            "qaAdjustedData",
            "verifierFinalData",
            "finalResponseData",
            "aiMetadata",
            "flags",
        ):
            setattr(ev, attr, None)
        for attr in (
            "aiScore",
            "qaScore",
            "verifierScore",
            "finalScore",
            "passFail",
            "aiCompletedAt",
            "qaUserId",
            "qaStartedAt",
            "qaCompletedAt",
            "verifierUserId",
            "verifierStartedAt",
            "verifierCompletedAt",
            "verifierRejectedAt",
            "verifierRejectReason",
            "lockedAt",
            "confidenceScore",
            "escalationReason",
            "feedback",
        ):
            setattr(ev, attr, None)
        ev.isEscalated = False

        conv = ts.get(Conversation, ev.conversationId)
        if conv:
            conv.status = "PENDING"
        ts.execute(
            text('DELETE FROM workflow_queues WHERE "evaluationId" = :eid'),
            {"eid": evaluation_id},
        )
        ts.execute(
            text('DELETE FROM deviation_records WHERE "evaluationId" = :eid'),
            {"eid": evaluation_id},
        )
        ts.execute(
            text('DELETE FROM audit_cases WHERE "evaluationId" = :eid'),
            {"eid": evaluation_id},
        )
        _add_audit(
            ts,
            evaluation_id=evaluation_id,
            entity_type="evaluation",
            entity_id=evaluation_id,
            action="ai_reaudit_requested",
            actor_id=actor_id,
            actor_role=actor_role,
            metadata={
                "workflowState": {"from": from_state, "to": WorkflowState.AI_PENDING.value},
                "reason": (reason or "").strip() or None,
            },
        )
        cid = ev.conversationId
        fdid = ev.formDefinitionId
        fver = ev.formVersion
        ts.commit()

    _enqueue_eval_process(
        tenant_id=tenant_id,
        conversation_id=cid,
        evaluation_id=evaluation_id,
        form_definition_id=fdid,
        form_version=fver,
    )
    return {"workflowState": WorkflowState.AI_PENDING.value, "queued": True}


def bulk_re_audit_ai(
    tenant_id: str,
    evaluation_ids: list[str],
    actor_id: str,
    actor_role: str,
    reason: str | None = None,
) -> dict[str, Any]:
    unique = list({eid.strip() for eid in evaluation_ids if eid.strip()})
    queued: list[str] = []
    failed: list[dict[str, str]] = []
    for eid in unique:
        try:
            re_audit_ai(tenant_id, eid, actor_id, actor_role, reason)
            queued.append(eid)
        except Exception as err:  # noqa: BLE001
            # Don't leak internal exception text to API clients; log server-side.
            log.warning("bulk re-audit failed eid=%s", eid, exc_info=True)
            code = getattr(err, "code", None) or type(err).__name__
            failed.append({"evaluationId": eid, "code": code})
    return {
        "requested": len(unique),
        "queued": len(queued),
        "failed": len(failed),
        "queuedIds": queued,
        "failures": failed,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Audit case resolution
# ──────────────────────────────────────────────────────────────────────────────


def resolve_audit_case(
    tenant_id: str,
    audit_case_id: str,
    actor_id: str,
    actor_role: str,
    dismiss: bool = False,
    note: str | None = None,
) -> dict[str, Any]:
    pool = get_tenant_pool()
    with pool.session(tenant_id) as ts:
        ac = ts.get(AuditCase, audit_case_id)
        if not ac:
            raise not_found("AUDIT_CASE_NOT_FOUND", "Audit case not found")
        if ac.status != "OPEN":
            raise conflict("AUDIT_CASE_ALREADY_CLOSED", "Audit case is already closed")
        status = "DISMISSED" if dismiss else "RESOLVED"
        now = datetime.now(timezone.utc)
        ac.status = status
        ac.resolvedAt = now
        ac.resolvedBy = actor_id
        ac.resolutionNote = note
        ts.execute(
            text(
                'DELETE FROM workflow_queues WHERE "evaluationId" = :eid AND "queueType" = :qt'
            ),
            {"eid": ac.evaluationId, "qt": "AUDIT_QUEUE"},
        )
        _add_audit(
            ts,
            evaluation_id=ac.evaluationId,
            entity_type="audit_case",
            entity_id=audit_case_id,
            action="audit_case_dismissed" if dismiss else "audit_case_resolved",
            actor_id=actor_id,
            actor_role=actor_role,
            metadata={"note": note, "status": status},
        )
        ts.commit()
        return {"id": audit_case_id, "status": status, "resolvedAt": now.isoformat()}


# ──────────────────────────────────────────────────────────────────────────────
# Manual / round-robin / reassign
# ──────────────────────────────────────────────────────────────────────────────


_QA_ASSIGNABLE = (WorkflowState.QA_PENDING.value, WorkflowState.QA_IN_PROGRESS.value)
_VERIFIER_ASSIGNABLE = (
    WorkflowState.QA_COMPLETED.value,
    WorkflowState.VERIFIER_PENDING.value,
    WorkflowState.VERIFIER_IN_PROGRESS.value,
)


def manual_assign(
    master: Session,
    tenant_id: str,
    evaluation_id: str,
    target_user_id: str,
    actor_id: str,
    actor_role: str,
) -> dict[str, Any]:
    target = master.execute(
        select(User).where(
            (User.id == target_user_id)
            & (User.tenantId == tenant_id)
            & (User.status == "ACTIVE")
        )
    ).scalar_one_or_none()
    if not target:
        raise bad_request(
            "INVALID_TARGET_USER",
            "Target user is not found, inactive, or does not belong to this tenant",
        )

    pool = get_tenant_pool()
    with pool.session(tenant_id) as ts:
        ev = _ensure_eval(ts, evaluation_id)
        is_qa_assignment = target.role in ("QA", "ADMIN")
        is_verifier_assignment = target.role in ("VERIFIER", "ADMIN")
        can_qa = is_qa_assignment and ev.workflowState in _QA_ASSIGNABLE
        can_verifier = is_verifier_assignment and ev.workflowState in _VERIFIER_ASSIGNABLE
        if not can_qa and not can_verifier:
            raise conflict(
                "INVALID_ASSIGNMENT",
                f"Cannot assign user with role {target.role} to evaluation in {ev.workflowState} state",
            )
        now = datetime.now(timezone.utc)
        queue = _get_queue_for(ts, evaluation_id)
        previous_assignee = queue.assignedTo if queue else None

        if can_qa:
            ev.workflowState = WorkflowState.QA_IN_PROGRESS.value
            ev.qaUserId = target_user_id
            ev.qaStartedAt = ev.qaStartedAt or now
            _upsert_queue(
                ts,
                evaluation_id=evaluation_id,
                queue_type="QA_QUEUE",
                assigned_to=target_user_id,
                priority=5,
            )
            assignment_type = "qa"
        else:
            ev.workflowState = WorkflowState.VERIFIER_IN_PROGRESS.value
            ev.verifierUserId = target_user_id
            ev.verifierStartedAt = ev.verifierStartedAt or now
            _upsert_queue(
                ts,
                evaluation_id=evaluation_id,
                queue_type="VERIFIER_QUEUE",
                assigned_to=target_user_id,
                priority=5,
            )
            assignment_type = "verifier"

        _add_audit(
            ts,
            evaluation_id=evaluation_id,
            entity_type="evaluation",
            entity_id=evaluation_id,
            action="manual_assign",
            actor_id=actor_id,
            actor_role=actor_role,
            metadata={
                "assignedTo": target_user_id,
                "assignedToName": target.name,
                "previousAssignee": previous_assignee,
                "assignmentType": assignment_type,
            },
        )
        ts.commit()

    return {
        "evaluationId": evaluation_id,
        "assignedTo": target_user_id,
        "assignedToName": target.name,
        "assignmentType": assignment_type,
    }


def round_robin_assign(
    master: Session,
    tenant_id: str,
    queue_type: str,
    actor_id: str,
    actor_role: str,
    department_id: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    if queue_type not in ("QA_QUEUE", "VERIFIER_QUEUE"):
        raise bad_request(
            "INVALID_QUEUE_TYPE", "Queue type must be one of: QA_QUEUE, VERIFIER_QUEUE"
        )
    target_role = "QA" if queue_type == "QA_QUEUE" else "VERIFIER"
    eligible_q = select(User).where(
        (User.tenantId == tenant_id)
        & (User.status == "ACTIVE")
        & (User.role.in_([target_role, "ADMIN"]))
    )
    if department_id:
        eligible_q = eligible_q.where(User.departmentId == department_id)
    eligible_q = eligible_q.order_by(User.createdAt.asc())
    eligible_users = list(master.execute(eligible_q).scalars())
    if not eligible_users:
        raise bad_request(
            "NO_ELIGIBLE_USERS", f"No active {target_role} users available for assignment"
        )

    pool = get_tenant_pool()
    with pool.session(tenant_id) as ts:
        q = select(WorkflowQueue).where(
            (WorkflowQueue.queueType == queue_type) & (WorkflowQueue.assignedTo.is_(None))
        )
        if department_id:
            q = q.where(WorkflowQueue.departmentId == department_id)
        unassigned = list(
            ts.execute(
                q.order_by(WorkflowQueue.priority.asc(), WorkflowQueue.createdAt.asc())
                .limit(limit or 1000)
            ).scalars()
        )
        if not unassigned:
            return {"assigned": 0, "message": "No unassigned items in queue"}

        counts_q = (
            select(WorkflowQueue.assignedTo, func.count())
            .where(
                (WorkflowQueue.queueType == queue_type)
                & (WorkflowQueue.assignedTo.is_not(None))
            )
            .group_by(WorkflowQueue.assignedTo)
        )
        if department_id:
            counts_q = counts_q.where(WorkflowQueue.departmentId == department_id)
        load_map: dict[str, int] = {u.id: 0 for u in eligible_users}
        for assignee, cnt in ts.execute(counts_q).all():
            if assignee in load_map:
                load_map[assignee] = int(cnt)
        sorted_users = sorted(eligible_users, key=lambda u: load_map.get(u.id, 0))

        is_qa = queue_type == "QA_QUEUE"
        state = (
            WorkflowState.QA_IN_PROGRESS.value
            if is_qa
            else WorkflowState.VERIFIER_IN_PROGRESS.value
        )
        now = datetime.now(timezone.utc)
        assignments: list[dict[str, Any]] = []
        for i, item in enumerate(unassigned):
            user = sorted_users[i % len(sorted_users)]
            assignments.append({"evaluationId": item.evaluationId, "userId": user.id, "userName": user.name})

        for a in assignments:
            queue_row = ts.execute(
                select(WorkflowQueue).where(WorkflowQueue.evaluationId == a["evaluationId"])
            ).scalar_one_or_none()
            if queue_row:
                queue_row.assignedTo = a["userId"]
            ev = ts.get(Evaluation, a["evaluationId"])
            if ev:
                ev.workflowState = state
                if is_qa:
                    ev.qaUserId = a["userId"]
                    ev.qaStartedAt = now
                else:
                    ev.verifierUserId = a["userId"]
                    ev.verifierStartedAt = now

        distribution = [
            {
                "userId": u.id,
                "name": u.name,
                "count": sum(1 for a in assignments if a["userId"] == u.id),
            }
            for u in sorted_users
        ]
        _add_audit(
            ts,
            evaluation_id=None,
            entity_type="workflow_queue",
            entity_id=queue_type,
            action="round_robin_assign",
            actor_id=actor_id,
            actor_role=actor_role,
            metadata={
                "queueType": queue_type,
                "departmentId": department_id,
                "totalAssigned": len(assignments),
                "userDistribution": distribution,
            },
        )
        ts.commit()

    return {"assigned": len(assignments), "distribution": distribution}


def reassign(
    master: Session,
    tenant_id: str,
    evaluation_id: str,
    new_user_id: str,
    actor_id: str,
    actor_role: str,
    reason: str | None = None,
) -> dict[str, Any]:
    new_user = master.execute(
        select(User).where(
            (User.id == new_user_id)
            & (User.tenantId == tenant_id)
            & (User.status == "ACTIVE")
        )
    ).scalar_one_or_none()
    if not new_user:
        raise bad_request(
            "INVALID_TARGET_USER",
            "Target user is not found, inactive, or does not belong to this tenant",
        )

    pool = get_tenant_pool()
    with pool.session(tenant_id) as ts:
        ev = _ensure_eval(ts, evaluation_id)
        is_qa_progress = ev.workflowState == WorkflowState.QA_IN_PROGRESS.value
        is_verifier_progress = ev.workflowState == WorkflowState.VERIFIER_IN_PROGRESS.value
        if not is_qa_progress and not is_verifier_progress:
            raise conflict(
                "NOT_IN_PROGRESS",
                f"Cannot reassign evaluation in {ev.workflowState} state. "
                "Only QA_IN_PROGRESS or VERIFIER_IN_PROGRESS can be reassigned.",
            )
        previous_user_id = ev.qaUserId if is_qa_progress else ev.verifierUserId
        now = datetime.now(timezone.utc)
        if is_qa_progress:
            ev.qaUserId = new_user_id
            ev.qaStartedAt = now
            assignment_type = "qa"
        else:
            ev.verifierUserId = new_user_id
            ev.verifierStartedAt = now
            assignment_type = "verifier"
        ts.execute(
            text(
                'UPDATE workflow_queues SET "assignedTo" = :uid WHERE "evaluationId" = :eid'
            ),
            {"uid": new_user_id, "eid": evaluation_id},
        )
        _add_audit(
            ts,
            evaluation_id=evaluation_id,
            entity_type="evaluation",
            entity_id=evaluation_id,
            action="reassign",
            actor_id=actor_id,
            actor_role=actor_role,
            metadata={
                "previousUserId": previous_user_id,
                "newUserId": new_user_id,
                "newUserName": new_user.name,
                "reason": reason or "Admin reassignment",
                "assignmentType": assignment_type,
            },
        )
        ts.commit()

    return {
        "evaluationId": evaluation_id,
        "previousUserId": previous_user_id,
        "newUserId": new_user_id,
        "newUserName": new_user.name,
        "assignmentType": assignment_type,
    }

