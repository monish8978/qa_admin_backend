"""Tenant-level settings — /api/v1/settings."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..common.enums import UserRole
from ..common.exceptions import not_found
from ..common.responses import build_response
from ..deps import get_current_payload, get_db, get_request_id, require_roles
from ..models.master import Tenant
from ..schemas.settings import (
    BlindReviewResponse,
    EscalationRuleResponse,
    PatchBlindReviewRequest,
    PatchEscalationRequest,
    SendTestEmailRequest,
    UpsertEmailSettingsRequest,
)
from ..services import tenant_settings_service as svc

router = APIRouter(prefix="/settings", tags=["settings"])
log = logging.getLogger("qa.api.routers.tenant_settings")


@router.get("")
def get_settings(
    payload: Annotated[dict, Depends(get_current_payload)],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching aggregate tenant settings for tenant: %s", rid, tenant_id)

    tenant = db.execute(select(Tenant).where(Tenant.id == tenant_id)).scalar_one_or_none()
    if tenant is None:
        raise not_found("TENANT_NOT_FOUND", "Tenant not found")

    escalation = svc.get_escalation(db, tenant_id)
    blind_review = svc.get_blind_review(db, tenant_id)

    response = {
        "tenant": {
            "id": tenant.id,
            "name": tenant.name,
            "slug": tenant.slug,
            "plan": tenant.plan,
            "status": tenant.status,
        },
        "escalation": _esc(escalation).model_dump(),
        "blindReview": _blind(blind_review).model_dump(),
    }

    log.info("[%s] Successfully retrieved aggregate tenant settings for tenant: %s", rid, tenant_id)
    return build_response(response, rid)


def _esc(row) -> EscalationRuleResponse:
    return EscalationRuleResponse(
        qaDeviationThreshold=row.qaDeviationThreshold,
        verifierDeviationThreshold=row.verifierDeviationThreshold,
        verifierMinRangeStart=row.verifierMinRangeStart,
        verifierMinRangeEnd=row.verifierMinRangeEnd,
        verifierMaxRangeStart=row.verifierMaxRangeStart,
        verifierMaxRangeEnd=row.verifierMaxRangeEnd,
        staleQueueHours=row.staleQueueHours,
    )


def _blind(row) -> BlindReviewResponse:
    return BlindReviewResponse(
        hideAgentFromQA=row.hideAgentFromQA,
        hideQAFromVerifier=row.hideQAFromVerifier,
    )


@router.get("/escalation")
def get_escalation(
    payload: Annotated[dict, Depends(get_current_payload)],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching escalation settings for tenant: %s", rid, tenant_id)
    res = svc.get_escalation(db, tenant_id)
    log.info("[%s] Successfully retrieved escalation settings for tenant: %s", rid, tenant_id)
    return build_response(_esc(res).model_dump(), rid)


@router.patch("/escalation")
def patch_escalation(
    body: PatchEscalationRequest,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Patching escalation settings for tenant: %s", rid, tenant_id)
    row = svc.patch_escalation(
        db, tenant_id, body.model_dump(exclude_unset=True)
    )
    log.info("[%s] Successfully patched escalation settings for tenant: %s", rid, tenant_id)
    return build_response(_esc(row).model_dump(), rid)


@router.get("/blind-review")
def get_blind_review(
    payload: Annotated[dict, Depends(get_current_payload)],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching blind review settings for tenant: %s", rid, tenant_id)
    res = svc.get_blind_review(db, tenant_id)
    log.info("[%s] Successfully retrieved blind review settings for tenant: %s", rid, tenant_id)
    return build_response(_blind(res).model_dump(), rid)


@router.patch("/blind-review")
def patch_blind_review(
    body: PatchBlindReviewRequest,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Patching blind review settings for tenant: %s", rid, tenant_id)
    row = svc.patch_blind_review(
        db, tenant_id, body.model_dump(exclude_unset=True)
    )
    log.info("[%s] Successfully patched blind review settings for tenant: %s", rid, tenant_id)
    return build_response(_blind(row).model_dump(), rid)


@router.get("/email")
def get_email_settings(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching email settings for tenant: %s", rid, tenant_id)
    res = svc.get_email_settings(db, tenant_id)
    log.info("[%s] Successfully retrieved email settings for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


@router.patch("/email")
def upsert_email_settings(
    body: UpsertEmailSettingsRequest,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Upserting email settings for tenant: %s", rid, tenant_id)
    data = svc.upsert_email_settings(
        db, tenant_id, body.model_dump(exclude_unset=True)
    )
    log.info("[%s] Successfully upserted email settings for tenant: %s", rid, tenant_id)
    return build_response(data, rid)


@router.post("/email/test")
def send_test_email(
    body: SendTestEmailRequest,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Sending test email to: %s for tenant: %s", rid, body.to, tenant_id)
    res = svc.send_test_email(db, tenant_id, body.to)
    log.info("[%s] Successfully sent test email to: %s for tenant: %s", rid, body.to, tenant_id)
    return build_response(res, rid)


@router.post("/api-keys/rotate")
def rotate_api_key(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Rotating ingest API key for tenant: %s", rid, tenant_id)
    from ..services import webhooks_ingest_service as ingest_svc

    raw_key = ingest_svc.rotate_api_key(tenant_id)
    log.info("[%s] Successfully rotated ingest API key for tenant: %s", rid, tenant_id)
    return build_response({"apiKey": raw_key}, rid)


@router.get("/onboarding-status")
def onboarding_status(
    payload: Annotated[dict, Depends(get_current_payload)],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching onboarding status for tenant: %s", rid, tenant_id)
    res = svc.get_onboarding_status(db, tenant_id)
    log.info("[%s] Successfully retrieved onboarding status for tenant: %s", rid, tenant_id)
    return build_response(res, rid)
