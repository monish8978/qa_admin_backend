"""Form definitions — /api/v1/forms (operates on the per-tenant DB)."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..common.enums import UserRole
from ..common.exceptions import not_found
from ..common.responses import build_response
from ..deps import get_current_payload, get_db, get_request_id, require_roles
from ..models.master import Tenant, Department
from ..schemas.settings import (
    CreateFormRequest,
    FormStatusActionRequest,
    UpdateFormRequest,
)
from ..services import forms_service as svc
from ..services.tenant_pool import get_tenant_pool

router = APIRouter(prefix="/forms", tags=["forms"])
log = logging.getLogger("qa.api.routers.forms")


def _get_dept_map(master: Session, tenant_id: str) -> dict[str, str]:
    depts = master.execute(
        select(Department.id, Department.name).where(Department.tenantId == tenant_id)
    ).all()
    return {d.id: d.name for d in depts}


def _serialize(form, dept_map: dict[str, str] | None = None) -> dict:
    dept_id = getattr(form, "departmentId", None)
    dept_obj = None
    if dept_id and dept_map and dept_id in dept_map:
        dept_obj = {"id": dept_id, "name": dept_map[dept_id]}

    return {
        "id": form.id,
        "formKey": form.formKey,
        "version": form.version,
        "departmentId": dept_id,
        "department": dept_obj,
        "name": form.name,
        "description": form.description,
        "status": form.status,
        "channels": form.channels,
        "scoringStrategy": form.scoringStrategy,
        "sections": form.sections,
        "questions": form.questions,
        "metadata": form.fmetadata,
        "publishedAt": form.publishedAt.isoformat() if form.publishedAt else None,
        "deprecatedAt": form.deprecatedAt.isoformat() if form.deprecatedAt else None,
        "archivedAt": form.archivedAt.isoformat() if form.archivedAt else None,
        "createdById": form.createdById,
        "createdAt": form.createdAt.isoformat() if form.createdAt else None,
    }


def _tenant_session(payload: dict):
    pool = get_tenant_pool()
    return pool.session(payload["tenantId"])


@router.get("")
def list_forms(
    payload: Annotated[dict, Depends(get_current_payload)],
    master: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
    status: Annotated[str | None, Query()] = None,
    department_id: Annotated[str | None, Query(alias="departmentId")] = None,
    search: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query()] = 1,
    limit: Annotated[int, Query()] = 20,
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Listing forms for tenant: %s (status=%s, department_id=%s, search=%s, page=%d, limit=%d)", rid, tenant_id, status, department_id, search, page, limit)
    
    page = max(1, page)
    limit = min(max(1, limit), 100)
    offset = (page - 1) * limit

    dept_map = _get_dept_map(master, tenant_id)

    with _tenant_session(payload) as ts:
        rows = svc.list_forms(ts, status=status, department_id=department_id, search=search)
        total = len(rows)
        paginated_rows = rows[offset : offset + limit]
        
        serialized = [_serialize(r, dept_map) for r in paginated_rows]
        res = {
            "items": serialized,
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total,
                "totalPages": (total + limit - 1) // limit if total > 0 else 0,
            }
        }
        log.info("[%s] Successfully listed %d of %d forms for tenant: %s", rid, len(paginated_rows), total, tenant_id)
        return build_response(res, rid)


@router.post("", status_code=201)
def create_form(
    body: CreateFormRequest,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    master: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Creating form for tenant: %s (formKey=%s, name=%s)", rid, tenant_id, body.formKey, body.name)
    tenant = master.execute(
        select(Tenant).where(Tenant.id == tenant_id)
    ).scalar_one()
    with _tenant_session(payload) as ts:
        form = svc.create_form(
            ts,
            plan=tenant.plan,
            custom_forms_limit=tenant.customFormsLimit,
            created_by_id=payload["sub"],
            form_key=body.formKey,
            name=body.name,
            description=body.description,
            channels=body.channels,
            scoring_strategy=body.scoringStrategy,
            sections=body.sections,
            questions=body.questions,
            department_id=body.departmentId,
            metadata=body.metadata,
        )
        dept_map = _get_dept_map(master, tenant_id)
        log.info("[%s] Successfully created form %s (id=%s) for tenant: %s", rid, form.formKey, form.id, tenant_id)
        return build_response(_serialize(form, dept_map), rid)


@router.get("/{form_id}")
def get_form(
    form_id: str,
    payload: Annotated[dict, Depends(get_current_payload)],
    master: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching form details for tenant: %s, form_id: %s", rid, tenant_id, form_id)
    dept_map = _get_dept_map(master, tenant_id)
    with _tenant_session(payload) as ts:
        form = svc.get_form(ts, form_id)
        if form is None:
            log.warning("[%s] Form not found: %s for tenant: %s", rid, form_id, tenant_id)
            raise not_found("FORM_NOT_FOUND", "Form not found")
        log.info("[%s] Successfully retrieved form %s details for tenant: %s", rid, form_id, tenant_id)
        return build_response(_serialize(form, dept_map), rid)


@router.patch("/{form_id}")
def update_form(
    form_id: str,
    body: UpdateFormRequest,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    master: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Updating form %s for tenant: %s", rid, form_id, tenant_id)
    dept_map = _get_dept_map(master, tenant_id)
    with _tenant_session(payload) as ts:
        form = svc.update_form(ts, form_id, body.model_dump(exclude_unset=True))
        log.info("[%s] Successfully updated form %s for tenant: %s", rid, form_id, tenant_id)
        return build_response(_serialize(form, dept_map), rid)


@router.post("/{form_id}/status")
def change_status(
    form_id: str,
    body: FormStatusActionRequest,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    master: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Changing status of form %s for tenant: %s (action=%s)", rid, form_id, tenant_id, body.action)
    dept_map = _get_dept_map(master, tenant_id)
    with _tenant_session(payload) as ts:
        form = svc.change_status(ts, form_id, body.action)
        log.info("[%s] Successfully changed status of form %s for tenant: %s to %s", rid, form_id, tenant_id, form.status)
        return build_response(_serialize(form, dept_map), rid)
