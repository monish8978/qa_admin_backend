"""Password hashing, JWT signing/verification, token hashing.

Mirrors apps/api/src/auth/auth.service.ts:
  - bcrypt with cost 12
  - HS256 JWTs signed with JWT_SECRET (access/invite) and REFRESH_SECRET (refresh)
  - SHA-256 hash of raw refresh tokens stored in DB
"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from jose import JWTError, jwt
import bcrypt

# Monkey-patch bcrypt for passlib compatibility
if not hasattr(bcrypt, "__about__"):
    bcrypt.__about__ = type("about", (), {"__version__": bcrypt.__version__})

from passlib.context import CryptContext

from .config import get_settings

BCRYPT_ROUNDS = 12

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=BCRYPT_ROUNDS)


TokenType = Literal["access", "refresh", "invite", "reset"]


def hash_password(plain: str) -> str:
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return _pwd_context.verify(plain, hashed)
    except ValueError:
        return False


def sha256_hex(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_DURATION_RE = re.compile(r"^(\d+)([smhd])$")


def parse_duration(value: str) -> timedelta:
    """Parse strings like '15m', '30d', '3600' (seconds) — same forms Nest's `jsonwebtoken` accepts."""
    if value.isdigit():
        return timedelta(seconds=int(value))
    match = _DURATION_RE.match(value)
    if not match:
        raise ValueError(f"Unsupported duration string: {value!r}")
    n, unit = int(match.group(1)), match.group(2)
    return {
        "s": timedelta(seconds=n),
        "m": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
    }[unit]


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def sign_jwt(
    *,
    sub: str,
    tenant_id: str,
    role: str,
    token_type: TokenType,
    secret: str,
    expires_in: str,
) -> str:
    now = _now_utc()
    payload: dict[str, Any] = {
        "sub": sub,
        "tenantId": tenant_id,
        "role": role,
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + parse_duration(expires_in)).timestamp()),
        "jti": secrets.token_hex(16),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def verify_jwt(token: str, secret: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except JWTError as e:
        raise ValueError(str(e)) from e


def issue_token_pair(*, user_id: str, tenant_id: str, role: str) -> tuple[str, str]:
    s = get_settings()
    access = sign_jwt(
        sub=user_id,
        tenant_id=tenant_id,
        role=role,
        token_type="access",
        secret=s.JWT_SECRET,
        expires_in=s.JWT_EXPIRES_IN,
    )
    refresh = sign_jwt(
        sub=user_id,
        tenant_id=tenant_id,
        role=role,
        token_type="refresh",
        secret=s.REFRESH_SECRET,
        expires_in=s.REFRESH_EXPIRES_IN,
    )
    return access, refresh


def random_token_hex(nbytes: int = 32) -> str:
    return secrets.token_hex(nbytes)
