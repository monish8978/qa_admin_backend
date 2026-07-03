"""Auth router — mirrors apps/api/src/auth/auth.controller.ts."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, status
from sqlalchemy.orm import Session

from ..common.responses import build_response
from ..deps import get_current_payload, get_db, get_request_id
from ..schemas.auth import (
    AcceptInviteRequest,
    ForgotPasswordRequest,
    LoginRequest,
    RefreshTokenRequest,
    ResetPasswordRequest,
    SignupRequest,
)
from ..services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["Auth"])
log = logging.getLogger("qa.api.routers.auth")


@router.post("/signup")
def signup(
    dto: SignupRequest,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
):
    log.info("[%s] User signing up with email: %s", request_id, dto.adminEmail)
    result = AuthService(db).signup(dto)
    log.info("[%s] User successfully signed up", request_id)
    return build_response(result, request_id)


@router.post("/login", status_code=status.HTTP_200_OK)
def login(
    dto: LoginRequest,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    x_tenant_slug: Annotated[str | None, Header(alias="X-Tenant-Slug")] = None,
):
    log.info(
        "[%s] User attempting login with email: %s, tenant_slug: %s",
        request_id,
        dto.email,
        x_tenant_slug,
    )
    result = AuthService(db).login(dto, x_tenant_slug)
    log.info(
        "[%s] User successfully logged in (tenantId=%s)",
        request_id,
        result.get("user", {}).get("tenantId"),
    )
    return build_response(result, request_id)


@router.post("/refresh", status_code=status.HTTP_200_OK)
def refresh(
    dto: RefreshTokenRequest,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
):
    log.info("[%s] Requesting token refresh", request_id)
    result = AuthService(db).refresh(dto.refreshToken)
    log.info("[%s] Token successfully refreshed", request_id)
    return build_response(result, request_id)


@router.post("/logout", status_code=status.HTTP_200_OK)
def logout(
    dto: RefreshTokenRequest,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    _payload: Annotated[dict, Depends(get_current_payload)],
):
    log.info("[%s] User logging out", request_id)
    AuthService(db).logout(dto.refreshToken)
    log.info("[%s] User successfully logged out", request_id)
    return build_response({"success": True}, request_id)


@router.post("/forgot-password", status_code=status.HTTP_200_OK)
def forgot_password(
    dto: ForgotPasswordRequest,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    x_tenant_slug: Annotated[str | None, Header(alias="X-Tenant-Slug")] = None,
):
    log.info("[%s] Forgot password requested for email: %s", request_id, dto.email)
    tenant_slug = dto.tenantSlug or x_tenant_slug
    AuthService(db).forgot_password(dto.email, tenant_slug)
    log.info("[%s] Forgot password link sent successfully", request_id)
    return build_response(
        {"message": "If that email exists, a reset link has been sent"}, request_id
    )


@router.post("/reset-password", status_code=status.HTTP_200_OK)
def reset_password(
    dto: ResetPasswordRequest,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
):
    log.info("[%s] Reset password request received", request_id)
    AuthService(db).reset_password(dto)
    log.info("[%s] Password reset completed successfully", request_id)
    return build_response({"success": True}, request_id)


@router.post("/accept-invite", status_code=status.HTTP_200_OK)
def accept_invite(
    dto: AcceptInviteRequest,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
):
    log.info("[%s] Accepting invite for token: %s", request_id, dto.token)
    result = AuthService(db).accept_invite(dto)
    log.info("[%s] Invite accepted successfully", request_id)
    return build_response(result, request_id)


@router.get("/me")
def me(
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: Annotated[dict, Depends(get_current_payload)],
):
    log.info("[%s] Fetching current user details for userId: %s", request_id, payload.get("sub"))
    result = AuthService(db).get_me(payload["sub"])
    log.info("[%s] Current user details fetched successfully", request_id)
    return build_response(result, request_id)


