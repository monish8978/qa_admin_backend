"""Evaluations REST endpoints — /api/v1/evaluations."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from ..common.enums import UserRole
from ..common.responses import build_response
from ..deps import get_current_payload, get_db, get_request_id, require_roles
from ..schemas.evaluations import (
    BulkReAuditAiRequest,
    BulkRoundRobinRequest,
    ManualAssignRequest,
    PreviewScoreRequest,
    QaSubmitRequest,
    ReAuditAiRequest,
    ReassignRequest,
    ResolveAuditCaseRequest,
    VerifierModifyRequest,
    VerifierRejectRequest,
)
from ..services import evaluations_service as svc

router = APIRouter(prefix="/evaluations", tags=["evaluations"])
log = logging.getLogger("qa.api.routers.evaluations")


def _wrap(data: Any, rid: str) -> dict:
    return build_response(data, rid)


# ── Listings ─────────────────────────────────────────────────────────────────


@router.get("")
def list_evaluations(
    payload: Annotated[dict, Depends(get_current_payload)],
    rid: Annotated[str, Depends(get_request_id)],
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    workflowState: Annotated[str | None, Query()] = None,
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Listing evaluations for tenant: %s (page=%d, limit=%d, workflowState=%s)", rid, tenant_id, page, limit, workflowState)
    result = svc.list_evaluations(
        tenant_id,
        {"page": page, "limit": limit, "workflowState": workflowState},
        role=payload["role"],
        user_id=payload["sub"],
    )
    log.info("[%s] Successfully listed evaluations for tenant: %s", rid, tenant_id)
    return _wrap(result, rid)


@router.get("/queue/qa")
def get_qa_queue(
    payload: Annotated[dict, Depends(get_current_payload)],
    rid: Annotated[str, Depends(get_request_id)],
    _r: Annotated[None, Depends(require_roles(UserRole.QA, UserRole.ADMIN))] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[str | None, Query()] = None,
    departmentId: Annotated[str | None, Query()] = None,
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching QA queue for tenant: %s (page=%d, limit=%d, search=%s, departmentId=%s)", rid, tenant_id, page, limit, search, departmentId)
    result = svc.get_qa_queue(
        tenant_id,
        page=page,
        limit=limit,
        search=search,
        department_id=departmentId,
        role=payload["role"],
        user_id=payload["sub"],
    )
    log.info("[%s] Successfully fetched QA queue for tenant: %s", rid, tenant_id)
    return _wrap(result, rid)


@router.get("/queue/verifier")
def get_verifier_queue(
    payload: Annotated[dict, Depends(get_current_payload)],
    rid: Annotated[str, Depends(get_request_id)],
    _r: Annotated[None, Depends(require_roles(UserRole.VERIFIER, UserRole.ADMIN))] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[str | None, Query()] = None,
    departmentId: Annotated[str | None, Query()] = None,
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching verifier queue for tenant: %s (page=%d, limit=%d, search=%s, departmentId=%s)", rid, tenant_id, page, limit, search, departmentId)
    result = svc.get_verifier_queue(
        tenant_id,
        page=page,
        limit=limit,
        search=search,
        department_id=departmentId,
        role=payload["role"],
        user_id=payload["sub"],
    )
    log.info("[%s] Successfully fetched verifier queue for tenant: %s", rid, tenant_id)
    return _wrap(result, rid)


@router.get("/queue/escalation")
def get_escalation_queue(
    payload: Annotated[dict, Depends(get_current_payload)],
    rid: Annotated[str, Depends(get_request_id)],
    _r: Annotated[None, Depends(require_roles(UserRole.ADMIN))] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[str | None, Query()] = None,
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching escalation queue for tenant: %s (page=%d, limit=%d, search=%s)", rid, tenant_id, page, limit, search)
    result = svc.get_escalation_queue(
        tenant_id,
        page=page,
        limit=limit,
        search=search,
        role=payload["role"],
        user_id=payload["sub"],
    )
    log.info("[%s] Successfully fetched escalation queue for tenant: %s", rid, tenant_id)
    return _wrap(result, rid)


@router.get("/queue/audit")
def get_audit_queue(
    payload: Annotated[dict, Depends(get_current_payload)],
    rid: Annotated[str, Depends(get_request_id)],
    _r: Annotated[None, Depends(require_roles(UserRole.ADMIN))] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[str | None, Query()] = None,
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching audit queue for tenant: %s (page=%d, limit=%d, search=%s)", rid, tenant_id, page, limit, search)
    result = svc.get_audit_queue(
        tenant_id,
        page=page,
        limit=limit,
        search=search,
        role=payload["role"],
        user_id=payload["sub"],
    )
    log.info("[%s] Successfully fetched audit queue for tenant: %s", rid, tenant_id)
    return _wrap(result, rid)


# ── Preview, audit export, prompt log ────────────────────────────────────────


@router.post("/preview-score")
def preview_score(
    body: PreviewScoreRequest,
    payload: Annotated[dict, Depends(get_current_payload)],
    rid: Annotated[str, Depends(get_request_id)],
    _r: Annotated[None, Depends(require_roles(UserRole.ADMIN))] = None,
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Previewing score for tenant: %s (formId=%s)", rid, tenant_id, body.formId)
    result = svc.preview_score(tenant_id, body.formId, body.answers)
    log.info("[%s] Successfully generated score preview for tenant: %s (formId=%s)", rid, tenant_id, body.formId)
    return _wrap(result, rid)


@router.get("/audit/export")
def export_audit_csv(
    payload: Annotated[dict, Depends(get_current_payload)],
    _r: Annotated[None, Depends(require_roles(UserRole.ADMIN))] = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
    evaluationId: Annotated[str | None, Query()] = None,
):
    tenant_id = payload["tenantId"]
    log.info("Exporting audit CSV for tenant: %s (from=%s, to=%s, evaluationId=%s)", tenant_id, from_, to, evaluationId)
    from_dt = datetime.fromisoformat(from_) if from_ else None
    to_dt = datetime.fromisoformat(to) if to else None
    csv = svc.export_audit_log_csv(
        tenant_id, from_date=from_dt, to_date=to_dt, evaluation_id=evaluationId
    )
    ts = datetime.utcnow().isoformat().replace(":", "-").replace(".", "-")
    log.info("Successfully exported audit CSV for tenant: %s (length=%d)", tenant_id, len(csv))
    return Response(
        content=csv,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="audit-log-{ts}.csv"'},
    )


@router.get("/logs/prompt-audit")
def get_prompt_audit_log(
    payload: Annotated[dict, Depends(get_current_payload)],
    rid: Annotated[str, Depends(get_request_id)],
    _r: Annotated[None, Depends(require_roles(UserRole.ADMIN))] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    evaluationId: Annotated[str | None, Query()] = None,
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching prompt audit logs for tenant: %s (limit=%d, evaluationId=%s)", rid, tenant_id, limit, evaluationId)
    result = svc.get_prompt_audit_log(
        tenant_id, {"limit": limit, "evaluationId": evaluationId}
    )
    log.info("[%s] Successfully fetched prompt audit logs for tenant: %s", rid, tenant_id)
    return _wrap(result, rid)


# ── Single evaluation ────────────────────────────────────────────────────────


@router.get("/{eval_id}")
def get_one(
    eval_id: str,
    payload: Annotated[dict, Depends(get_current_payload)],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching evaluation details for tenant: %s, eval_id: %s", rid, tenant_id, eval_id)
    result = svc.get_evaluation(db, tenant_id, eval_id, payload["role"], payload["sub"])
    log.info("[%s] Successfully fetched evaluation details for tenant: %s, eval_id: %s", rid, tenant_id, eval_id)
    return _wrap(result, rid)


@router.get("/{eval_id}/audit")
def get_audit(
    eval_id: str,
    payload: Annotated[dict, Depends(get_current_payload)],
    rid: Annotated[str, Depends(get_request_id)],
    _r: Annotated[None, Depends(require_roles(UserRole.ADMIN))] = None,
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching audit log for tenant: %s, eval_id: %s", rid, tenant_id, eval_id)
    result = svc.get_audit_log(tenant_id, eval_id, payload["role"], payload["sub"])
    log.info("[%s] Successfully fetched audit log for tenant: %s, eval_id: %s", rid, tenant_id, eval_id)
    return _wrap(result, rid)


# ── QA actions ───────────────────────────────────────────────────────────────


@router.post("/{eval_id}/qa-start")
def qa_start(
    eval_id: str,
    payload: Annotated[dict, Depends(get_current_payload)],
    rid: Annotated[str, Depends(get_request_id)],
    _r: Annotated[None, Depends(require_roles(UserRole.QA, UserRole.ADMIN))] = None,
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Starting QA evaluation for tenant: %s, eval_id: %s (qaId=%s)", rid, tenant_id, eval_id, payload["sub"])
    result = svc.qa_start(tenant_id, eval_id, payload["sub"], payload["role"])
    log.info("[%s] Successfully started QA evaluation for tenant: %s, eval_id: %s", rid, tenant_id, eval_id)
    return _wrap(result, rid)


@router.post("/{eval_id}/qa-submit")
def qa_submit(
    eval_id: str,
    body: QaSubmitRequest,
    payload: Annotated[dict, Depends(get_current_payload)],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
    _r: Annotated[None, Depends(require_roles(UserRole.QA, UserRole.ADMIN))] = None,
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Submitting QA evaluation for tenant: %s, eval_id: %s (qaId=%s)", rid, tenant_id, eval_id, payload["sub"])
    result = svc.qa_submit(
        db,
        tenant_id,
        eval_id,
        payload["sub"],
        payload["role"],
        body.model_dump(),
    )
    log.info("[%s] Successfully submitted QA evaluation for tenant: %s, eval_id: %s", rid, tenant_id, eval_id)
    return _wrap(result, rid)


# ── Verifier actions ─────────────────────────────────────────────────────────


@router.post("/{eval_id}/verifier-start")
def verifier_start(
    eval_id: str,
    payload: Annotated[dict, Depends(get_current_payload)],
    rid: Annotated[str, Depends(get_request_id)],
    _r: Annotated[None, Depends(require_roles(UserRole.VERIFIER, UserRole.ADMIN))] = None,
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Starting Verifier evaluation for tenant: %s, eval_id: %s (verifierId=%s)", rid, tenant_id, eval_id, payload["sub"])
    result = svc.verifier_start(
        tenant_id, eval_id, payload["sub"], payload["role"]
    )
    log.info("[%s] Successfully started Verifier evaluation for tenant: %s, eval_id: %s", rid, tenant_id, eval_id)
    return _wrap(result, rid)


@router.post("/{eval_id}/verifier-approve")
def verifier_approve(
    eval_id: str,
    payload: Annotated[dict, Depends(get_current_payload)],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
    _r: Annotated[None, Depends(require_roles(UserRole.VERIFIER, UserRole.ADMIN))] = None,
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Approving evaluation by Verifier for tenant: %s, eval_id: %s (verifierId=%s)", rid, tenant_id, eval_id, payload["sub"])
    result = svc.verifier_approve(
        db, tenant_id, eval_id, payload["sub"], payload["role"]
    )
    log.info("[%s] Successfully approved evaluation by Verifier for tenant: %s, eval_id: %s", rid, tenant_id, eval_id)
    return _wrap(result, rid)


@router.post("/{eval_id}/verifier-modify")
def verifier_modify(
    eval_id: str,
    body: VerifierModifyRequest,
    payload: Annotated[dict, Depends(get_current_payload)],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
    _r: Annotated[None, Depends(require_roles(UserRole.VERIFIER, UserRole.ADMIN))] = None,
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Modifying evaluation by Verifier for tenant: %s, eval_id: %s (verifierId=%s)", rid, tenant_id, eval_id, payload["sub"])
    result = svc.verifier_modify(
        db,
        tenant_id,
        eval_id,
        payload["sub"],
        payload["role"],
        body.model_dump(),
    )
    log.info("[%s] Successfully modified evaluation by Verifier for tenant: %s, eval_id: %s", rid, tenant_id, eval_id)
    return _wrap(result, rid)


@router.post("/{eval_id}/verifier-reject")
def verifier_reject(
    eval_id: str,
    body: VerifierRejectRequest,
    payload: Annotated[dict, Depends(get_current_payload)],
    rid: Annotated[str, Depends(get_request_id)],
    _r: Annotated[None, Depends(require_roles(UserRole.VERIFIER, UserRole.ADMIN))] = None,
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Rejecting evaluation by Verifier for tenant: %s, eval_id: %s (verifierId=%s)", rid, tenant_id, eval_id, payload["sub"])
    result = svc.verifier_reject(
        tenant_id, eval_id, payload["sub"], payload["role"], body.model_dump(exclude_none=True)
    )
    log.info("[%s] Successfully rejected evaluation by Verifier for tenant: %s, eval_id: %s", rid, tenant_id, eval_id)
    return _wrap(result, rid)


# ── Retry / re-audit ─────────────────────────────────────────────────────────


@router.post("/{eval_id}/retry-ai")
def retry_ai(
    eval_id: str,
    payload: Annotated[dict, Depends(get_current_payload)],
    rid: Annotated[str, Depends(get_request_id)],
    _r: Annotated[None, Depends(require_roles(UserRole.ADMIN))] = None,
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Requesting AI retry for tenant: %s, eval_id: %s", rid, tenant_id, eval_id)
    result = svc.retry_ai_failed(
        tenant_id, eval_id, payload["sub"], payload["role"]
    )
    log.info("[%s] Successfully initiated AI retry for tenant: %s, eval_id: %s", rid, tenant_id, eval_id)
    return _wrap(result, rid)


@router.post("/{eval_id}/re-audit-ai")
def re_audit_ai(
    eval_id: str,
    body: ReAuditAiRequest,
    payload: Annotated[dict, Depends(get_current_payload)],
    rid: Annotated[str, Depends(get_request_id)],
    _r: Annotated[None, Depends(require_roles(UserRole.ADMIN))] = None,
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Requesting AI re-audit for tenant: %s, eval_id: %s (reason=%s)", rid, tenant_id, eval_id, body.reason)
    result = svc.re_audit_ai(
        tenant_id, eval_id, payload["sub"], payload["role"], body.reason
    )
    log.info("[%s] Successfully initiated AI re-audit for tenant: %s, eval_id: %s", rid, tenant_id, eval_id)
    return _wrap(result, rid)


@router.post("/re-audit-ai/bulk")
def bulk_re_audit_ai(
    body: BulkReAuditAiRequest,
    payload: Annotated[dict, Depends(get_current_payload)],
    rid: Annotated[str, Depends(get_request_id)],
    _r: Annotated[None, Depends(require_roles(UserRole.ADMIN))] = None,
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Requesting bulk AI re-audit for tenant: %s (count=%d, reason=%s)", rid, tenant_id, len(body.evaluationIds), body.reason)
    result = svc.bulk_re_audit_ai(
        tenant_id,
        body.evaluationIds,
        payload["sub"],
        payload["role"],
        body.reason,
    )
    log.info("[%s] Successfully initiated bulk AI re-audit for tenant: %s", rid, tenant_id)
    return _wrap(result, rid)


# ── Audit case resolution ────────────────────────────────────────────────────


@router.patch("/audit-cases/{audit_case_id}/resolve")
def resolve_audit_case(
    audit_case_id: str,
    body: ResolveAuditCaseRequest,
    payload: Annotated[dict, Depends(get_current_payload)],
    rid: Annotated[str, Depends(get_request_id)],
    _r: Annotated[None, Depends(require_roles(UserRole.ADMIN))] = None,
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Resolving audit case for tenant: %s, audit_case_id: %s (dismiss=%s)", rid, tenant_id, audit_case_id, body.dismiss)
    result = svc.resolve_audit_case(
        tenant_id,
        audit_case_id,
        payload["sub"],
        payload["role"],
        dismiss=body.dismiss,
        note=body.note,
    )
    log.info("[%s] Successfully resolved audit case for tenant: %s, audit_case_id: %s", rid, tenant_id, audit_case_id)
    return _wrap(result, rid)


# ── Assignment endpoints ─────────────────────────────────────────────────────


@router.post("/assign")
def manual_assign(
    body: ManualAssignRequest,
    payload: Annotated[dict, Depends(get_current_payload)],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
    _r: Annotated[None, Depends(require_roles(UserRole.ADMIN))] = None,
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Manually assigning evaluation for tenant: %s (evaluationId=%s, userId=%s)", rid, tenant_id, body.evaluationId, body.userId)
    result = svc.manual_assign(
        db,
        tenant_id,
        body.evaluationId,
        body.userId,
        payload["sub"],
        payload["role"],
    )
    log.info("[%s] Successfully assigned evaluation for tenant: %s (evaluationId=%s)", rid, tenant_id, body.evaluationId)
    return _wrap(result, rid)


@router.post("/assign/round-robin")
def round_robin_assign(
    body: BulkRoundRobinRequest,
    payload: Annotated[dict, Depends(get_current_payload)],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
    _r: Annotated[None, Depends(require_roles(UserRole.ADMIN))] = None,
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Round-robin assigning evaluations for tenant: %s (queueType=%s, departmentId=%s, limit=%s)", rid, tenant_id, body.queueType, body.departmentId, body.limit)
    result = svc.round_robin_assign(
        db,
        tenant_id,
        body.queueType,
        payload["sub"],
        payload["role"],
        department_id=body.departmentId,
        limit=body.limit,
    )
    log.info("[%s] Successfully round-robin assigned evaluations for tenant: %s", rid, tenant_id)
    return _wrap(result, rid)


@router.post("/reassign")
def reassign(
    body: ReassignRequest,
    payload: Annotated[dict, Depends(get_current_payload)],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
    _r: Annotated[None, Depends(require_roles(UserRole.ADMIN))] = None,
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Reassigning evaluation for tenant: %s (evaluationId=%s, newUserId=%s)", rid, tenant_id, body.evaluationId, body.newUserId)
    result = svc.reassign(
        db,
        tenant_id,
        body.evaluationId,
        body.newUserId,
        payload["sub"],
        payload["role"],
        body.reason,
    )
    log.info("[%s] Successfully reassigned evaluation for tenant: %s (evaluationId=%s)", rid, tenant_id, body.evaluationId)
    return _wrap(result, rid)
