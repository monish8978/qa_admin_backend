"""Conversations endpoints — /api/v1/conversations."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..common.enums import UserRole
from ..common.responses import build_response
from ..deps import get_current_payload, get_db, get_request_id, require_roles
from ..schemas.conversations import UploadConversationsRequest
from ..services import conversations_service as svc

router = APIRouter(prefix="/conversations", tags=["conversations"])
log = logging.getLogger("qa.api.routers.conversations")


@router.get("")
def list_conversations(
    payload: Annotated[dict, Depends(get_current_payload)],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
    status: str | None = Query(None),
    agentId: str | None = Query(None),
    search: str | None = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    tenant_id = payload["tenantId"]
    log.info(
        "[%s] Listing conversations for tenant: %s (status=%s, agentId=%s, search=%s, page=%s)",
        rid,
        tenant_id,
        status,
        agentId,
        search,
        page,
    )
    result = svc.list_conversations(
        db,
        tenant_id,
        {"status": status, "agentId": agentId, "search": search, "page": page, "limit": limit},
        role=payload["role"],
        user_id=payload["sub"],
    )
    log.info("[%s] Successfully retrieved conversations list for tenant: %s", rid, tenant_id)
    return build_response(result, rid)


@router.get("/{conversation_id}")
def get_conversation(
    conversation_id: str,
    payload: Annotated[dict, Depends(get_current_payload)],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching conversation details for ID: %s, tenant: %s", rid, conversation_id, tenant_id)
    result = svc.get_conversation(
        tenant_id,
        conversation_id,
        master=db,
        actor_role=payload.get("role"),
        actor_id=payload.get("sub"),
    )
    log.info("[%s] Successfully retrieved conversation details for ID: %s", rid, conversation_id)
    return build_response(result, rid)


@router.post("/upload", status_code=201)
def upload(
    body: UploadConversationsRequest,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info(
        "[%s] Uploading %d conversations for tenant: %s (channel=%s)",
        rid,
        len(body.conversations),
        tenant_id,
        body.channel,
    )
    result = svc.upload_conversations(
        db,
        tenant_id,
        channel=body.channel,
        conversations=[c.model_dump() for c in body.conversations],
    )
    log.info("[%s] Successfully completed conversation upload for tenant: %s", rid, tenant_id)
    return build_response(result, rid)


@router.post("/backfill-pending")
def backfill_pending(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Backfilling pending evaluations for tenant: %s", rid, tenant_id)
    result = svc.backfill_pending_evaluations(db, tenant_id)
    log.info("[%s] Successfully backfilled pending evaluations for tenant: %s", rid, tenant_id)
    return build_response(result, rid)


@router.post("/remap-corrupted-qa-pending")
def remap_corrupted(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Remapping corrupted QA pending evaluations for tenant: %s", rid, tenant_id)
    result = svc.remap_corrupted_qa_pending(tenant_id)
    log.info("[%s] Successfully remapped corrupted QA pending evaluations for tenant: %s", rid, tenant_id)
    return build_response(result, rid)

