"""Outbound webhook management — /api/v1/outbound-webhooks."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from ..common.enums import UserRole
from ..common.responses import build_response
from ..deps import get_db, get_request_id, require_roles
from ..schemas.billing import CreateOutboundWebhookRequest, UpdateWebhookStatusRequest
from ..services import outbound_webhooks_service as svc

router = APIRouter(prefix="/outbound-webhooks", tags=["outbound-webhooks"])
log = logging.getLogger("qa.api.routers.outbound_webhooks")


@router.post("")
def create(
    body: CreateOutboundWebhookRequest,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Creating outbound webhook for tenant: %s (url=%s, events=%s)", rid, tenant_id, body.url, body.events)
    res = svc.create(db, tenant_id, url=str(body.url), events=body.events)
    log.info("[%s] Successfully created outbound webhook for tenant: %s (id=%s)", rid, tenant_id, res.get("id"))
    return build_response(res, rid)


@router.get("")
def list_hooks(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Listing outbound webhooks for tenant: %s", rid, tenant_id)
    res = svc.list_hooks(db, tenant_id)
    log.info("[%s] Successfully retrieved outbound webhooks for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


@router.get("/deliveries")
def list_deliveries(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    webhookId: str | None = None,
    status: str | None = None,
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Listing outbound webhook deliveries for tenant: %s (page=%d, limit=%d, webhookId=%s, status=%s)", rid, tenant_id, page, limit, webhookId, status)
    res = svc.list_deliveries(
        db,
        tenant_id,
        page=page,
        limit=limit,
        webhook_id=webhookId,
        status=status,
    )
    log.info("[%s] Successfully retrieved webhook deliveries for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


@router.patch("/{hook_id}/status")
def update_status(
    hook_id: str,
    body: UpdateWebhookStatusRequest,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Updating status of outbound webhook %s for tenant: %s to %s", rid, hook_id, tenant_id, body.status)
    res = svc.update_status(db, tenant_id, hook_id, body.status)
    log.info("[%s] Successfully updated status of outbound webhook %s for tenant: %s to %s", rid, hook_id, tenant_id, body.status)
    return build_response(res, rid)


@router.post("/{hook_id}/rotate-secret")
def rotate_secret(
    hook_id: str,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Rotating secret of outbound webhook %s for tenant: %s", rid, hook_id, tenant_id)
    res = svc.rotate_secret(db, tenant_id, hook_id)
    log.info("[%s] Successfully rotated secret of outbound webhook %s for tenant: %s", rid, hook_id, tenant_id)
    return build_response(res, rid)


@router.delete("/{hook_id}", status_code=204)
def remove(
    hook_id: str,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
):
    # Delete requests do not standardly have request_id unless they are wrapped, but we can log using tenant_id
    tenant_id = payload["tenantId"]
    log.info("Removing outbound webhook %s for tenant: %s", hook_id, tenant_id)
    svc.remove(db, tenant_id, hook_id)
    log.info("Successfully removed outbound webhook %s for tenant: %s", hook_id, tenant_id)
    return Response(status_code=204)


@router.post("/deliveries/{delivery_id}/retry")
def retry_delivery(
    delivery_id: str,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Requesting retry for webhook delivery: %s for tenant: %s", rid, delivery_id, tenant_id)
    res = svc.retry_delivery(db, tenant_id, delivery_id)
    log.info("[%s] Successfully enqueued retry for webhook delivery: %s for tenant: %s", rid, delivery_id, tenant_id)
    return build_response(res, rid)
