"""Billing endpoints — mirrors apps/api/src/billing/billing.controller.ts.

`POST /billing/stripe/webhook` is **unauthenticated** and reads the raw
request body to verify the Stripe signature.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from ..common.enums import UserRole
from ..common.responses import build_response
from ..deps import get_current_payload, get_db, get_request_id, require_roles
from ..schemas.billing import (
    ChangePlanRequest,
    CreateCheckoutSessionRequest,
    CreatePortalSessionRequest,
)
from ..services import billing_service as svc

router = APIRouter(prefix="/billing", tags=["billing"])
log = logging.getLogger("qa.api.routers.billing")


@router.get("")
@router.get("/subscription")
def get_subscription(
    payload: Annotated[dict, Depends(get_current_payload)],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching subscription for tenant: %s", rid, tenant_id)
    res = svc.get_subscription(db, tenant_id)
    log.info("[%s] Successfully retrieved subscription for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


@router.get("/usage")
def get_usage(
    payload: Annotated[dict, Depends(get_current_payload)],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching usage stats for tenant: %s", rid, tenant_id)
    res = svc.get_usage(db, tenant_id)
    log.info("[%s] Successfully retrieved usage stats for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


@router.post("/checkout-session")
@router.post("/stripe/checkout")
def create_checkout(
    body: CreateCheckoutSessionRequest,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Creating checkout session for tenant: %s, plan: %s", rid, tenant_id, body.plan)
    res = svc.create_checkout_session(
        db,
        tenant_id,
        plan=body.plan,
        success_url=body.successUrl,
        cancel_url=body.cancelUrl,
    )
    log.info("[%s] Successfully created checkout session for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


@router.post("/portal-session")
@router.post("/stripe/portal-session")
def create_portal(
    body: CreatePortalSessionRequest,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Creating portal session for tenant: %s", rid, tenant_id)
    res = svc.create_portal_session(db, tenant_id, body.returnUrl)
    log.info("[%s] Successfully created portal session for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


@router.post("/change-plan")
@router.post("/stripe/change-plan")
def change_plan(
    body: ChangePlanRequest,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Changing plan for tenant: %s to %s", rid, tenant_id, body.plan)
    res = svc.change_plan(
        db,
        tenant_id,
        plan=body.plan,
        proration_behavior=body.prorationBehavior,
    )
    log.info("[%s] Successfully changed plan for tenant: %s to %s", rid, tenant_id, body.plan)
    return build_response(res, rid)


@router.post("/cancel")
@router.post("/stripe/cancel")
def cancel(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Requesting subscription cancellation for tenant: %s", rid, tenant_id)
    res = svc.cancel_subscription(db, tenant_id)
    log.info("[%s] Successfully requested subscription cancellation for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


@router.post("/resume")
@router.post("/stripe/resume")
def resume(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Requesting subscription resumption for tenant: %s", rid, tenant_id)
    res = svc.resume_subscription(db, tenant_id)
    log.info("[%s] Successfully requested subscription resumption for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


# Public endpoint — Stripe-signed webhook delivery.
@router.post("/stripe/webhook")
async def stripe_webhook(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
    stripe_signature: Annotated[str | None, Header(alias="Stripe-Signature")] = None,
):
    log.info("[%s] Received Stripe webhook request", rid)
    raw = await request.body()
    res = svc.handle_stripe_webhook(db, stripe_signature, raw)
    log.info("[%s] Successfully processed Stripe webhook response: %s", rid, res)
    return build_response(res, rid)
