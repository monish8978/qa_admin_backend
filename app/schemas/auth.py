"""Auth request/response schemas — mirror apps/api/src/auth/dto/auth.dto.ts."""
from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from ..common.enums import PlanType, UserRole, UserStatus

_SLUG_RE = re.compile(r"^[a-z0-9-]+$")


class SignupRequest(BaseModel):
    tenantName: str = Field(min_length=2, max_length=50)
    tenantSlug: str = Field(min_length=3, max_length=30)
    adminEmail: EmailStr
    adminName: str = Field(min_length=2, max_length=50)
    password: str = Field(min_length=12, max_length=100)
    plan: PlanType

    @field_validator("tenantSlug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError("tenantSlug must be lowercase letters, numbers, or hyphens")
        return v


class LoginRequest(BaseModel):
    # Use plain str rather than EmailStr â€“ pydantic's EmailStr rejects reserved
    # TLDs like .local (RFC 6762), which the existing user table can contain
    # (e.g. seeded admin@dev.local). Authentication still fails safely on
    # unknown addresses via the DB lookup.
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1)
    tenantSlug: str | None = Field(default=None)
    skip2fa: bool = Field(default=False)


class RefreshTokenRequest(BaseModel):
    refreshToken: str


class ForgotPasswordRequest(BaseModel):
    # Keep forgot-password input compatible with existing internal addresses
    # such as admin@dev.local.
    email: str = Field(min_length=3, max_length=254)
    tenantSlug: str | None = Field(default=None, min_length=3)


class ResetPasswordRequest(BaseModel):
    token: str
    otp: str | None = None
    password: str = Field(min_length=8, max_length=12)


class VerifyMfaRequest(BaseModel):
    mfaToken: str
    otp: str


class AcceptInviteRequest(BaseModel):
    token: str
    password: str = Field(min_length=12)


class TenantSummary(BaseModel):
    id: str
    slug: str
    name: str
    plan: PlanType


class AuthUserSummary(BaseModel):
    id: str
    name: str
    email: EmailStr
    role: UserRole


class SignupResponse(BaseModel):
    accessToken: str
    refreshToken: str
    tenant: TenantSummary


class LoginResponse(BaseModel):
    accessToken: str
    refreshToken: str
    user: AuthUserSummary


class TokenPair(BaseModel):
    accessToken: str
    refreshToken: str


class MeResponse(BaseModel):
    id: str
    email: EmailStr
    name: str
    role: UserRole
    status: UserStatus
    tenantId: str
    lastLoginAt: datetime | None = None


class SignupSendOtpRequest(BaseModel):
    email: EmailStr


class SignupVerifyOtpRequest(BaseModel):
    email: EmailStr
    otp: str = Field(min_length=6, max_length=6)
