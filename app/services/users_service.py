"""Users service — port of apps/api/src/users/users.service.ts."""
from __future__ import annotations

from datetime import datetime, timezone
from math import ceil

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..common.enums import PLAN_LIMITS, PlanType, UserRole, UserStatus
from ..common.exceptions import bad_request, conflict, forbidden, not_found
from ..models import Department, RefreshToken, Tenant, User
from ..schemas.users import (
    CreateUserRequest,
    InviteUserRequest,
    ListUsersQuery,
    UpdateUserRequest,
)
from ..security import hash_password, random_token_hex
from .auth_service import AuthService


def _serialize(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "status": user.status,
        "departmentId": user.departmentId,
        "department": (
            {
                "id": user.department.id,
                "name": user.department.name,
                "slug": user.department.slug,
                "autoAssignEnabled": user.department.autoAssignEnabled,
                "isActive": user.department.isActive,
            }
            if user.department
            else None
        ),
        "lastLoginAt": user.lastLoginAt,
        "createdAt": user.createdAt,
    }


class UsersService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list_users(self, tenant_id: str, q: ListUsersQuery) -> dict:
        filters = [User.tenantId == tenant_id]
        if q.role:
            filters.append(User.role == q.role.value)
        if q.status:
            filters.append(User.status == q.status.value)
        if q.departmentId:
            filters.append(User.departmentId == q.departmentId)
        if q.search and q.search.strip():
            s = f"%{q.search.strip()}%"
            filters.append(or_(User.email.ilike(s), User.name.ilike(s)))

        skip = (q.page - 1) * q.limit
        stmt = (
            select(User)
            .options(selectinload(User.department))
            .where(and_(*filters))
            .order_by(User.createdAt.asc())
            .offset(skip)
            .limit(q.limit)
        )
        items = self.db.scalars(stmt).all()
        total = self.db.scalar(select(func.count()).select_from(User).where(and_(*filters))) or 0

        return {
            "items": [_serialize(u) for u in items],
            "pagination": {
                "page": q.page,
                "limit": q.limit,
                "total": total,
                "totalPages": ceil(total / q.limit) if q.limit else 0,
            },
        }

    def get_user(self, tenant_id: str, user_id: str) -> dict:
        user = self.db.scalar(
            select(User)
            .options(selectinload(User.department))
            .where(User.id == user_id, User.tenantId == tenant_id)
        )
        if not user:
            raise not_found("USER_NOT_FOUND", "User not found")
        return _serialize(user)

    def create_user(self, tenant_id: str, dto: CreateUserRequest) -> dict:
        if dto.departmentId:
            dept = self.db.scalar(
                select(Department).where(
                    Department.id == dto.departmentId,
                    Department.tenantId == tenant_id,
                    Department.isActive.is_(True),
                )
            )
            if not dept:
                raise bad_request(
                    "INVALID_DEPARTMENT", "Selected department is invalid or inactive"
                )

        existing = self.db.scalar(
            select(User).where(User.tenantId == tenant_id, User.email == dto.email)
        )
        if existing:
            raise conflict("USER_ALREADY_EXISTS", "User with this email already exists")

        tenant = self.db.get(Tenant, tenant_id)
        if tenant is None:
            raise not_found("TENANT_NOT_FOUND", "Tenant not found")
        limits = PLAN_LIMITS.get(PlanType(tenant.plan))
        if limits and limits["users"] != 999_999:
            current = (
                self.db.scalar(
                    select(func.count())
                    .select_from(User)
                    .where(User.tenantId == tenant_id, User.status != UserStatus.INACTIVE.value)
                )
                or 0
            )
            if current >= limits["users"]:
                raise bad_request(
                    "PLAN_LIMIT_EXCEEDED",
                    f"User limit of {limits['users']} reached on {tenant.plan} plan. "
                    "Upgrade to create more users.",
                )

        plain_password = dto.password or _generate_password()
        user = User(
            tenantId=tenant_id,
            departmentId=dto.departmentId,
            email=dto.email,
            name=dto.name,
            role=dto.role.value,
            passwordHash=hash_password(plain_password),
            status=UserStatus.ACTIVE.value,
        )
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        return {"user": _serialize(user), "password": plain_password}

    def update_user(self, tenant_id: str, user_id: str, dto: UpdateUserRequest) -> dict:
        user = self.db.scalar(
            select(User).where(User.id == user_id, User.tenantId == tenant_id)
        )
        if not user:
            raise not_found("USER_NOT_FOUND", "User not found")

        if dto.departmentId is not None:
            if dto.departmentId == "":
                user.departmentId = None
            else:
                dept = self.db.scalar(
                    select(Department).where(
                        Department.id == dto.departmentId,
                        Department.tenantId == tenant_id,
                        Department.isActive.is_(True),
                    )
                )
                if not dept:
                    raise bad_request(
                        "INVALID_DEPARTMENT", "Selected department is invalid or inactive"
                    )
                user.departmentId = dto.departmentId

        if dto.role and dto.role != UserRole.ADMIN and user.role == UserRole.ADMIN.value:
            admin_count = (
                self.db.scalar(
                    select(func.count())
                    .select_from(User)
                    .where(
                        User.tenantId == tenant_id,
                        User.role == UserRole.ADMIN.value,
                        User.status == UserStatus.ACTIVE.value,
                    )
                )
                or 0
            )
            if admin_count <= 1:
                raise forbidden(
                    "CANNOT_DEMOTE_LAST_ADMIN", "Cannot change role of the only active admin"
                )

        if dto.name:
            user.name = dto.name
        if dto.role:
            user.role = dto.role.value
        if dto.status:
            user.status = dto.status.value

        self.db.commit()
        self.db.refresh(user)
        return {"user": _serialize(user)}

    def deactivate_user(self, tenant_id: str, user_id: str) -> None:
        user = self.db.scalar(
            select(User).where(User.id == user_id, User.tenantId == tenant_id)
        )
        if not user:
            raise not_found("USER_NOT_FOUND", "User not found")

        if user.role == UserRole.ADMIN.value:
            admin_count = (
                self.db.scalar(
                    select(func.count())
                    .select_from(User)
                    .where(
                        User.tenantId == tenant_id,
                        User.role == UserRole.ADMIN.value,
                        User.status == UserStatus.ACTIVE.value,
                    )
                )
                or 0
            )
            if admin_count <= 1:
                raise forbidden(
                    "CANNOT_DEACTIVATE_LAST_ADMIN", "Cannot deactivate the only active admin"
                )

        user.status = UserStatus.INACTIVE.value
        now = datetime.now(tz=timezone.utc)
        for t in self.db.scalars(
            select(RefreshToken).where(
                RefreshToken.userId == user.id, RefreshToken.revokedAt.is_(None)
            )
        ):
            t.revokedAt = now
        self.db.commit()

    def invite_user(self, tenant_id: str, dto: InviteUserRequest) -> dict:
        existing = self.db.scalar(
            select(User).where(User.tenantId == tenant_id, User.email == dto.email)
        )
        if existing:
            raise conflict("USER_ALREADY_EXISTS", "User with this email already exists")

        tenant = self.db.get(Tenant, tenant_id)
        if tenant is None:
            raise not_found("TENANT_NOT_FOUND", "Tenant not found")
        limits = PLAN_LIMITS.get(PlanType(tenant.plan))
        if limits and limits["users"] != 999_999:
            current = (
                self.db.scalar(
                    select(func.count())
                    .select_from(User)
                    .where(User.tenantId == tenant_id, User.status != UserStatus.INACTIVE.value)
                )
                or 0
            )
            if current >= limits["users"]:
                raise bad_request(
                    "PLAN_LIMIT_EXCEEDED",
                    f"User limit of {limits['users']} reached on {tenant.plan} plan.",
                )

        user = User(
            tenantId=tenant_id,
            email=dto.email,
            name=dto.name,
            role=dto.role.value,
            passwordHash="",
            status=UserStatus.INVITED.value,
        )
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)

        invite_token = AuthService.sign_invite_token(
            user_id=user.id, tenant_id=tenant_id, role=dto.role.value
        )
        return {"user": _serialize(user), "inviteToken": invite_token}


def _generate_password() -> str:
    return f"Qa{random_token_hex(8)}!"
