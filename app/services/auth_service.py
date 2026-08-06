"""Auth service — port of apps/api/src/auth/auth.service.ts."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, select
from sqlalchemy.orm import Session, selectinload

import random
import json

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
    VerifyMfaRequest,
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
        redis = get_redis()
        if redis is None:
            raise bad_request("FEATURE_UNAVAILABLE", "Redis is required for signup verification.")

        verified = redis.get(f"signup_verified:{dto.adminEmail}")
        if not verified:
            raise bad_request(
                "EMAIL_NOT_VERIFIED", "Please verify your email address using OTP first."
            )

        existing = self.db.scalar(select(Tenant).where(Tenant.slug == dto.tenantSlug))
        if existing:
            raise conflict("TENANT_SLUG_TAKEN", "Tenant slug is already taken")

        # Delete verification state so it cannot be reused
        redis.delete(f"signup_verified:{dto.adminEmail}")

        now = datetime.now(tz=timezone.utc)
        trial_end = now + timedelta(days=30)

        tenant = Tenant(
            slug=dto.tenantSlug,
            name=dto.tenantName,
            plan=dto.plan.value,
            status="PROVISIONING",  # Keep as provisioning so DB can be seeded
        )
        self.db.add(tenant)
        self.db.flush()

        admin = User(
            tenantId=tenant.id,
            email=dto.adminEmail,
            name=dto.adminName,
            passwordHash=hash_password(dto.password),
            role="ADMIN",
            status="INACTIVE",  # Inactive until approved
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

        log.info("signup registered and auto-activated tenant=%s admin=%s", tenant.slug, admin.email)

        access, refresh = self._issue_and_store(admin)
        self.db.commit()

        return {
            "pendingApproval": False,
            "accessToken": access,
            "refreshToken": refresh,
            "tenant": {
                "id": tenant.id,
                "slug": tenant.slug,
                "name": tenant.name,
                "plan": tenant.plan,
            },
            "user": {
                "id": admin.id,
                "name": admin.name,
                "email": admin.email,
                "role": admin.role,
            },
        }

    # ── Login ────────────────────────────────────────────────────────────────
    def login(self, dto: LoginRequest, tenant_slug: str | None) -> dict:
        if tenant_slug:
            tenant = self.db.scalar(select(Tenant).where(Tenant.slug == tenant_slug))
            if not tenant:
                raise bad_request("INVALID_WORKSPACE", "Invalid workspace slug. Workspace does not exist.")

        # Check if the user exists but is soft-deleted
        deleted_user_stmt = select(User)
        if tenant_slug:
            deleted_user_stmt = deleted_user_stmt.join(User.tenant).where(Tenant.slug == tenant_slug)
        deleted_user = self.db.scalar(deleted_user_stmt.where(User.email == dto.email, User.deletedAt.isnot(None)))
        if deleted_user:
            raise bad_request(
                "ACCOUNT_DELETED",
                "Your account has been deleted by your organization. Please contact your organization administrator to restore access."
            )

        stmt = select(User).options(selectinload(User.tenant)).where(User.email == dto.email, User.deletedAt.is_(None))
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
            user_anywhere = self.db.scalar(select(User).where(User.email == dto.email, User.deletedAt.is_(None)))
            if tenant_slug and user_anywhere:
                raise bad_request("INVALID_WORKSPACE_SLUG", "Invalid workspace slug for this account")

            log.warning(
                "login failed reason=INVALID_CREDENTIALS email=%s tenantSlug=%s",
                dto.email,
                tenant_slug,
            )
            raise unauthorized("INVALID_CREDENTIALS", "Invalid email or password")

        if user.tenant and user.tenant.status == "ACTIVE" and user.status == UserStatus.INACTIVE.value:
            user.status = UserStatus.ACTIVE.value
            self.db.commit()

        if user.status == UserStatus.INACTIVE.value:
            raise forbidden("ACCOUNT_SUSPENDED", "Account is deactivated")

        if user.tenant.status in ("SUSPENDED", "CANCELLED"):
            raise forbidden("ACCOUNT_SUSPENDED", "Tenant account is suspended")

        if not verify_password(dto.password, user.passwordHash):
            raise unauthorized("INVALID_CREDENTIALS", "Invalid email or password")

        # Bypass 2FA if requested
        if dto.skip2fa:
            log.info("Bypassing 2FA for user=%s tenant=%s", user.email, user.tenantId)
            return self._complete_login(user)

        # Generate MFA OTP and session
        redis = get_redis()
        if redis is None:
            raise bad_request("FEATURE_UNAVAILABLE", "Redis is required for MFA.")

        otp = str(random.randint(100000, 999999))
        mfa_token = random_token_hex(32)
        redis.set(f"mfa_session:{mfa_token}", json.dumps({"userId": user.id, "otp": otp, "confirmLogout": dto.confirmLogout}), ex=300)
        log.info("OTP generated for user %s: %s", user.email, otp)

        try:
            notify_service.send_notification(
                self.db,
                template="mfa_otp",
                to=user.email,
                context={"otp": otp},
                tenant_id=user.tenantId,
            )
        except Exception as e:
            log.error("Failed to send verification OTP email: %s", e)

        log.info("login mfa_required user=%s tenant=%s", user.email, user.tenantId)
        return {
            "mfaRequired": True,
            "mfaToken": mfa_token,
        }

    # ── Verify MFA ────────────────────────────────────────────────────────────
    def verify_mfa(self, dto: VerifyMfaRequest) -> dict:
        redis = get_redis()
        if redis is None:
            raise bad_request("FEATURE_UNAVAILABLE", "Redis is required for MFA.")

        session_data_bytes = redis.get(f"mfa_session:{dto.mfaToken}")
        if not session_data_bytes:
            raise bad_request("INVALID_MFA_TOKEN", "The verification code (OTP) has expired or is invalid. Please request a new code.")

        session_data = json.loads(
            session_data_bytes.decode("utf-8")
            if isinstance(session_data_bytes, bytes)
            else session_data_bytes
        )

        if session_data.get("otp") != dto.otp:
            raise unauthorized("INVALID_OTP", "Invalid OTP entered.")

        redis.delete(f"mfa_session:{dto.mfaToken}")

        user_id = session_data.get("userId")
        user = self.db.scalar(
            select(User).options(selectinload(User.tenant)).where(User.id == user_id)
        )
        if not user:
            raise unauthorized("USER_NOT_FOUND", "User not found.")

        if user.tenant and user.tenant.status == "ACTIVE" and user.status == UserStatus.INACTIVE.value:
            user.status = UserStatus.ACTIVE.value
            self.db.commit()

        if user.status == UserStatus.INACTIVE.value:
            raise forbidden("ACCOUNT_SUSPENDED", "Account is deactivated")

        if user.tenant.status in ("SUSPENDED", "CANCELLED"):
            raise forbidden("ACCOUNT_SUSPENDED", "Tenant account is suspended")

        confirm_logout = session_data.get("confirmLogout", False)
        return self._complete_login(user, confirm_logout=confirm_logout)

    def _complete_login(self, user: User, confirm_logout: bool = False) -> dict:
        now = datetime.now(tz=timezone.utc)

        if confirm_logout:
            from sqlalchemy import update
            self.db.execute(
                update(RefreshToken)
                .where(
                    RefreshToken.userId == user.id,
                    RefreshToken.revokedAt.is_(None)
                )
                .values(revokedAt=now)
            )
            self.db.commit()
            log.info("Revoked previous active refresh tokens for user=%s", user.email)

        prev_login = user.lastLoginAt
        if prev_login is not None and prev_login.tzinfo is None:
            prev_login = prev_login.replace(tzinfo=timezone.utc)
        user.lastLoginAt = now

        # Monthly active user metric
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

        from sqlalchemy import update

        # Fetch the token to inspect its status
        existing = self.db.scalar(
            select(RefreshToken).where(RefreshToken.tokenHash == token_hash)
        )
        if not existing:
            raise unauthorized("TOKEN_EXPIRED", "Refresh token is expired or invalid")

        # Reuse Detection: if the token is already revoked, revoke all tokens for this user
        if existing.revokedAt is not None:
            self.db.execute(
                update(RefreshToken)
                .where(
                    RefreshToken.userId == existing.userId,
                    RefreshToken.revokedAt.is_(None),
                )
                .values(revokedAt=now)
            )
            self.db.commit()
            raise unauthorized("TOKEN_REUSE_DETECTED", "Refresh token reuse detected. All sessions revoked.")

        if existing.expiresAt <= now:
            raise unauthorized("TOKEN_EXPIRED", "Refresh token is expired")

        # Mark the token as used/revoked
        existing.revokedAt = now

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
        otp = str(random.randint(100000, 999999))
        log.info("Password Reset OTP generated for user %s: %s", email, otp)

        redis = get_redis()
        if redis is None:
            log.warning("forgot_password skipped — Redis disabled")
            return

        redis.set(
            f"pwd_reset:{token_hash}",
            json.dumps({"userId": user.id, "otp": otp}),
            ex=PASSWORD_RESET_TTL_S,
        )
        redis.set(
            f"pwd_reset:email:{email}",
            json.dumps({"userId": user.id, "otp": otp}),
            ex=PASSWORD_RESET_TTL_S,
        )

        reset_url = f"{self.settings.WEB_URL}/reset-password?token={token}"
        try:
            notify_service.send_notification(
                self.db,
                template="password_reset",
                to=email,
                context={"resetUrl": reset_url, "otp": otp, "name": user.name},
                tenant_id=user.tenantId,
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

        session_data_bytes = None
        token_hash = None
        if "@" in dto.token:
            session_data_bytes = redis.get(f"pwd_reset:email:{dto.token}")
        else:
            token_hash = sha256_hex(dto.token)
            session_data_bytes = redis.get(f"pwd_reset:{token_hash}")

        if not session_data_bytes:
            raise bad_request("INVALID_RESET_TOKEN", "Reset token or email is invalid or expired")

        session_data = json.loads(
            session_data_bytes.decode("utf-8")
            if isinstance(session_data_bytes, bytes)
            else session_data_bytes
        )

        user_id = session_data.get("userId")
        user = self.db.get(User, user_id)
        if not user:
            raise bad_request("INVALID_RESET_TOKEN", "Reset token is invalid or expired")
        user.passwordHash = hash_password(dto.password)

        if "@" in dto.token:
            redis.delete(f"pwd_reset:email:{dto.token}")
        else:
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
        
        allowed_emails = [e.strip().lower() for e in self.settings.SUPER_ADMIN_EMAILS.split(",") if e.strip()]
        is_sa = user.role == "ADMIN" and user.email.lower() in allowed_emails

        return {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
            "status": user.status,
            "tenantId": user.tenantId,
            "lastLoginAt": user.lastLoginAt,
            "isSuperAdmin": is_sa,
        }

    # ── Signup OTP & Approval ──────────────────────────────────────────────────
    def signup_send_otp(self, email: str) -> None:
        otp = str(random.randint(100000, 999999))
        log.info("Signup OTP generated for email %s: %s", email, otp)
        redis = get_redis()
        if redis is None:
            raise bad_request("FEATURE_UNAVAILABLE", "Redis is required for email verification.")
        redis.set(f"signup_otp:{email}", otp, ex=300)  # 5 minutes

        try:
            notify_service.send_notification(
                self.db,
                template="signup_otp",
                to=email,
                context={"otp": otp},
            )
        except Exception as e:
            log.error("notify failed: %s", e)

    def signup_verify_otp(self, email: str, otp: str) -> None:
        redis = get_redis()
        if redis is None:
            raise bad_request("FEATURE_UNAVAILABLE", "Redis is required for email verification.")
        saved = redis.get(f"signup_otp:{email}")
        if not saved:
            raise bad_request("INVALID_OTP", "OTP code is expired or invalid. Please request a new one.")
        saved_str = saved.decode("utf-8") if isinstance(saved, bytes) else saved
        if saved_str != otp:
            raise bad_request("INVALID_OTP", "Invalid OTP code entered.")

        redis.delete(f"signup_otp:{email}")
        redis.set(f"signup_verified:{email}", "true", ex=900)  # 15 minutes

    def approve_tenant(self, tenant_id: str, token: str) -> None:
        redis = get_redis()
        if redis is None:
            raise bad_request("FEATURE_UNAVAILABLE", "Redis is required for tenant approval.")

        saved = redis.get(f"tenant_approval:{tenant_id}")
        if not saved:
            raise bad_request("INVALID_TOKEN", "Approval link is invalid or expired.")
        saved_str = saved.decode("utf-8") if isinstance(saved, bytes) else saved
        if saved_str != token:
            raise bad_request("INVALID_TOKEN", "Approval link is invalid or expired.")

        tenant = self.db.get(Tenant, tenant_id)
        if not tenant:
            raise bad_request("TENANT_NOT_FOUND", "Tenant not found.")

        # Approve tenant and user
        tenant.status = "ACTIVE"

        admin_user = self.db.scalar(
            select(User).where(User.tenantId == tenant_id, User.role == "ADMIN")
        )
        if admin_user:
            admin_user.status = "ACTIVE"

        redis.delete(f"tenant_approval:{tenant_id}")
        self.db.commit()

        # Send approval notification to registered user
        if admin_user:
            try:
                login_url = f"{self.settings.WEB_URL}/login"
                notify_service.send_notification(
                    self.db,
                    template="tenant_approved",
                    to=admin_user.email,
                    context={"tenantName": tenant.name, "loginUrl": login_url, "name": admin_user.name},
                    tenant_id=tenant.id,
                )
            except Exception as e:
                log.error("Failed to send approval mail to user: %s", e)

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

