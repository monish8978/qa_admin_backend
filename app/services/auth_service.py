"""Auth service — port of apps/api/src/auth/auth.service.ts."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, select
from sqlalchemy.orm import Session, selectinload

from ..common.enums import PlanType, UserStatus
from ..common.exceptions import bad_request, conflict, forbidden, unauthorized
from ..config import get_settings
from ..models import RefreshToken, Subscription, Tenant, UsageMetric, User
from ..redis_client import get_redis
from ..schemas.auth import (
    AcceptInviteRequest,
    LoginRequest,
    ResetPasswordRequest,
    SignupRequest,
)
from ..security import (
    hash_password,
    issue_token_pair,
    random_token_hex,
    sha256_hex,
    sign_jwt,
    verify_jwt,
    verify_password,
)
from . import notify_service

log = logging.getLogger("qa.auth")

PASSWORD_RESET_TTL_S = 15 * 60


class AuthService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()

    # ── Signup ────────────────────────────────────────────────────────────────
    def signup(self, dto: SignupRequest) -> dict:
        existing = self.db.scalar(select(Tenant).where(Tenant.slug == dto.tenantSlug))
        if existing:
            raise conflict("TENANT_SLUG_TAKEN", "Tenant slug is already taken")

        now = datetime.now(tz=timezone.utc)
        trial_end = now + timedelta(days=30)

        tenant = Tenant(
            slug=dto.tenantSlug,
            name=dto.tenantName,
            plan=dto.plan.value,
            status="PROVISIONING",
        )
        self.db.add(tenant)
        self.db.flush()

        admin = User(
            tenantId=tenant.id,
            email=dto.adminEmail,
            name=dto.adminName,
            passwordHash=hash_password(dto.password),
            role="ADMIN",
            status="ACTIVE",
        )
        self.db.add(admin)

        self.db.add(
            Subscription(
                tenantId=tenant.id,
                plan=dto.plan.value,
                status="TRIALING",
                currentPeriodStart=now,
                currentPeriodEnd=trial_end,
                trialEndsAt=trial_end,
            )
        )
        self.db.flush()

        access, refresh = self._issue_and_store(admin)

        from .provision_task import tenant_provision
        log.info("Provisioning tenant %s synchronously to prevent race conditions", tenant.slug)
        
        # We MUST commit the transaction first so the background worker (which opens a new session)
        # can actually see the Tenant record we just inserted.
        self.db.commit()
        
        try:
            tenant_provision.apply(kwargs={"tenantId": tenant.id, "adminUserId": admin.id}).get()
        except Exception as e:
            log.error("Synchronous provisioning failed for tenant %s: %s", tenant.id, e, exc_info=True)
            raise bad_request("PROVISIONING_FAILED", "Failed to provision tenant database") from e

        log.info("signup ok tenant=%s admin=%s", tenant.slug, admin.email)

        return {
            "accessToken": access,
            "refreshToken": refresh,
            "tenant": {
                "id": tenant.id,
                "slug": tenant.slug,
                "name": tenant.name,
                "plan": tenant.plan,
            },
        }

    # ── Login ────────────────────────────────────────────────────────────────
    def login(self, dto: LoginRequest, tenant_slug: str | None) -> dict:
        stmt = select(User).options(selectinload(User.tenant)).where(User.email == dto.email)
        if tenant_slug:
            stmt = stmt.join(User.tenant).where(Tenant.slug == tenant_slug)
        # Emails are unique *per tenant*, not globally. Without an explicit
        # tenant slug the same email can match users in multiple tenants — pick
        # arbitrarily would log the user into the wrong tenant, so fail loudly.
        matches = self.db.scalars(stmt).all()
        if len(matches) > 1:
            log.warning(
                "login ambiguous email=%s matched %d tenants without tenantSlug",
                dto.email,
                len(matches),
            )
            raise bad_request(
                "AMBIGUOUS_TENANT",
                "Multiple workspaces use this email. Enter your workspace slug.",
            )
        user = matches[0] if matches else None

        if not user:
            log.warning(
                "login failed reason=INVALID_CREDENTIALS email=%s tenantSlug=%s",
                dto.email,
                tenant_slug,
            )
            raise unauthorized("INVALID_CREDENTIALS", "Invalid email or password")

        if user.status == UserStatus.INACTIVE.value:
            raise forbidden("ACCOUNT_SUSPENDED", "Account is deactivated")

        if user.tenant.status in ("SUSPENDED", "CANCELLED"):
            raise forbidden("ACCOUNT_SUSPENDED", "Tenant account is suspended")

        if not verify_password(dto.password, user.passwordHash):
            raise unauthorized("INVALID_CREDENTIALS", "Invalid email or password")

        now = datetime.now(tz=timezone.utc)
        prev_login = user.lastLoginAt
        # Postgres `timestamp(3)` (Prisma default) returns naive datetimes; normalise to UTC.
        if prev_login is not None and prev_login.tzinfo is None:
            prev_login = prev_login.replace(tzinfo=timezone.utc)
        user.lastLoginAt = now

        # Monthly active user metric (mirrors Nest behaviour). Use the shared
        # period helper so the UsageMetric unique key matches every other writer.
        from .usage_meter_service import current_period

        period_start, period_end = current_period(now)
        first_this_month = prev_login is None or prev_login < period_start
        if first_this_month:
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            stmt = pg_insert(UsageMetric).values(
                tenantId=user.tenantId,
                periodStart=period_start,
                periodEnd=period_end,
                activeUsers=1,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[UsageMetric.tenantId, UsageMetric.periodStart, UsageMetric.periodEnd],
                set_={"activeUsers": UsageMetric.activeUsers + 1},
            )
            self.db.execute(stmt)

        access, refresh = self._issue_and_store(user)
        self.db.commit()

        log.info("login ok user=%s tenant=%s", user.email, user.tenantId)
        return {
            "accessToken": access,
            "refreshToken": refresh,
            "user": {
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "role": user.role,
            },
        }

    # ── Refresh ──────────────────────────────────────────────────────────────
    def refresh(self, raw_refresh: str) -> dict:
        try:
            payload = verify_jwt(raw_refresh, self.settings.REFRESH_SECRET)
        except ValueError:
            raise unauthorized("TOKEN_EXPIRED", "Refresh token is expired or invalid") from None

        if payload.get("type") != "refresh":
            raise unauthorized("TOKEN_EXPIRED", "Invalid token type")

        token_hash = sha256_hex(raw_refresh)
        now = datetime.now(tz=timezone.utc)

        # Atomically revoke the presented token. Only one concurrent refresh can
        # win the UPDATE … WHERE revokedAt IS NULL race; the loser gets zero rows
        # and is rejected, preventing refresh-token reuse / double issuance.
        from sqlalchemy import update

        result = self.db.execute(
            update(RefreshToken)
            .where(
                RefreshToken.tokenHash == token_hash,
                RefreshToken.revokedAt.is_(None),
                RefreshToken.expiresAt > now,
            )
            .values(revokedAt=now)
        )
        if result.rowcount != 1:
            self.db.rollback()
            raise unauthorized("TOKEN_REVOKED", "Refresh token has been revoked")

        user = self.db.scalar(
            select(User).options(selectinload(User.tenant)).where(User.id == payload["sub"])
        )
        if not user:
            self.db.commit()
            raise unauthorized("TOKEN_REVOKED", "Refresh token has been revoked")

        # Honour suspension/deactivation that happened after the token was issued.
        if user.status == UserStatus.INACTIVE.value:
            self.db.commit()
            raise forbidden("ACCOUNT_SUSPENDED", "Account is deactivated")
        if user.tenant is not None and user.tenant.status in ("SUSPENDED", "CANCELLED"):
            self.db.commit()
            raise forbidden("ACCOUNT_SUSPENDED", "Tenant account is suspended")

        access, refresh = self._issue_and_store(user)
        self.db.commit()
        return {"accessToken": access, "refreshToken": refresh}

    # ── Logout ───────────────────────────────────────────────────────────────
    def logout(self, raw_refresh: str) -> None:
        token_hash = sha256_hex(raw_refresh)
        tokens = self.db.scalars(
            select(RefreshToken).where(
                RefreshToken.tokenHash == token_hash, RefreshToken.revokedAt.is_(None)
            )
        ).all()
        now = datetime.now(tz=timezone.utc)
        for t in tokens:
            t.revokedAt = now
        self.db.commit()

    # ── Forgot Password ──────────────────────────────────────────────────────
    def forgot_password(self, email: str, tenant_slug: str | None = None) -> None:
        stmt = select(User).options(selectinload(User.tenant)).where(User.email == email)
        if tenant_slug:
            stmt = stmt.join(User.tenant).where(Tenant.slug == tenant_slug)
        matches = self.db.scalars(stmt).all()
        if len(matches) > 1:
            log.warning(
                "forgot_password ambiguous email=%s matched %d tenants without tenantSlug",
                email,
                len(matches),
            )
            raise bad_request(
                "AMBIGUOUS_TENANT",
                "Multiple workspaces use this email. Enter your workspace slug.",
            )
        user = matches[0] if matches else None
        if not user:
            return  # do not leak existence

        token = random_token_hex(32)
        token_hash = sha256_hex(token)

        redis = get_redis()
        if redis is None:
            log.warning("forgot_password skipped — Redis disabled")
            return
        redis.set(f"pwd_reset:{token_hash}", user.id, ex=PASSWORD_RESET_TTL_S)

        reset_url = f"{self.settings.WEB_URL}/reset-password?token={token}"
        try:
            notify_service.send_email(
                to=email, template="password_reset", data={"resetUrl": reset_url}
            )
        except Exception as e:  # non-fatal — same behaviour as Nest
            log.error("notify failed: %s", e)

    # ── Reset Password ───────────────────────────────────────────────────────
    def reset_password(self, dto: ResetPasswordRequest) -> None:
        redis = get_redis()
        if redis is None:
            raise bad_request(
                "FEATURE_UNAVAILABLE",
                "Password reset requires Redis. Please contact your administrator.",
            )
        token_hash = sha256_hex(dto.token)
        user_id = redis.get(f"pwd_reset:{token_hash}")
        if not user_id:
            raise bad_request("INVALID_RESET_TOKEN", "Reset token is invalid or expired")

        user = self.db.get(User, user_id)
        if not user:
            raise bad_request("INVALID_RESET_TOKEN", "Reset token is invalid or expired")
        user.passwordHash = hash_password(dto.password)
        redis.delete(f"pwd_reset:{token_hash}")

        now = datetime.now(tz=timezone.utc)
        for t in self.db.scalars(
            select(RefreshToken).where(
                RefreshToken.userId == user.id, RefreshToken.revokedAt.is_(None)
            )
        ):
            t.revokedAt = now
        self.db.commit()

    # ── Accept Invite ────────────────────────────────────────────────────────
    def accept_invite(self, dto: AcceptInviteRequest) -> dict:
        try:
            payload = verify_jwt(dto.token, self.settings.JWT_SECRET)
        except ValueError:
            raise bad_request("INVALID_INVITE_TOKEN", "Invite token is invalid or expired") from None
        if payload.get("type") != "invite":
            raise bad_request("INVALID_INVITE_TOKEN", "Invalid token type")

        user = self.db.get(User, payload["sub"])
        if not user or user.status != UserStatus.INVITED.value:
            raise bad_request("INVITE_ALREADY_USED", "Invite has already been used")

        user.passwordHash = hash_password(dto.password)
        user.status = UserStatus.ACTIVE.value
        access, refresh = self._issue_and_store(user)
        self.db.commit()
        return {
            "accessToken": access,
            "refreshToken": refresh,
            "user": {
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "role": user.role,
            },
        }

    # ── Me ───────────────────────────────────────────────────────────────────
    def get_me(self, user_id: str) -> dict:
        user = self.db.get(User, user_id)
        if not user:
            raise unauthorized("USER_NOT_FOUND", "User not found")
        return {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
            "status": user.status,
            "tenantId": user.tenantId,
            "lastLoginAt": user.lastLoginAt,
        }

    # ── Helpers ──────────────────────────────────────────────────────────────
    def _issue_and_store(self, user: User) -> tuple[str, str]:
        access, refresh = issue_token_pair(
            user_id=user.id, tenant_id=user.tenantId, role=user.role
        )
        from ..security import parse_duration

        self.db.add(
            RefreshToken(
                userId=user.id,
                tokenHash=sha256_hex(refresh),
                expiresAt=datetime.now(tz=timezone.utc)
                + parse_duration(self.settings.REFRESH_EXPIRES_IN),
            )
        )
        return access, refresh

    @staticmethod
    def sign_invite_token(*, user_id: str, tenant_id: str, role: str) -> str:
        s = get_settings()
        return sign_jwt(
            sub=user_id,
            tenant_id=tenant_id,
            role=role,
            token_type="invite",
            secret=s.JWT_SECRET,
            expires_in="7d",
        )

    @staticmethod
    def _plan_value(value: str) -> PlanType:
        return PlanType(value)

