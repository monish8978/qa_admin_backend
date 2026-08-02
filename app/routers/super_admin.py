from __future__ import annotations

import logging
import time
from typing import Annotated, Any, Literal
from pydantic import BaseModel

from fastapi import APIRouter, Depends, status
from sqlalchemy import select, func as sa_func, text
from sqlalchemy.orm import Session

from ..common.responses import build_response
from ..common.exceptions import forbidden, unauthorized, not_found, bad_request
from ..deps import get_db, get_current_payload, get_request_id
from ..models.master import Tenant, User

router = APIRouter(prefix="/super-admin", tags=["Super Admin"])
log = logging.getLogger("qa.api.routers.super_admin")


from ..config import get_settings

def require_super_admin(
    payload: Annotated[dict, Depends(get_current_payload)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    user = db.get(User, payload.get("sub"))
    if not user:
        raise unauthorized("USER_NOT_FOUND", "User not found")
    
    settings = get_settings()
    allowed_emails = [e.strip().lower() for e in settings.SUPER_ADMIN_EMAILS.split(",") if e.strip()]
    is_sa = (
        user.role == "ADMIN" and user.email.lower() in allowed_emails
    )
    if not is_sa:
        raise forbidden("INSUFFICIENT_ROLE", "This action requires Super Admin privileges")
    return payload


class UpdateStatusRequest(BaseModel):
    status: Literal["PROVISIONING", "ACTIVE", "SUSPENDED", "CANCELLED"]


class UpdatePlanRequest(BaseModel):
    plan: Literal["BASIC", "PRO", "ENTERPRISE"]


class UpdateUserStatusRequest(BaseModel):
    status: Literal["ACTIVE", "INACTIVE", "INVITED"]


class UpdateCustomLimitsRequest(BaseModel):
    customConversationsLimit: int | None = None
    customFormsLimit: int | None = None
    customUsersLimit: int | None = None


class CreateTenantRequest(BaseModel):
    name: str
    slug: str
    plan: Literal["BASIC", "PRO", "ENTERPRISE"] = "BASIC"
    dbHost: str = ""
    dbPort: int = 5432
    dbName: str = ""
    dbUser: str = ""
    dbPasswordEnc: str = ""


class UpdateFeatureFlagsRequest(BaseModel):
    featureFlags: dict[str, Any]


@router.get("/tenants")
def list_tenants(
    db: Annotated[Session, Depends(get_db)],
    _sa: Annotated[dict, Depends(require_super_admin)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenants = db.scalars(select(Tenant).order_by(Tenant.createdAt.desc())).all()
    return build_response(
        [
            {
                "id": t.id,
                "slug": t.slug,
                "name": t.name,
                "plan": t.plan,
                "status": t.status,
                "customConversationsLimit": t.customConversationsLimit,
                "customFormsLimit": t.customFormsLimit,
                "customUsersLimit": t.customUsersLimit,
                "featureFlags": t.featureFlags or {},
                "createdAt": t.createdAt.isoformat() if t.createdAt else None,
            }
            for t in tenants
        ],
        rid
    )


@router.post("/tenants", status_code=status.HTTP_201_CREATED)
def create_tenant(
    body: CreateTenantRequest,
    db: Annotated[Session, Depends(get_db)],
    _sa: Annotated[dict, Depends(require_super_admin)],
    rid: Annotated[str, Depends(get_request_id)],
):
    """Create a new tenant workspace."""
    # Check slug uniqueness
    existing = db.scalars(select(Tenant).where(Tenant.slug == body.slug)).first()
    if existing:
        raise bad_request("SLUG_TAKEN", f"Slug '{body.slug}' is already taken")

    tenant = Tenant(
        name=body.name,
        slug=body.slug,
        plan=body.plan,
        status="PROVISIONING",
        dbHost=body.dbHost,
        dbPort=body.dbPort,
        dbName=body.dbName,
        dbUser=body.dbUser,
        dbPasswordEnc=body.dbPasswordEnc,
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return build_response(
        {
            "id": tenant.id,
            "slug": tenant.slug,
            "name": tenant.name,
            "plan": tenant.plan,
            "status": tenant.status,
            "createdAt": tenant.createdAt.isoformat() if tenant.createdAt else None,
        },
        rid,
    )


@router.put("/tenants/{tenant_id}/custom-limits")
def update_tenant_custom_limits(
    tenant_id: str,
    body: UpdateCustomLimitsRequest,
    db: Annotated[Session, Depends(get_db)],
    _sa: Annotated[dict, Depends(require_super_admin)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise not_found("TENANT_NOT_FOUND", "Tenant not found")
    
    tenant.customConversationsLimit = body.customConversationsLimit
    tenant.customFormsLimit = body.customFormsLimit
    tenant.customUsersLimit = body.customUsersLimit
    db.commit()
    return build_response({
        "success": True,
        "customConversationsLimit": tenant.customConversationsLimit,
        "customFormsLimit": tenant.customFormsLimit,
        "customUsersLimit": tenant.customUsersLimit
    }, rid)


@router.put("/tenants/{tenant_id}/status")
def update_tenant_status(
    tenant_id: str,
    body: UpdateStatusRequest,
    db: Annotated[Session, Depends(get_db)],
    _sa: Annotated[dict, Depends(require_super_admin)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise not_found("TENANT_NOT_FOUND", "Tenant not found")
    
    tenant.status = body.status
    db.commit()
    return build_response({"success": True, "status": tenant.status}, rid)


@router.put("/tenants/{tenant_id}/plan")
def update_tenant_plan(
    tenant_id: str,
    body: UpdatePlanRequest,
    db: Annotated[Session, Depends(get_db)],
    _sa: Annotated[dict, Depends(require_super_admin)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise not_found("TENANT_NOT_FOUND", "Tenant not found")
    
    tenant.plan = body.plan
    db.commit()
    return build_response({"success": True, "plan": tenant.plan}, rid)


@router.put("/tenants/{tenant_id}/feature-flags")
def update_feature_flags(
    tenant_id: str,
    body: UpdateFeatureFlagsRequest,
    db: Annotated[Session, Depends(get_db)],
    _sa: Annotated[dict, Depends(require_super_admin)],
    rid: Annotated[str, Depends(get_request_id)],
):
    """Update per-tenant feature flags (stored in featureFlags JSON column)."""
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise not_found("TENANT_NOT_FOUND", "Tenant not found")

    # Merge with existing flags so unknown flags are preserved
    current: dict = getattr(tenant, "featureFlags", None) or {}
    merged = {**current, **body.featureFlags}

    # Use text update to bypass potential ORM JSONB issues
    db.execute(
        text('UPDATE tenants SET "featureFlags" = :flags WHERE id = :tid'),
        {"flags": __import__("json").dumps(merged), "tid": tenant_id},
    )
    db.commit()
    return build_response({"success": True, "featureFlags": merged}, rid)


@router.post("/tenants/{tenant_id}/approve")
def approve_tenant(
    tenant_id: str,
    db: Annotated[Session, Depends(get_db)],
    _sa: Annotated[dict, Depends(require_super_admin)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise not_found("TENANT_NOT_FOUND", "Tenant not found")
    
    tenant.status = "ACTIVE"
    admin_users = db.scalars(
        select(User).where(User.tenantId == tenant_id, User.role == "ADMIN")
    ).all()
    for user in admin_users:
        user.status = "ACTIVE"
    
    db.commit()
    return build_response({"success": True}, rid)


@router.get("/users")
def list_users(
    db: Annotated[Session, Depends(get_db)],
    _sa: Annotated[dict, Depends(require_super_admin)],
    rid: Annotated[str, Depends(get_request_id)],
):
    stmt = select(User).join(User.tenant).order_by(User.createdAt.desc())
    users = db.scalars(stmt).all()
    return build_response(
        [
            {
                "id": u.id,
                "name": u.name,
                "email": u.email,
                "role": u.role,
                "status": u.status,
                "tenantId": u.tenantId,
                "tenantSlug": u.tenant.slug,
                "tenantName": u.tenant.name,
                "lastLoginAt": u.lastLoginAt.isoformat() if u.lastLoginAt else None,
                "createdAt": u.createdAt.isoformat() if u.createdAt else None,
            }
            for u in users
        ],
        rid
    )


@router.put("/users/{user_id}/status")
def update_user_status(
    user_id: str,
    body: UpdateUserStatusRequest,
    db: Annotated[Session, Depends(get_db)],
    _sa: Annotated[dict, Depends(require_super_admin)],
    rid: Annotated[str, Depends(get_request_id)],
):
    user = db.get(User, user_id)
    if not user:
        raise not_found("USER_NOT_FOUND", "User not found")
    
    user.status = body.status
    db.commit()
    return build_response({"success": True, "status": user.status}, rid)


@router.get("/system-health")
def system_health(
    db: Annotated[Session, Depends(get_db)],
    _sa: Annotated[dict, Depends(require_super_admin)],
    rid: Annotated[str, Depends(get_request_id)],
):
    """Returns global system health stats visible only to super admins."""
    from ..redis_client import get_redis

    checks: dict[str, Any] = {}

    # DB check
    t0 = time.monotonic()
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = {"status": "ok", "latencyMs": round((time.monotonic() - t0) * 1000, 1)}
    except Exception:
        checks["database"] = {"status": "error", "latencyMs": None}

    # Redis check
    redis = get_redis()
    if redis is None:
        checks["redis"] = {"status": "disabled", "latencyMs": None}
    else:
        t0 = time.monotonic()
        try:
            redis.ping()
            checks["redis"] = {"status": "ok", "latencyMs": round((time.monotonic() - t0) * 1000, 1)}
        except Exception:
            checks["redis"] = {"status": "error", "latencyMs": None}

    # Global stats
    total_tenants = db.scalar(select(sa_func.count(Tenant.id))) or 0
    active_tenants = db.scalar(
        select(sa_func.count(Tenant.id)).where(Tenant.status == "ACTIVE")
    ) or 0
    total_users = db.scalar(select(sa_func.count(User.id))) or 0
    active_users = db.scalar(
        select(sa_func.count(User.id)).where(User.status == "ACTIVE")
    ) or 0

    # Per-tenant summary (name, plan, status, user_count)
    tenant_rows = db.execute(
        select(
            Tenant.id,
            Tenant.name,
            Tenant.slug,
            Tenant.plan,
            Tenant.status,
            Tenant.createdAt,
            sa_func.count(User.id).label("userCount"),
        )
        .outerjoin(User, User.tenantId == Tenant.id)
        .group_by(Tenant.id)
        .order_by(Tenant.createdAt.desc())
    ).all()

    tenants_summary = [
        {
            "id": row.id,
            "name": row.name,
            "slug": row.slug,
            "plan": row.plan,
            "status": row.status,
            "userCount": row.userCount,
            "createdAt": row.createdAt.isoformat() if row.createdAt else None,
        }
        for row in tenant_rows
    ]

    overall_status = "ok" if all(
        v["status"] in ("ok", "disabled") for v in checks.values()
    ) else "degraded"

    return build_response(
        {
            "status": overall_status,
            "checks": checks,
            "stats": {
                "totalTenants": total_tenants,
                "activeTenants": active_tenants,
                "totalUsers": total_users,
                "activeUsers": active_users,
            },
            "tenants": tenants_summary,
        },
        rid,
    )

