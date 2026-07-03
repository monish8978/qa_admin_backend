"""Conversations service — mirrors apps/api/src/conversations/conversations.service.ts."""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..common.enums import PLAN_LIMITS, PlanType, UserRole
from ..common.exceptions import bad_request, forbidden, not_found
from ..models.master import LlmConfig, Tenant
from ..models.tenant import Conversation, Evaluation, FormDefinition, WorkflowQueue
from .department_routing import (
    get_active_departments,
    resolve_conversation_department,
    select_least_loaded_user,
)
from . import routing_service
from .evaluations_service import _assert_evaluation_read_access
from ..config import get_settings
from .tenant_pool import get_tenant_pool
from . import usage_meter_service

log = logging.getLogger("qa.conversations")

CHANNEL_SEARCH_VALUES = {"CHAT", "EMAIL", "CALL", "SOCIAL"}


def _parse_search_channel(value: str) -> str | None:
    norm = value.strip().upper()
    return norm if norm in CHANNEL_SEARCH_VALUES else None


def _derive_pass_fail(
    score: float | None, pass_mark: float | None, fallback: bool | None
) -> bool | None:
    if fallback is False:
        return False
    if score is not None and pass_mark is not None:
        return score >= pass_mark
    return fallback


_has_form_department_column_cache: dict[str, bool] = {}


def _has_form_department_column(ts: Session, tenant_id: str) -> bool:
    cached = _has_form_department_column_cache.get(tenant_id)
    if cached is not None:
        return cached
    row = ts.execute(
        text(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'form_definitions'
                  AND column_name = 'departmentId'
            ) AS exists
            """
        )
    ).scalar_one()
    _has_form_department_column_cache[tenant_id] = bool(row)
    return bool(row)


def _resolve_published_form(
    ts: Session,
    tenant_id: str,
    *,
    channel: str,
    department_id: str | None,
    cache: dict[str, dict[str, Any] | None],
) -> dict[str, Any] | None:
    norm_channel = channel.strip().upper()
    key = f"{norm_channel}:{department_id or '__none__'}"
    if key in cache:
        return cache[key]

    has_col = _has_form_department_column(ts, tenant_id)
    channel_json = f'["{norm_channel}"]'

    form_row: dict[str, Any] | None = None
    if has_col and department_id:
        form_row = ts.execute(
            text(
                'SELECT id, version FROM form_definitions '
                "WHERE status = 'PUBLISHED' "
                '  AND "departmentId" = :did '
                "  AND CAST(channels AS JSONB) @> CAST(:ch AS JSONB) "
                "ORDER BY version DESC LIMIT 1"
            ),
            {"did": department_id, "ch": channel_json},
        ).mappings().first()
        if not form_row:
            form_row = ts.execute(
                text(
                    'SELECT id, version FROM form_definitions '
                    "WHERE status = 'PUBLISHED' "
                    '  AND "departmentId" IS NULL '
                    "  AND CAST(channels AS JSONB) @> CAST(:ch AS JSONB) "
                    "ORDER BY version DESC LIMIT 1"
                ),
                {"ch": channel_json},
            ).mappings().first()
    elif has_col:
        form_row = ts.execute(
            text(
                'SELECT id, version FROM form_definitions '
                "WHERE status = 'PUBLISHED' "
                '  AND "departmentId" IS NULL '
                "  AND CAST(channels AS JSONB) @> CAST(:ch AS JSONB) "
                "ORDER BY version DESC LIMIT 1"
            ),
            {"ch": channel_json},
        ).mappings().first()
        if not form_row:
            form_row = ts.execute(
                text(
                    'SELECT id, version FROM form_definitions '
                    "WHERE status = 'PUBLISHED' "
                    "  AND CAST(channels AS JSONB) @> CAST(:ch AS JSONB) "
                    "ORDER BY version DESC LIMIT 1"
                ),
                {"ch": channel_json},
            ).mappings().first()
    else:
        form_row = ts.execute(
            text(
                'SELECT id, version FROM form_definitions '
                "WHERE status = 'PUBLISHED' "
                "  AND CAST(channels AS JSONB) @> CAST(:ch AS JSONB) "
                "ORDER BY version DESC LIMIT 1"
            ),
            {"ch": channel_json},
        ).mappings().first()

    result = dict(form_row) if form_row else None
    cache[key] = result
    return result


def _should_use_llm(
    *,
    tenant_id: str,
    channel: str,
    department_id: str | None,
    conversation_key: str,
    ai_pct: int,
) -> bool:
    pct = max(0, min(100, round(ai_pct)))
    if pct <= 0:
        return False
    if pct >= 100:
        return True
    src = f"{tenant_id}:{department_id or 'none'}:{channel}:{conversation_key}"
    digest = hashlib.sha256(src.encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:4], "big") % 100
    return bucket < pct


def _enqueue_eval(
    *,
    tenant_id: str,
    conversation_id: str,
    evaluation_id: str,
    form_definition_id: str,
    form_version: int,
) -> bool:
    try:
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
        return True
    except Exception as e:  # noqa: BLE001
        log.warning(
            "Failed to enqueue eval.process tenantId=%s conv=%s err=%s",
            tenant_id,
            conversation_id,
            e,
        )
        return False


# ─── list / get ──────────────────────────────────────────────────────────────

def list_conversations(
    master: Session, tenant_id: str, query: dict[str, Any], role: str | None = None, user_id: str | None = None
) -> dict[str, Any]:
    page = max(int(query.get("page") or 1), 1)
    limit = max(min(int(query.get("limit") or 20), 100), 1)
    skip = (page - 1) * limit
    status = query.get("status")
    agent_id = query.get("agentId")
    search = (query.get("search") or "").strip()

    pool = get_tenant_pool()
    with pool.session(tenant_id) as ts:
        clauses = ["1=1"]
        params: dict[str, Any] = {"skip": skip, "limit": limit}
        if status:
            clauses.append('c."status" = :status')
            params["status"] = status
        if agent_id:
            clauses.append('c."agentId" = :agent_id')
            params["agent_id"] = agent_id
        if role == "QA" and user_id:
            clauses.append('e."qaUserId" = :uid')
            params["uid"] = user_id
        elif role == "VERIFIER" and user_id:
            clauses.append('e."verifierUserId" = :uid')
            params["uid"] = user_id

        if search:
            search_channel = _parse_search_channel(search)
            or_clauses = [
                'c."externalId" ILIKE :search',
                'c."agentName" ILIKE :search',
                'c."customerRef" ILIKE :search',
            ]
            if search_channel:
                or_clauses.append('c.channel = :search_channel')
                params["search_channel"] = search_channel
            clauses.append("(" + " OR ".join(or_clauses) + ")")
            params["search"] = f"%{search}%"

        where_sql = " AND ".join(clauses)

        rows = ts.execute(
            text(
                f"""
                SELECT c.id, c."externalId", c.channel, c."agentName", c."customerRef",
                       c.status, c."receivedAt",
                       e."workflowState", e."aiScore", e."qaScore",
                       e."verifierScore", e."finalScore", e."passFail",
                       fd."scoringStrategy"
                FROM conversations c
                LEFT JOIN evaluations e ON e."conversationId" = c.id
                LEFT JOIN form_definitions fd ON fd.id = e."formDefinitionId"
                WHERE {where_sql}
                ORDER BY c."receivedAt" DESC
                OFFSET :skip LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
        total = ts.execute(
            text(
                f"""
                SELECT COUNT(*) FROM conversations c
                LEFT JOIN evaluations e ON e."conversationId" = c.id
                WHERE {where_sql}
                """
            ),
            {k: v for k, v in params.items() if k not in ("skip", "limit")},
        ).scalar_one()

    items: list[dict[str, Any]] = []
    for r in rows:
        base = {
            "id": r["id"],
            "externalId": r["externalId"],
            "channel": r["channel"],
            "agentName": r["agentName"],
            "customerRef": r["customerRef"],
            "status": r["status"],
            "receivedAt": r["receivedAt"].isoformat() if r["receivedAt"] else None,
        }
        if r["workflowState"] is None:
            base["evaluation"] = None
            items.append(base)
            continue
        pass_mark = None
        ss = r["scoringStrategy"]
        if isinstance(ss, dict) and isinstance(ss.get("passMark"), (int, float)):
            pass_mark = float(ss["passMark"])
        base["evaluation"] = {
            "workflowState": r["workflowState"],
            "aiScore": r["aiScore"],
            "qaScore": r["qaScore"],
            "verifierScore": r["verifierScore"],
            "finalScore": r["finalScore"],
            "passFail": _derive_pass_fail(r["finalScore"], pass_mark, r["passFail"]),
        }
        items.append(base)

    return {
        "items": items,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": int(total),
            "totalPages": (int(total) + limit - 1) // limit,
        },
    }


def _blind_alias(tenant_id: str, kind: str, source: str) -> str:
    """Deterministic anonymised alias — matches evaluations_service masking so the
    same agent/QA renders the same alias in both the conversation and evaluation
    views."""
    settings = get_settings()
    salt = settings.MASTER_ENCRYPTION_KEY or settings.JWT_SECRET or "qa-platform"
    digest = hashlib.sha256(f"{salt}:{tenant_id}:{kind}:{source}".encode()).hexdigest()[:12]
    prefix = "agent" if kind == "agent" else kind
    return f"{prefix}_{digest}"


def get_conversation(
    tenant_id: str,
    conversation_id: str,
    master: Session | None = None,
    actor_role: str | None = None,
    actor_id: str | None = None,
) -> dict[str, Any]:
    pool = get_tenant_pool()
    with pool.session(tenant_id) as ts:
        conv = ts.execute(
            select(Conversation).where(Conversation.id == conversation_id)
        ).scalar_one_or_none()
        if conv is None:
            raise not_found("CONVERSATION_NOT_FOUND", "Conversation not found")
        evaluation = ts.execute(
            select(Evaluation).where(Evaluation.conversationId == conv.id)
        ).scalar_one_or_none()
        form = None
        if evaluation is not None:
            form = ts.execute(
                select(FormDefinition).where(FormDefinition.id == evaluation.formDefinitionId)
            ).scalar_one_or_none()

        result: dict[str, Any] = {
            "id": conv.id,
            "externalId": conv.externalId,
            "departmentId": conv.departmentId,
            "channel": conv.channel,
            "agentId": conv.agentId,
            "agentName": conv.agentName,
            "customerRef": conv.customerRef,
            "content": conv.content,
            "metadata": conv.cmetadata,
            "status": conv.status,
            "receivedAt": conv.receivedAt.isoformat() if conv.receivedAt else None,
            "evaluation": None,
        }

        # Blind review: apply the same agent/QA masking the evaluation detail
        # endpoint uses, so the conversation view can't be used to bypass it.
        blind = None
        if master is not None and actor_role in ("QA", "VERIFIER"):
            from ..models.master import BlindReviewSettings

            blind = master.execute(
                select(BlindReviewSettings).where(BlindReviewSettings.tenantId == tenant_id)
            ).scalar_one_or_none()
        if blind is not None and getattr(blind, "hideAgentFromQA", False) and actor_role == "QA":
            source = result.get("agentId") or result.get("agentName") or result.get("id")
            alias = _blind_alias(tenant_id, "agent", str(source))
            result["agentId"] = alias
            result["agentName"] = alias

        if evaluation is None:
            if actor_role in (UserRole.QA.value, UserRole.VERIFIER.value):
                raise forbidden("FORBIDDEN", "You do not have access to this conversation")
            return result

        _assert_evaluation_read_access(evaluation, actor_role, actor_id)

        pass_mark = None
        if form is not None and isinstance(form.scoringStrategy, dict):
            pm = form.scoringStrategy.get("passMark")
            if isinstance(pm, (int, float)):
                pass_mark = float(pm)

        result["evaluation"] = {
            "id": evaluation.id,
            "formDefinitionId": evaluation.formDefinitionId,
            "formVersion": evaluation.formVersion,
            "workflowState": evaluation.workflowState,
            "aiScore": evaluation.aiScore,
            "qaScore": evaluation.qaScore,
            "verifierScore": evaluation.verifierScore,
            "finalScore": evaluation.finalScore,
            "passFail": _derive_pass_fail(
                evaluation.finalScore, pass_mark, evaluation.passFail
            ),
            "aiResponseData": evaluation.aiResponseData,
            "qaAdjustedData": evaluation.qaAdjustedData,
            "verifierFinalData": evaluation.verifierFinalData,
            "finalResponseData": evaluation.finalResponseData,
            "isEscalated": evaluation.isEscalated,
            "qaUserId": evaluation.qaUserId,
            "verifierUserId": evaluation.verifierUserId,
            "lockedAt": evaluation.lockedAt.isoformat() if evaluation.lockedAt else None,
        }
        if (
            blind is not None
            and getattr(blind, "hideQAFromVerifier", False)
            and actor_role == "VERIFIER"
            and result["evaluation"].get("qaUserId")
        ):
            result["evaluation"]["qaUserId"] = _blind_alias(
                tenant_id, "qa", str(result["evaluation"]["qaUserId"])
            )
        return result


# ─── upload ──────────────────────────────────────────────────────────────────

def upload_conversations(
    master: Session,
    tenant_id: str,
    *,
    channel: str,
    conversations: list[dict[str, Any]],
) -> dict[str, Any]:
    log.info(
        "Upload request received tenantId=%s channel=%s count=%s",
        tenant_id, channel, len(conversations or []),
    )
    if not conversations:
        raise bad_request("EMPTY_PAYLOAD", "No conversations provided")
    if len(conversations) > 500:
        raise bad_request("BATCH_TOO_LARGE", "Maximum 500 conversations per upload")

    tenant = master.execute(select(Tenant).where(Tenant.id == tenant_id)).scalar_one_or_none()
    if tenant is None:
        raise not_found("TENANT_NOT_FOUND", "Tenant not found")

    try:
        limits = PLAN_LIMITS[PlanType(tenant.plan)]
    except (KeyError, ValueError):
        limits = PLAN_LIMITS[PlanType.BASIC]
    monthly_cap = limits["conversationsPerMonth"]
    if monthly_cap != 999_999:
        used = usage_meter_service.get_monthly_conversation_count(master, tenant_id)
        remaining = monthly_cap - used
        if remaining <= 0:
            raise bad_request(
                "PLAN_LIMIT_EXCEEDED",
                f"Monthly conversation limit of {monthly_cap} reached. Upgrade your plan to continue.",
            )
        if len(conversations) > remaining:
            raise bad_request(
                "PLAN_LIMIT_WOULD_EXCEED",
                f"This upload would exceed your monthly limit. You have {remaining} conversations remaining this month.",
            )

    active_depts = get_active_departments(master, tenant_id)
    ctx = routing_service.load_routing_context(master, tenant_id, active_depts)

    routed: list[dict[str, Any]] = []
    for c in conversations:
        legacy = resolve_conversation_department(active_depts, channel, c.get("metadata"))
        dept, mode = routing_service.resolve_department_with_assignment_mode(
            ctx, channel=channel, metadata=c.get("metadata"), legacy_department=legacy
        )
        routed.append({**c, "routedDepartment": dept, "routedAssignmentMode": mode})

    llm_cfg = master.execute(
        select(LlmConfig).where(LlmConfig.tenantId == tenant_id)
    ).scalar_one_or_none()
    settings = get_settings()
    llm_enabled = bool(llm_cfg.enabled) if llm_cfg else False
    if llm_enabled and not settings.redis_enabled:
        log.warning(
            "LLM is enabled for tenantId=%s but REDIS_ENABLED=false; falling back to manual QA queue",
            tenant_id,
        )
        llm_enabled = False
    log.info(
        "Upload context tenantId=%s channel=%s llmEnabled=%s", tenant_id, channel, llm_enabled
    )

    pool = get_tenant_pool()
    uploaded = 0
    evaluated = 0
    form_cache: dict[str, dict[str, Any] | None] = {}

    with pool.session(tenant_id) as ts:
        for c in routed:
            ext_id = c.get("externalId") or f"__no_ext_{secrets.token_hex(8)}"
            existing_conv = ts.execute(
                select(Conversation).where(Conversation.externalId == ext_id)
            ).scalar_one_or_none()
            if existing_conv is not None:
                continue
            received_at = c.get("receivedAt")
            if isinstance(received_at, str):
                try:
                    received_at = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
                except ValueError:
                    received_at = datetime.now(timezone.utc)
            elif received_at is None:
                received_at = datetime.now(timezone.utc)

            conv = Conversation(
                externalId=c.get("externalId"),
                departmentId=(c["routedDepartment"] or {}).get("id") if c["routedDepartment"] else None,
                channel=channel.upper(),
                agentId=c.get("agentId"),
                agentName=c.get("agentName"),
                customerRef=c.get("customerRef"),
                content=c.get("content") or {},
                cmetadata=c.get("metadata"),
                receivedAt=received_at,
            )
            ts.add(conv)
            ts.flush()
            uploaded += 1

            routed_dept = c["routedDepartment"]
            form = _resolve_published_form(
                ts, tenant_id,
                channel=channel,
                department_id=routed_dept["id"] if routed_dept else None,
                cache=form_cache,
            )
            if not form:
                continue

            ai_pct = routing_service.resolve_llm_auto_processing_percent(
                ctx, channel=channel, department_id=routed_dept["id"] if routed_dept else None,
            )
            use_llm = llm_enabled and _should_use_llm(
                tenant_id=tenant_id,
                channel=channel,
                department_id=routed_dept["id"] if routed_dept else None,
                conversation_key=conv.externalId or conv.id,
                ai_pct=ai_pct,
            )

            existing_eval = ts.execute(
                select(Evaluation).where(Evaluation.conversationId == conv.id)
            ).scalar_one_or_none()
            if existing_eval is not None:
                continue

            qa_assignee = None
            if (
                routed_dept
                and routed_dept.get("autoAssignEnabled")
                and not use_llm
                and c.get("routedAssignmentMode") == "ROUND_ROBIN"
            ):
                qa_assignee = select_least_loaded_user(
                    master, ts,
                    tenant_id=tenant_id,
                    department_id=routed_dept["id"],
                    queue_type="QA_QUEUE",
                )
            qa_start = datetime.now(timezone.utc) if qa_assignee else None

            evaluation = Evaluation(
                conversationId=conv.id,
                formDefinitionId=form["id"],
                formVersion=form["version"],
                departmentId=routed_dept["id"] if routed_dept else None,
                workflowState=(
                    "AI_PENDING"
                    if use_llm
                    else ("QA_IN_PROGRESS" if qa_assignee else "QA_PENDING")
                ),
                qaUserId=(qa_assignee["id"] if qa_assignee else None),
                qaStartedAt=qa_start,
            )
            ts.add(evaluation)
            ts.flush()
            evaluated += 1

            if use_llm:
                conv.status = "EVALUATING"
                ts.flush()
                _enqueue_eval(
                    tenant_id=tenant_id,
                    conversation_id=conv.id,
                    evaluation_id=evaluation.id,
                    form_definition_id=form["id"],
                    form_version=form["version"],
                )
            else:
                ts.add(
                    WorkflowQueue(
                        evaluationId=evaluation.id,
                        queueType="QA_QUEUE",
                        departmentId=evaluation.departmentId,
                        assignedTo=qa_assignee["id"] if qa_assignee else None,
                        priority=5,
                    )
                )
                conv.status = "QA_REVIEW"
                if routed_dept:
                    conv.departmentId = routed_dept["id"]
                ts.flush()
        ts.commit()

    try:
        if uploaded:
            usage_meter_service.record_conversation(master, tenant_id, uploaded)
    except Exception:  # noqa: BLE001
        log.warning("usage_meter.record_conversation failed", exc_info=True)

    log.info(
        "Upload completed tenantId=%s channel=%s uploaded=%s evaluated=%s",
        tenant_id, channel, uploaded, evaluated,
    )
    return {"uploaded": uploaded, "evaluated": evaluated}


# ─── backfill ────────────────────────────────────────────────────────────────

def backfill_pending_evaluations(master: Session, tenant_id: str) -> dict[str, Any]:
    active_depts = get_active_departments(master, tenant_id)
    ctx = routing_service.load_routing_context(master, tenant_id, active_depts)
    llm_cfg = master.execute(
        select(LlmConfig).where(LlmConfig.tenantId == tenant_id)
    ).scalar_one_or_none()
    llm_enabled = bool(llm_cfg.enabled) if llm_cfg else False

    processed = 0
    skipped = 0
    reasons: list[str] = []
    form_cache: dict[str, dict[str, Any] | None] = {}

    pool = get_tenant_pool()
    with pool.session(tenant_id) as ts:
        pending = list(
            ts.execute(
                select(Conversation).where(Conversation.status == "PENDING")
            ).scalars()
        )
        if not pending:
            return {"processed": 0, "skipped": 0, "reason": []}

        for conv in pending:
            existing = ts.execute(
                select(Evaluation).where(Evaluation.conversationId == conv.id)
            ).scalar_one_or_none()

            if conv.departmentId:
                legacy = next(
                    (d for d in active_depts if d["id"] == conv.departmentId), None
                )
            else:
                legacy = resolve_conversation_department(
                    active_depts, conv.channel, conv.cmetadata
                )
            dept, mode = routing_service.resolve_department_with_assignment_mode(
                ctx, channel=conv.channel, metadata=conv.cmetadata, legacy_department=legacy,
            )
            ai_pct = routing_service.resolve_llm_auto_processing_percent(
                ctx, channel=conv.channel, department_id=dept["id"] if dept else None
            )
            use_llm = llm_enabled and _should_use_llm(
                tenant_id=tenant_id,
                channel=conv.channel,
                department_id=dept["id"] if dept else None,
                conversation_key=conv.externalId or conv.id,
                ai_pct=ai_pct,
            )

            if existing and existing.workflowState == "AI_PENDING" and not use_llm:
                qa_assignee = None
                if dept and dept.get("autoAssignEnabled") and mode == "ROUND_ROBIN":
                    qa_assignee = select_least_loaded_user(
                        master, ts,
                        tenant_id=tenant_id,
                        department_id=dept["id"],
                        queue_type="QA_QUEUE",
                    )
                existing.departmentId = dept["id"] if dept else None
                existing.workflowState = "QA_IN_PROGRESS" if qa_assignee else "QA_PENDING"
                existing.qaUserId = qa_assignee["id"] if qa_assignee else None
                existing.qaStartedAt = datetime.now(timezone.utc) if qa_assignee else None
                wq = ts.execute(
                    select(WorkflowQueue).where(WorkflowQueue.evaluationId == existing.id)
                ).scalar_one_or_none()
                if wq is None:
                    ts.add(
                        WorkflowQueue(
                            evaluationId=existing.id,
                            queueType="QA_QUEUE",
                            departmentId=dept["id"] if dept else None,
                            assignedTo=qa_assignee["id"] if qa_assignee else None,
                            priority=5,
                        )
                    )
                else:
                    wq.queueType = "QA_QUEUE"
                    wq.departmentId = dept["id"] if dept else None
                    wq.assignedTo = qa_assignee["id"] if qa_assignee else None
                    wq.priority = 5
                conv.status = "QA_REVIEW"
                if dept:
                    conv.departmentId = dept["id"]
                processed += 1
                continue

            if existing is not None:
                skipped += 1
                continue

            form = _resolve_published_form(
                ts, tenant_id,
                channel=conv.channel,
                department_id=dept["id"] if dept else None,
                cache=form_cache,
            )
            if not form:
                skipped += 1
                msg = f"No published form for channel {conv.channel}"
                if msg not in reasons:
                    reasons.append(msg)
                continue

            evaluation = Evaluation(
                conversationId=conv.id,
                formDefinitionId=form["id"],
                formVersion=form["version"],
                departmentId=dept["id"] if dept else None,
                workflowState="AI_PENDING" if use_llm else "QA_PENDING",
            )
            ts.add(evaluation)
            ts.flush()

            if use_llm:
                _enqueue_eval(
                    tenant_id=tenant_id,
                    conversation_id=conv.id,
                    evaluation_id=evaluation.id,
                    form_definition_id=form["id"],
                    form_version=form["version"],
                )
            elif not llm_enabled:
                qa_assignee = None
                if dept and dept.get("autoAssignEnabled") and mode == "ROUND_ROBIN":
                    qa_assignee = select_least_loaded_user(
                        master, ts,
                        tenant_id=tenant_id,
                        department_id=dept["id"],
                        queue_type="QA_QUEUE",
                    )
                if qa_assignee:
                    evaluation.workflowState = "QA_IN_PROGRESS"
                    evaluation.qaUserId = qa_assignee["id"]
                    evaluation.qaStartedAt = datetime.now(timezone.utc)
                ts.add(
                    WorkflowQueue(
                        evaluationId=evaluation.id,
                        queueType="QA_QUEUE",
                        departmentId=dept["id"] if dept else None,
                        assignedTo=qa_assignee["id"] if qa_assignee else None,
                        priority=5,
                    )
                )
                conv.status = "QA_REVIEW"
                if dept:
                    conv.departmentId = dept["id"]
            processed += 1

        ts.commit()

    return {"processed": processed, "skipped": skipped, "reason": reasons}


# ─── remap corrupted forms ───────────────────────────────────────────────────

def _is_corrupted_form(sections: Any, questions: Any) -> bool:
    sec = sections if isinstance(sections, list) else []
    q = questions if isinstance(questions, list) else []
    has_section = any(
        isinstance(s, dict)
        and isinstance(s.get("id"), str)
        and isinstance(s.get("title"), str)
        for s in sec
    )
    has_question = any(
        isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("key"), str)
        for item in q
    )
    return not (has_section and has_question)


def remap_corrupted_qa_pending(tenant_id: str) -> dict[str, Any]:
    pool = get_tenant_pool()
    remapped = 0
    skipped = 0
    reasons: list[str] = []
    valid_by_channel: dict[str, dict[str, Any] | None] = {}

    with pool.session(tenant_id) as ts:
        evals = list(
            ts.execute(
                select(Evaluation, FormDefinition, Conversation)
                .join(FormDefinition, FormDefinition.id == Evaluation.formDefinitionId)
                .join(Conversation, Conversation.id == Evaluation.conversationId)
                .where(Evaluation.workflowState == "QA_PENDING")
            ).all()
        )
        if not evals:
            return {"remapped": 0, "skipped": 0, "reason": []}

        def _get_valid(channel: str) -> dict[str, Any] | None:
            if channel in valid_by_channel:
                return valid_by_channel[channel]
            channel_json = f'["{channel}"]'
            candidates = ts.execute(
                text(
                    'SELECT id, version, sections, questions FROM form_definitions '
                    "WHERE status = 'PUBLISHED' "
                    "  AND channels::jsonb @> :ch::jsonb "
                    "ORDER BY version DESC"
                ),
                {"ch": channel_json},
            ).mappings().all()
            chosen = None
            for f in candidates:
                if not _is_corrupted_form(f["sections"], f["questions"]):
                    chosen = {"id": f["id"], "version": f["version"]}
                    break
            valid_by_channel[channel] = chosen
            return chosen

        for ev, fd, conv in evals:
            if not _is_corrupted_form(fd.sections, fd.questions):
                skipped += 1
                continue
            replacement = _get_valid(conv.channel)
            if not replacement:
                skipped += 1
                msg = f"No valid published form found for channel {conv.channel}"
                if msg not in reasons:
                    reasons.append(msg)
                continue
            if (
                replacement["id"] == ev.formDefinitionId
                and replacement["version"] == ev.formVersion
            ):
                skipped += 1
                continue
            ev.formDefinitionId = replacement["id"]
            ev.formVersion = replacement["version"]
            remapped += 1
        ts.commit()

    return {"remapped": remapped, "skipped": skipped, "reason": reasons}
