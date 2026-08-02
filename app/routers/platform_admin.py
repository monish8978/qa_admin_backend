from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..common.enums import UserRole
from ..common.responses import build_response
from ..deps import get_db, get_request_id, require_roles
from ..models.master import Tenant, User, UsageMetric, PlatformPlan, PlatformAuditLog, PlatformNotification
from ..services.auth_service import AuthService
from ..common.enums import PLAN_LIMITS
from datetime import datetime, timezone, timedelta

router = APIRouter(prefix="/platform-admin", tags=["Platform Admin"])
log = logging.getLogger("qa.api.routers.platform_admin")


class UpdatePlanRequest(BaseModel):
    plan: str
    customConversationsLimit: int | None = None
    customUsersLimit: int | None = None
    customFormsLimit: int | None = None


class UpdateStatusRequest(BaseModel):
    status: str


@router.get("/tenants")
def list_tenants(
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    show_deleted: bool = False,
):
    log.info("[%s] Listing all tenants for platform admin", request_id)
    from sqlalchemy.orm import selectinload
    query = select(Tenant).options(selectinload(Tenant.users))
    if show_deleted:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=30)
        query = query.where(Tenant.deletedAt.isnot(None), Tenant.deletedAt > cutoff)
    else:
        query = query.where(Tenant.deletedAt.is_(None))

    tenants = db.scalars(query.order_by(Tenant.createdAt.desc())).all()

    result = []
    for t in tenants:
        admin_user = next((u for u in t.users if u.role == "ADMIN"), None)
        admin_email = admin_user.email if admin_user else "No Admin"
        result.append({
            "id": t.id,
            "slug": t.slug,
            "name": t.name,
            "plan": t.plan if isinstance(t.plan, str) else t.plan.value,
            "pendingPlan": t.pendingPlan if isinstance(t.pendingPlan, str) else (t.pendingPlan.value if t.pendingPlan else None),
            "status": t.status,
            "dbHost": t.dbHost,
            "dbName": t.dbName,
            "createdAt": t.createdAt.isoformat() if t.createdAt else None,
            "deletedAt": t.deletedAt.isoformat() if t.deletedAt else None,
            "adminEmail": admin_email,
            "customConversationsLimit": t.customConversationsLimit,
            "customUsersLimit": t.customUsersLimit,
            "customFormsLimit": t.customFormsLimit,
        })
    return build_response(result, request_id)


@router.delete("/tenants/{tenant_id}")
def delete_tenant(
    tenant_id: str,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
):
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    now = datetime.now(tz=timezone.utc)
    tenant.deletedAt = now

    # Cascade soft delete to all users of this tenant
    from sqlalchemy import update
    db.execute(
        update(User).where(User.tenantId == tenant_id).values(deletedAt=now)
    )

    # Revoke all active sessions for those users
    from ..models.master import RefreshToken
    user_ids = db.scalars(select(User.id).where(User.tenantId == tenant_id)).all()
    if user_ids:
        for t in db.scalars(
            select(RefreshToken).where(
                RefreshToken.userId.in_(user_ids), RefreshToken.revokedAt.is_(None)
            )
        ):
            t.revokedAt = now

    db.commit()
    return build_response({"success": True}, request_id)


@router.post("/tenants/{tenant_id}/restore")
def restore_tenant(
    tenant_id: str,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
):
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    tenant.deletedAt = None

    # Cascade restore to all users of this tenant
    from sqlalchemy import update
    db.execute(
        update(User).where(User.tenantId == tenant_id).values(deletedAt=None)
    )

    db.commit()
    return build_response({"success": True}, request_id)


@router.patch("/tenants/{tenant_id}/plan")
def update_tenant_plan(
    tenant_id: str,
    body: UpdatePlanRequest,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
):
    log.info(
        "[%s] Updating plan of tenant %s to %s (limits: conv=%s, users=%s, forms=%s)",
        request_id, tenant_id, body.plan,
        body.customConversationsLimit, body.customUsersLimit, body.customFormsLimit,
    )
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    old_plan = tenant.plan if isinstance(tenant.plan, str) else tenant.plan.value
    tenant.plan = body.plan  # type: ignore[assignment]
    tenant.pendingPlan = None
    tenant.customConversationsLimit = body.customConversationsLimit
    tenant.customUsersLimit = body.customUsersLimit
    tenant.customFormsLimit = body.customFormsLimit

    # Audit Log
    audit = PlatformAuditLog(
        user_id=payload.get("sub"),
        user_email=payload.get("email", "System"),
        action="plan.updated",
        resource_type="tenant",
        resource_id=tenant_id,
        details={"tenantName": tenant.name, "tenantSlug": tenant.slug, "oldPlan": old_plan, "newPlan": body.plan},
    )
    db.add(audit)
    db.commit()
    db.refresh(tenant)

    plan_val = tenant.plan if isinstance(tenant.plan, str) else tenant.plan.value
    return build_response(
        {
            "id": tenant.id,
            "slug": tenant.slug,
            "name": tenant.name,
            "plan": plan_val,
            "pendingPlan": None,
            "status": tenant.status,
            "customConversationsLimit": tenant.customConversationsLimit,
            "customUsersLimit": tenant.customUsersLimit,
            "customFormsLimit": tenant.customFormsLimit,
        },
        request_id,
    )

@router.patch("/tenants/{tenant_id}/status")
def update_tenant_status(
    tenant_id: str,
    body: UpdateStatusRequest,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
):
    log.info("[%s] Updating status of tenant %s to %s", request_id, tenant_id, body.status)
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    old_status = tenant.status
    tenant.status = body.status

    # Audit Log
    audit = PlatformAuditLog(
        user_id=payload.get("sub"),
        user_email=payload.get("email", "System"),
        action="tenant.status.updated",
        resource_type="tenant",
        resource_id=tenant_id,
        details={"tenantName": tenant.name, "tenantSlug": tenant.slug, "oldStatus": old_status, "newStatus": body.status},
    )
    db.add(audit)
    db.commit()
    db.refresh(tenant)

    return build_response(
        {
            "id": tenant.id,
            "slug": tenant.slug,
            "name": tenant.name,
            "plan": tenant.plan,
            "status": tenant.status,
        },
        request_id,
    )


@router.post("/tenants/{tenant_id}/impersonate")
def impersonate_tenant(
    tenant_id: str,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
):
    log.info("[%s] Super Admin %s impersonating tenant %s", request_id, payload.get("sub"), tenant_id)
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    # Find the first admin user in this tenant to impersonate
    target_user = db.scalars(
        select(User).where(User.tenantId == tenant_id, User.role == "ADMIN").order_by(User.createdAt.asc())
    ).first()

    if not target_user:
        # Fallback to any user if no admin found
        target_user = db.scalars(
            select(User).where(User.tenantId == tenant_id).order_by(User.createdAt.asc())
        ).first()

    if not target_user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tenant has no users to impersonate")

    # Issue tokens
    auth_service = AuthService(db)
    access, refresh = auth_service._issue_and_store(target_user)

    # Audit Log
    audit = PlatformAuditLog(
        user_id=payload.get("sub"),
        user_email=payload.get("email", "System"),
        action="tenant.impersonated",
        resource_type="tenant",
        resource_id=tenant_id,
        details={"tenantName": tenant.name, "tenantSlug": tenant.slug, "impersonatedUser": target_user.email, "impersonatedRole": target_user.role},
    )
    db.add(audit)
    db.commit()

    return build_response({
        "accessToken": access,
        "refreshToken": refresh,
        "user": {
            "id": target_user.id,
            "name": target_user.name,
            "email": target_user.email,
            "role": target_user.role,
        },
    }, request_id)


@router.get("/users")
def list_users(
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    show_deleted: bool = False,
):
    from sqlalchemy.orm import selectinload
    
    # Pre-calculate user count per tenant
    user_counts = dict(
        db.execute(
            select(User.tenantId, func.count(User.id)).group_by(User.tenantId)
        ).all()
    )

    query = select(User).options(selectinload(User.tenant))
    if show_deleted:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=30)
        query = query.where(User.deletedAt.isnot(None), User.deletedAt > cutoff)
    else:
        query = query.where(User.deletedAt.is_(None))

    users = db.scalars(query.order_by(User.createdAt.desc())).all()

    result = [
        {
            "id": u.id,
            "email": u.email,
            "name": u.name,
            "role": u.role,
            "status": u.status,
            "tenantId": u.tenantId,
            "tenantName": u.tenant.name if u.tenant else "System",
            "tenantSlug": u.tenant.slug if u.tenant else "system",
            "tenantUserCount": user_counts.get(u.tenantId, 0),
            "createdAt": u.createdAt.isoformat() if u.createdAt else None,
            "lastLoginAt": u.lastLoginAt.isoformat() if u.lastLoginAt else None,
            "deletedAt": u.deletedAt.isoformat() if u.deletedAt else None,
        }
        for u in users
    ]
    return build_response(result, request_id)


@router.delete("/users/{user_id}")
def delete_user(
    user_id: str,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.deletedAt = datetime.now(tz=timezone.utc)
    # Revoke all active refresh tokens for immediate logout
    now = datetime.now(tz=timezone.utc)
    from ..models.master import RefreshToken
    for t in db.scalars(
        select(RefreshToken).where(
            RefreshToken.userId == user.id, RefreshToken.revokedAt.is_(None)
        )
    ):
        t.revokedAt = now
    db.commit()
    return build_response({"success": True}, request_id)


@router.post("/users/{user_id}/restore")
def restore_user(
    user_id: str,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.deletedAt = None
    db.commit()
    return build_response({"success": True}, request_id)

@router.get("/notifications")
def get_notifications(
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
):
    log.info("[%s] Fetching super admin notifications", request_id)
    notifications = []
    now = datetime.now(timezone.utc)
    
    # 1. Plan Upgrade Requests
    pending_tenants = db.scalars(select(Tenant).where(Tenant.pendingPlan.is_not(None))).all()
    for t in pending_tenants:
        notifications.append({
            "id": f"upgrade-{t.id}",
            "type": "UPGRADE_REQUEST",
            "title": "Plan Upgrade Requested",
            "message": f"Workspace '{t.name}' (slug: {t.slug}) requested an upgrade to {t.pendingPlan if isinstance(t.pendingPlan, str) else t.pendingPlan.value}.",
            "tenantId": t.id,
            "createdAt": t.updatedAt.isoformat() if t.updatedAt else now.isoformat(),
            "read": False,
        })
    
    # 2. Usage Alerts (90% limit reached)
    from dateutil.relativedelta import relativedelta
    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end_of_month = start_of_month + relativedelta(months=1)
    
    usage_records = db.execute(
        select(UsageMetric, Tenant)
        .join(Tenant)
        .where(
            UsageMetric.periodStart >= start_of_month,
            UsageMetric.periodStart < end_of_month
        )
    ).all()
    
    for usage, tenant in usage_records:
        limit = tenant.customConversationsLimit
        if limit is None:
            plan_val = tenant.plan if not isinstance(tenant.plan, str) else tenant.plan
            # Handle string enum vs Enum object safely
            try:
                import enum
                if isinstance(plan_val, enum.Enum):
                    plan_val = plan_val.value
                plan_limits = PLAN_LIMITS.get(plan_val)
                # For string based plan lookup
                if not plan_limits:
                    from ..common.enums import PlanType
                    plan_limits = PLAN_LIMITS.get(PlanType(plan_val))
                limit = plan_limits.get("conversationsPerMonth") if plan_limits else None
            except Exception:
                limit = None
                
        if limit and limit > 0:
            percentage = (usage.conversationsProcessed / limit) * 100
            if percentage >= 90:
                notifications.append({
                    "id": f"usage-{tenant.id}",
                    "type": "USAGE_LIMIT",
                    "title": "High Usage Alert",
                    "message": f"{tenant.name} has reached {percentage:.1f}% of their conversation limit ({usage.conversationsProcessed}/{limit}).",
                    "tenantId": tenant.id,
                    "createdAt": usage.updatedAt.isoformat() if usage.updatedAt else now.isoformat(),
                    "read": False,
                })
                
    # 3. Super Admin General Notifications (from PlatformNotification)
    platform_notifs = db.scalars(
        select(PlatformNotification)
        .where(PlatformNotification.target_audience == 'super_admin')
        .order_by(PlatformNotification.created_at.desc())
        .limit(20)
    ).all()
    
    for n in platform_notifs:
        notifications.append({
            "id": n.id,
            "type": n.type.upper() if n.type else "INFO",
            "title": n.title,
            "message": n.message,
            "tenantId": None,
            "createdAt": n.created_at.isoformat() if n.created_at else now.isoformat(),
            "read": False,
        })
                
    # Sort notifications by newest first
    notifications.sort(key=lambda x: x["createdAt"], reverse=True)
    
    return build_response(notifications, request_id)

class CreatePlanRequest(BaseModel):
    code: str
    name: str
    description: str | None = None
    priceMonthly: int
    priceYearly: int
    conversationsLimit: int | None = None
    formsLimit: int | None = None
    usersLimit: int | None = None
    features: list[str] = []
    isActive: bool = True

class UpdatePlanMetadataRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    priceMonthly: int | None = None
    priceYearly: int | None = None
    conversationsLimit: int | None = None
    formsLimit: int | None = None
    usersLimit: int | None = None
    features: list[str] | None = None
    isActive: bool | None = None

@router.get("/plans")
def list_plans(
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
):
    """List all platform plans. Accessible to authenticated users for billing, but CRUD needs ADMIN."""
    log.info("[%s] Listing all platform plans", request_id)
    try:
        plans = db.scalars(select(PlatformPlan).order_by(PlatformPlan.priceMonthly.asc())).all()
        result = []
        for p in plans:
            result.append({
                "id": p.id,
                "code": p.code,
                "name": p.name,
                "description": p.description,
                "priceMonthly": p.priceMonthly,
                "priceYearly": p.priceYearly,
                "conversationsLimit": p.conversationsLimit,
                "formsLimit": p.formsLimit,
                "usersLimit": p.usersLimit,
                "features": p.features if p.features else [],
                "isActive": p.isActive,
                "createdAt": p.createdAt.isoformat() if p.createdAt else None,
            })
        return build_response(result, request_id)
    except Exception as e:
        # Fallback to hardcoded if table doesn't exist yet
        log.error("[%s] Failed to fetch plans: %s", request_id, e)
        return build_response([], request_id)

@router.post("/plans")
def create_plan(
    body: CreatePlanRequest,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
):
    log.info("[%s] Creating new plan: %s", request_id, body.code)
    existing = db.scalars(select(PlatformPlan).where(PlatformPlan.code == body.code)).first()
    if existing:
        raise HTTPException(status_code=400, detail="Plan code already exists")
    
    plan = PlatformPlan(
        code=body.code,
        name=body.name,
        description=body.description,
        priceMonthly=body.priceMonthly,
        priceYearly=body.priceYearly,
        conversationsLimit=body.conversationsLimit,
        formsLimit=body.formsLimit,
        usersLimit=body.usersLimit,
        features=body.features,
        isActive=body.isActive,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    
    return build_response({
        "id": plan.id,
        "code": plan.code,
        "name": plan.name,
    }, request_id)

@router.delete("/plans/{plan_id}")
def delete_plan(
    plan_id: str,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
):
    log.info("[%s] Deleting plan: %s", request_id, plan_id)
    plan = db.get(PlatformPlan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
        
    db.delete(plan)
    db.commit()
    
    return build_response({"message": "Plan deleted successfully"}, request_id)

@router.patch("/plans/{plan_id}")
def update_plan(
    plan_id: str,
    body: UpdatePlanMetadataRequest,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
):
    log.info("[%s] Updating plan: %s", request_id, plan_id)
    plan = db.get(PlatformPlan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
        
    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(plan, key, value)
        
    db.commit()
    db.refresh(plan)
    
    return build_response({"id": plan.id, "code": plan.code}, request_id)

@router.get("/audits")
def list_audits(
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
):
    log.info("[%s] Fetching audit logs", request_id)
    audits = db.scalars(select(PlatformAuditLog).order_by(PlatformAuditLog.created_at.desc()).limit(100)).all()
    
    return build_response([{
        "id": a.id,
        "userId": a.user_id,
        "userEmail": a.user_email,
        "action": a.action,
        "resourceType": a.resource_type,
        "resourceId": a.resource_id,
        "details": a.details,
        "createdAt": a.created_at.isoformat() if a.created_at else None
    } for a in audits], request_id)

class CreateNotificationRequest(BaseModel):
    title: str
    message: str
    type: str = "info"
    targetAudience: str = "all"

@router.get("/global-notifications")
def list_global_notifications(
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
):
    log.info("[%s] Fetching notifications", request_id)
    # Exclude system-generated super_admin alerts — those are only for the bell/audit log
    notifications = db.scalars(
        select(PlatformNotification)
        .where(PlatformNotification.target_audience != 'super_admin')
        .order_by(PlatformNotification.created_at.desc())
        .limit(100)
    ).all()
    
    return build_response([{
        "id": n.id,
        "title": n.title,
        "message": n.message,
        "type": n.type,
        "targetAudience": n.target_audience,
        "sentBy": n.sent_by,
        "createdAt": n.created_at.isoformat() if n.created_at else None
    } for n in notifications], request_id)

@router.post("/global-notifications")
def create_global_notification(
    body: CreateNotificationRequest,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
):
    log.info("[%s] Creating notification", request_id)
    notif = PlatformNotification(
        title=body.title,
        message=body.message,
        type=body.type,
        target_audience=body.targetAudience,
        sent_by=payload.get("email", "System"),
    )
    db.add(notif)
    
    # Log the audit
    audit = PlatformAuditLog(
        user_id=payload.get("sub"),
        user_email=payload.get("email"),
        action="notification.sent",
        resource_type="notification",
        resource_id=notif.id,
        details={"title": body.title, "audience": body.targetAudience},
    )
    db.add(audit)
    
    db.commit()
    return build_response({"message": "Notification sent successfully"}, request_id)
