"""Routing endpoints — /api/v1/routing. ADMIN gated."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from ..common.enums import UserRole
from ..common.responses import build_response
from ..deps import get_db, get_request_id, require_roles
from ..schemas.conversations import (
    CreateAppDepartmentMappingRequest,
    RoutingPreviewRequest,
    UpdateAppDepartmentMappingRequest,
    UpsertRoutingSettingsRequest,
)
from ..services import routing_service as svc
from ..services.department_routing import (
    get_active_departments,
    resolve_conversation_department,
)

router = APIRouter(prefix="/routing", tags=["routing"])
log = logging.getLogger("qa.api.routers.routing")


@router.get("/settings")
def get_settings(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching routing settings for tenant: %s", rid, tenant_id)
    res = svc.get_settings(db, tenant_id)
    log.info("[%s] Successfully retrieved routing settings for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


@router.patch("/settings")
def patch_settings(
    body: UpsertRoutingSettingsRequest,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Patching routing settings for tenant: %s (sub=%s)", rid, tenant_id, payload["sub"])
    res = svc.upsert_settings(
        db, tenant_id, payload["sub"], body.model_dump(exclude_unset=True)
    )
    log.info("[%s] Successfully patched routing settings for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


@router.get("/mappings")
def list_mappings(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Listing routing mappings for tenant: %s", rid, tenant_id)
    res = svc.list_mappings(db, tenant_id)
    log.info("[%s] Successfully retrieved routing mappings for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


@router.get("/mappings/stats")
def mapping_stats(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching routing mapping coverage stats for tenant: %s", rid, tenant_id)
    res = svc.list_mapping_llm_coverage_stats(db, tenant_id)
    log.info("[%s] Successfully retrieved routing mapping coverage stats for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


@router.post("/mappings")
def create_mapping(
    body: CreateAppDepartmentMappingRequest,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Creating routing mapping for tenant: %s (applicationKey=%s, channel=%s)", rid, tenant_id, body.applicationKey, body.channel)
    res = svc.create_mapping(
        db, tenant_id, payload["sub"], body.model_dump(exclude_unset=True)
    )
    log.info("[%s] Successfully created routing mapping for tenant: %s (id=%s)", rid, tenant_id, res.get("id"))
    return build_response(res, rid)


@router.patch("/mappings/{mapping_id}")
def update_mapping(
    mapping_id: str,
    body: UpdateAppDepartmentMappingRequest,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Updating routing mapping %s for tenant: %s", rid, mapping_id, tenant_id)
    res = svc.update_mapping(
        db, tenant_id, payload["sub"], mapping_id,
        body.model_dump(exclude_unset=True),
    )
    log.info("[%s] Successfully updated routing mapping %s for tenant: %s", rid, mapping_id, tenant_id)
    return build_response(res, rid)


@router.delete("/mappings/{mapping_id}", status_code=204)
def delete_mapping(
    mapping_id: str,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
):
    tenant_id = payload["tenantId"]
    log.info("Deleting routing mapping %s for tenant: %s (actor=%s)", mapping_id, tenant_id, payload["sub"])
    svc.delete_mapping(db, tenant_id, payload["sub"], mapping_id)
    log.info("Successfully deleted routing mapping %s for tenant: %s", mapping_id, tenant_id)
    return Response(status_code=204)


@router.get("/audits")
def list_audits(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    action: str | None = None,
    mappingId: str | None = None,
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Listing routing audits for tenant: %s (page=%d, limit=%d, action=%s, mappingId=%s)", rid, tenant_id, page, limit, action, mappingId)
    res = svc.list_audits(
        db, tenant_id,
        page=page, limit=limit, action=action, mapping_id=mappingId,
    )
    log.info("[%s] Successfully retrieved routing audits for tenant: %s", rid, tenant_id)
    return build_response(res, rid)


@router.post("/preview")
def preview(
    body: RoutingPreviewRequest,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Generating routing preview for tenant: %s (channel=%s, applicationKey=%s)", rid, tenant_id, body.channel, body.applicationKey)
    active_depts = get_active_departments(db, tenant_id)
    legacy = resolve_conversation_department(active_depts, body.channel, body.metadata or {})
    result = svc.preview_route(
        db, tenant_id,
        channel=body.channel,
        application_key=body.applicationKey,
        metadata=body.metadata,
        active_departments=active_depts,
        legacy_department=legacy,
    )
    log.info("[%s] Successfully generated routing preview for tenant: %s", rid, tenant_id)
    return build_response(result, rid)
