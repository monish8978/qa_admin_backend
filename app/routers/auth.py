"""Auth router — mirrors apps/api/src/auth/auth.controller.ts."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..common.responses import build_response
from ..deps import get_current_payload, get_db, get_request_id, rate_limit_ip
from ..schemas.auth import (
    AcceptInviteRequest,
    ForgotPasswordRequest,
    LoginRequest,
    RefreshTokenRequest,
    ResetPasswordRequest,
    SignupRequest,
    VerifyMfaRequest,
    SignupSendOtpRequest,
    SignupVerifyOtpRequest,
)
from ..services.auth_service import AuthService
from ..models.master import PlatformNotification
from sqlalchemy import select

router = APIRouter(prefix="/auth", tags=["Auth"])
log = logging.getLogger("qa.api.routers.auth")


@router.get("/test-deploy")
def test_deploy():
    return {"status": "updated_v1"}

@router.get("/notifications")
def get_user_notifications(
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: Annotated[dict, Depends(get_current_payload)],
):
    log.info("[%s] Fetching global notifications for user", request_id)
    notifications = db.scalars(select(PlatformNotification).where(PlatformNotification.target_audience != 'super_admin').order_by(PlatformNotification.created_at.desc()).limit(10)).all()
    return build_response([{
        "id": n.id,
        "title": n.title,
        "message": n.message,
        "type": n.type,
        "targetAudience": n.target_audience,
        "sentBy": n.sent_by,
        "createdAt": n.created_at.isoformat() if n.created_at else None
    } for n in notifications], request_id)


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


@router.post("/login", status_code=status.HTTP_200_OK, dependencies=[Depends(rate_limit_ip(5, 60))])
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
    tenant_slug = getattr(dto, 'tenantSlug', None) or x_tenant_slug
    result = AuthService(db).login(dto, tenant_slug)
    log.info(
        "[%s] User successfully logged in or mfa_required (tenantId=%s)",
        request_id,
        result.get("user", {}).get("tenantId") if result else None,
    )
    return build_response(result, request_id)


@router.post("/verify-mfa", status_code=status.HTTP_200_OK)
def verify_mfa(
    dto: VerifyMfaRequest,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
):
    log.info("[%s] Verifying OTP for token: %s", request_id, dto.mfaToken)
    result = AuthService(db).verify_mfa(dto)
    log.info(
        "[%s] OTP verified successfully. User logged in (tenantId=%s)",
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


@router.post("/forgot-password", status_code=status.HTTP_200_OK, dependencies=[Depends(rate_limit_ip(3, 60))])
def forgot_password(
    dto: ForgotPasswordRequest,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    x_tenant_slug: Annotated[str | None, Header(alias="X-Tenant-Slug")] = None,
):
    log.info("[%s] Forgot password requested for email: %s", request_id, dto.email)
    tenant_slug = getattr(dto, 'tenantSlug', None) or x_tenant_slug
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


@router.post("/signup/send-otp", status_code=status.HTTP_200_OK)
def signup_send_otp(
    dto: SignupSendOtpRequest,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
):
    log.info("[%s] Sending signup OTP to email: %s", request_id, dto.email)
    AuthService(db).signup_send_otp(dto.email)
    log.info("[%s] Signup OTP sent successfully", request_id)
    return build_response({"success": True}, request_id)


@router.post("/signup/verify-otp", status_code=status.HTTP_200_OK)
def signup_verify_otp(
    dto: SignupVerifyOtpRequest,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
):
    log.info("[%s] Verifying signup OTP for email: %s", request_id, dto.email)
    AuthService(db).signup_verify_otp(dto.email, dto.otp)
    log.info("[%s] Signup OTP verified successfully", request_id)
    return build_response({"success": True}, request_id)


@router.get("/approve-tenant")
def approve_tenant(
    tenantId: str,
    token: str,
    db: Annotated[Session, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    format: str = "html",
):
    log.info("[%s] Approving tenant with ID: %s (format: %s)", request_id, tenantId, format)
    try:
        AuthService(db).approve_tenant(tenantId, token)
        log.info("[%s] Tenant successfully approved", request_id)
        if format == "json":
            return {"status": "success", "message": "Tenant successfully approved"}
        return HTMLResponse(content="""<!DOCTYPE html>
<html>
<head>
  <title>Workspace Approved</title>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
      background-color: #f8fafc;
      color: #334155;
      display: flex;
      justify-content: center;
      align-items: center;
      min-height: 100vh;
      margin: 0;
    }
    .card {
      background: white;
      padding: 40px;
      border-radius: 16px;
      box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.05), 0 2px 4px -2px rgb(0 0 0 / 0.05);
      border: 1px solid #e2e8f0;
      max-width: 450px;
      width: 100%;
      text-align: center;
    }
    .icon {
      background: linear-gradient(135deg, #4f46e5, #7c3aed);
      color: white;
      width: 60px;
      height: 60px;
      border-radius: 50%;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 28px;
      margin-bottom: 20px;
      box-shadow: 0 10px 15px -3px rgba(79, 70, 229, 0.2);
    }
    h1 {
      font-size: 22px;
      font-weight: 700;
      color: #0f172a;
      margin: 0 0 12px 0;
    }
    p {
      font-size: 14px;
      line-height: 1.6;
      color: #64748b;
      margin: 0 0 24px 0;
    }
    .btn {
      display: inline-block;
      background: linear-gradient(135deg, #4f46e5, #7c3aed);
      color: white;
      text-decoration: none;
      padding: 12px 24px;
      font-weight: 600;
      font-size: 14px;
      border-radius: 8px;
      transition: all 0.2s;
    }
    .btn:hover {
      opacity: 0.95;
      transform: translateY(-1px);
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">✓</div>
    <h1>Workspace Approved Successfully!</h1>
    <p>The workspace and user account have been activated. The registered user has been notified via email and can now log in.</p>
    <a href="/login" class="btn">Go to Login</a>
  </div>
</body>
</html>""")
    except Exception as e:
        log.error("[%s] Failed to approve tenant: %s", request_id, e)
        if format == "json":
            raise
        return HTMLResponse(content=f"""<!DOCTYPE html>
<html>
<head>
  <title>Approval Failed</title>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
      background-color: #f8fafc;
      color: #334155;
      display: flex;
      justify-content: center;
      align-items: center;
      min-height: 100vh;
      margin: 0;
    }}
    .card {{
      background: white;
      padding: 40px;
      border-radius: 16px;
      box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.05), 0 2px 4px -2px rgb(0 0 0 / 0.05);
      border: 1px solid #e2e8f0;
      max-width: 450px;
      width: 100%;
      text-align: center;
    }}
    .icon {{
      background: linear-gradient(135deg, #ef4444, #f43f5e);
      color: white;
      width: 60px;
      height: 60px;
      border-radius: 50%;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 28px;
      margin-bottom: 20px;
      box-shadow: 0 10px 15px -3px rgba(239, 68, 68, 0.2);
    }}
    h1 {{
      font-size: 22px;
      font-weight: 700;
      color: #0f172a;
      margin: 0 0 12px 0;
    }}
    p {{
      font-size: 14px;
      line-height: 1.6;
      color: #64748b;
      margin: 0 0 24px 0;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">✗</div>
    <h1>Approval Failed</h1>
    <p>Failed to approve workspace: {str(e)}</p>
  </div>
</body>
</html>""", status_code=400)


