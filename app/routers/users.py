"""Users router — mirrors apps/api/src/users/users.controller.ts."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from ..common.enums import UserRole
from ..common.responses import build_response
from ..deps import get_db, get_request_id, require_roles
from ..schemas.users import (
    CreateUserRequest,
    InviteUserRequest,
    ListUsersQuery,
    UpdateUserRequest,
)
from ..services.users_service import UsersService

router = APIRouter(prefix="/users", tags=["Users"])
log = logging.getLogger("qa.api.routers.users")


@router.get("")
def list_users(
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    query: Annotated[ListUsersQuery, Depends()],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Listing users for tenant: %s", request_id, tenant_id)
    result = UsersService(db).list_users(tenant_id, query)
    log.info("[%s] Successfully retrieved user list for tenant: %s", request_id, tenant_id)
    return build_response(result, request_id)


@router.post("")
def create_user(
    dto: CreateUserRequest,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Creating new user for tenant: %s (email: %s)", request_id, tenant_id, dto.email)
    result = UsersService(db).create_user(tenant_id, dto)
    log.info("[%s] Successfully created user for tenant: %s", request_id, tenant_id)
    return build_response(result, request_id)


@router.post("/invite")
def invite_user(
    dto: InviteUserRequest,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Inviting user for tenant: %s (email: %s, role: %s)", request_id, tenant_id, dto.email, dto.role)
    result = UsersService(db).invite_user(tenant_id, dto)
    log.info("[%s] Successfully sent invitation for tenant: %s", request_id, tenant_id)
    return build_response(result, request_id)


@router.get("/{user_id}")
def get_user(
    user_id: str,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching user details for user_id: %s, tenant: %s", request_id, user_id, tenant_id)
    result = UsersService(db).get_user(tenant_id, user_id)
    log.info("[%s] Successfully retrieved details for user_id: %s", request_id, user_id)
    return build_response(result, request_id)


@router.patch("/{user_id}")
def update_user(
    user_id: str,
    dto: UpdateUserRequest,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Updating user: %s for tenant: %s", request_id, user_id, tenant_id)
    result = UsersService(db).update_user(tenant_id, user_id, dto)
    log.info("[%s] Successfully updated user: %s", request_id, user_id)
    return build_response(result, request_id)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def deactivate_user(
    user_id: str,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Deactivating user: %s for tenant: %s", request_id, user_id, tenant_id)
    UsersService(db).deactivate_user(tenant_id, user_id)
    log.info("[%s] Successfully deactivated user: %s", request_id, user_id)

