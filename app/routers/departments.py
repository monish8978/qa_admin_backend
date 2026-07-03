"""Departments REST endpoints — /api/v1/departments."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..common.enums import UserRole
from ..common.responses import build_response
from ..deps import get_current_payload, get_db, get_request_id, require_roles
from ..schemas.settings import (
    CreateDepartmentRequest,
    DepartmentResponse,
    UpdateDepartmentRequest,
)
from ..services import departments_service as svc

router = APIRouter(prefix="/departments", tags=["departments"])
log = logging.getLogger("qa.api.routers.departments")


def _serialize(dept) -> DepartmentResponse:
    raw_channels = dept.channels or {}
    if isinstance(raw_channels, list):
        raw_channels = {str(ch): True for ch in raw_channels}
    return DepartmentResponse(
        id=dept.id,
        name=dept.name,
        slug=dept.slug,
        description=dept.description,
        channels=raw_channels,
        autoAssignEnabled=dept.autoAssignEnabled,
        isActive=dept.isActive,
        createdAt=dept.createdAt,
        updatedAt=dept.updatedAt,
    )


@router.get("")
def list_departments(
    payload: Annotated[dict, Depends(get_current_payload)],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Listing departments for tenant: %s", rid, tenant_id)
    rows = svc.list_departments(db, tenant_id)
    log.info("[%s] Successfully retrieved %d departments for tenant: %s", rid, len(rows), tenant_id)
    return build_response([_serialize(r).model_dump() for r in rows], rid)


@router.post("", status_code=201)
def create_department(
    body: CreateDepartmentRequest,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Creating department for tenant: %s (name=%s, slug=%s)", rid, tenant_id, body.name, body.slug)
    dept = svc.create_department(
        db, tenant_id,
        name=body.name,
        slug=body.slug,
        description=body.description,
        channels=body.channels,
        auto_assign_enabled=body.autoAssignEnabled,
        is_active=body.isActive,
    )
    log.info("[%s] Successfully created department ID: %s for tenant: %s", rid, dept.id, tenant_id)
    return build_response(_serialize(dept).model_dump(), rid)


@router.get("/{department_id}")
def get_department(
    department_id: str,
    payload: Annotated[dict, Depends(get_current_payload)],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching department: %s for tenant: %s", rid, department_id, tenant_id)
    dept = svc.get_department(db, tenant_id, department_id)
    log.info("[%s] Successfully retrieved details for department: %s", rid, department_id)
    return build_response(_serialize(dept).model_dump(), rid)


@router.patch("/{department_id}")
def update_department(
    department_id: str,
    body: UpdateDepartmentRequest,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Updating department: %s for tenant: %s", rid, department_id, tenant_id)
    patch = body.model_dump(exclude_unset=True)
    dept = svc.update_department(db, tenant_id, department_id, patch=patch)
    log.info("[%s] Successfully updated department: %s", rid, department_id)
    return build_response(_serialize(dept).model_dump(), rid)


@router.delete("/{department_id}", status_code=204)
def delete_department(
    department_id: str,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Deleting department: %s for tenant: %s", rid, department_id, tenant_id)
    svc.delete_department(db, tenant_id, department_id)
    log.info("[%s] Successfully deleted department: %s", rid, department_id)


@router.get("/{department_id}/users")
def list_users(
    department_id: str,
    payload: Annotated[dict, Depends(get_current_payload)],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Listing users for department: %s (tenant=%s)", rid, department_id, tenant_id)
    users = svc.list_department_users(db, tenant_id, department_id)
    log.info("[%s] Successfully retrieved %d department users", rid, len(users))
    return build_response(
        [
            {"id": u.id, "email": u.email, "name": u.name, "role": u.role, "status": u.status}
            for u in users
        ],
        rid,
    )

