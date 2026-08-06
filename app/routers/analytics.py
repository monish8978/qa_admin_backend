"""Analytics endpoints — /api/v1/analytics."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..common.enums import UserRole
from ..common.responses import build_response
from ..deps import get_db, get_request_id, require_roles
from ..services import analytics_service as svc

router = APIRouter(prefix="/analytics", tags=["analytics"])
log = logging.getLogger("qa.api.routers.analytics")


def _range(from_: str | None, to: str | None, default_days: int = 30) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    f = datetime.fromisoformat(from_.replace("Z", "+00:00")) if from_ else now - timedelta(days=default_days)
    t = datetime.fromisoformat(to.replace("Z", "+00:00")) if to else now
    return f, t


def _admin_deps():
    return (
        Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
        Annotated[Session, Depends(get_db)],
        Annotated[str, Depends(get_request_id)],
    )


@router.get("/overview")
def overview(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN, UserRole.QA, UserRole.VERIFIER))],
    rid: Annotated[str, Depends(get_request_id)],
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
):
    tenant_id = payload["tenantId"]
    user_id = payload.get("sub")
    role = payload.get("role")
    log.info("[%s] Fetching analytics overview for tenant: %s, user: %s (role=%s, from=%s, to=%s)", rid, tenant_id, user_id, role, from_, to)
    f, t = _range(from_, to)
    res = svc.overview(tenant_id, f, t, role=role, user_id=user_id)
    log.info("[%s] Successfully retrieved analytics overview for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


@router.get("/agent-performance")
def agent_performance(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN, UserRole.QA, UserRole.VERIFIER))],
    rid: Annotated[str, Depends(get_request_id)],
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching agent performance for tenant: %s (from=%s, to=%s)", rid, tenant_id, from_, to)
    f, t = _range(from_, to)
    res = svc.agent_performance(tenant_id, f, t, role=payload["role"], user_id=payload["sub"])
    log.info("[%s] Successfully retrieved agent performance for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


@router.get("/deviation-trends")
def deviation_trends(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN, UserRole.QA, UserRole.VERIFIER))],
    rid: Annotated[str, Depends(get_request_id)],
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching deviation trends for tenant: %s (from=%s, to=%s)", rid, tenant_id, from_, to)
    f, t = _range(from_, to)
    res = svc.deviation_trends(tenant_id, f, t, role=payload["role"], user_id=payload["sub"])
    log.info("[%s] Successfully retrieved deviation trends for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


@router.get("/question-deviations")
def question_deviations(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN, UserRole.QA, UserRole.VERIFIER))],
    rid: Annotated[str, Depends(get_request_id)],
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching question deviations for tenant: %s (from=%s, to=%s)", rid, tenant_id, from_, to)
    f, t = _range(from_, to)
    res = svc.question_deviations(tenant_id, f, t, role=payload["role"], user_id=payload["sub"])
    log.info("[%s] Successfully retrieved question deviations for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


@router.get("/escalation-stats")
def escalation_stats(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN, UserRole.QA, UserRole.VERIFIER))],
    rid: Annotated[str, Depends(get_request_id)],
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching escalation stats for tenant: %s (from=%s, to=%s)", rid, tenant_id, from_, to)
    f, t = _range(from_, to)
    res = svc.escalation_stats(tenant_id, f, t, role=payload["role"], user_id=payload["sub"])
    log.info("[%s] Successfully retrieved escalation stats for tenant: %s", rid, tenant_id)
    return build_response(res, rid)



@router.get("/verifier-overrides")
def verifier_overrides(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN, UserRole.QA, UserRole.VERIFIER))],
    rid: Annotated[str, Depends(get_request_id)],
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching verifier overrides for tenant: %s (from=%s, to=%s)", rid, tenant_id, from_, to)
    f, t = _range(from_, to)
    res = svc.verifier_overrides(tenant_id, f, t, role=payload["role"], user_id=payload["sub"])
    log.info("[%s] Successfully retrieved verifier overrides for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


@router.get("/rejection-reasons")
def rejection_reasons(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN, UserRole.QA, UserRole.VERIFIER))],
    rid: Annotated[str, Depends(get_request_id)],
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching rejection reasons for tenant: %s (from=%s, to=%s)", rid, tenant_id, from_, to)
    f, t = _range(from_, to)
    res = svc.rejection_reasons(tenant_id, f, t, role=payload["role"], user_id=payload["sub"])
    log.info("[%s] Successfully retrieved rejection reasons for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


@router.get("/score-trends")
def score_trends(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN, UserRole.QA, UserRole.VERIFIER))],
    rid: Annotated[str, Depends(get_request_id)],
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching score trends for tenant: %s (from=%s, to=%s)", rid, tenant_id, from_, to)
    f, t = _range(from_, to)
    res = svc.score_trends(tenant_id, f, t, role=payload["role"], user_id=payload["sub"])
    log.info("[%s] Successfully retrieved score trends for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


# @router.get("/ai-usage-trends")
# def ai_usage_trends(
#     payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN, UserRole.QA, UserRole.VERIFIER))],
#     db: Annotated[Session, Depends(get_db)],
#     rid: Annotated[str, Depends(get_request_id)],
#     from_: str | None = Query(None, alias="from"),
#     to: str | None = Query(None),
# ):
#     tenant_id = payload["tenantId"]
#     log.info("[%s] Fetching AI usage trends for tenant: %s (from=%s, to=%s)", rid, tenant_id, from_, to)
#     f, t = _range(from_, to, default_days=365)
#     res = svc.ai_usage_trends(db, tenant_id, f, t)
#     log.info("[%s] Successfully retrieved AI usage trends for tenant: %s", rid, tenant_id)
#     return build_response(res, rid)


@router.get("/qa-reviewer-performance")
def qa_reviewer_performance(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN, UserRole.QA, UserRole.VERIFIER))],
    rid: Annotated[str, Depends(get_request_id)],
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching QA reviewer performance for tenant: %s (from=%s, to=%s)", rid, tenant_id, from_, to)
    f, t = _range(from_, to)
    res = svc.qa_reviewer_performance(tenant_id, f, t, role=payload["role"], user_id=payload["sub"])
    log.info("[%s] Successfully retrieved QA reviewer performance for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


@router.get("/verifier-report")
def verifier_report(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN, UserRole.QA, UserRole.VERIFIER))],
    rid: Annotated[str, Depends(get_request_id)],
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching verifier report for tenant: %s (from=%s, to=%s)", rid, tenant_id, from_, to)
    f, t = _range(from_, to)
    res = svc.verifier_report(tenant_id, f, t, role=payload["role"], user_id=payload["sub"])
    log.info("[%s] Successfully retrieved verifier report for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


@router.get("/conversation-volume")
def conversation_volume(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN, UserRole.QA, UserRole.VERIFIER))],
    rid: Annotated[str, Depends(get_request_id)],
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching conversation volume for tenant: %s (from=%s, to=%s)", rid, tenant_id, from_, to)
    f, t = _range(from_, to)
    res = svc.conversation_volume(tenant_id, f, t, role=payload["role"], user_id=payload["sub"])
    log.info("[%s] Successfully retrieved conversation volume for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


@router.get("/sla-report")
def sla_report(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN, UserRole.QA, UserRole.VERIFIER))],
    rid: Annotated[str, Depends(get_request_id)],
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching SLA report for tenant: %s (from=%s, to=%s)", rid, tenant_id, from_, to)
    f, t = _range(from_, to)
    res = svc.sla_report(tenant_id, f, t, role=payload["role"], user_id=payload["sub"])
    log.info("[%s] Successfully retrieved SLA report for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


@router.get("/form-score-distribution")
def form_score_distribution(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN, UserRole.QA, UserRole.VERIFIER))],
    rid: Annotated[str, Depends(get_request_id)],
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching form score distribution for tenant: %s (from=%s, to=%s)", rid, tenant_id, from_, to)
    f, t = _range(from_, to)
    res = svc.form_score_distribution(tenant_id, f, t, role=payload["role"], user_id=payload["sub"])
    log.info("[%s] Successfully retrieved form score distribution for tenant: %s", rid, tenant_id)
    return build_response(res, rid)
