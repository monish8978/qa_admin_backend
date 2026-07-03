"""FastAPI dependencies: DB session, current user, role guard, request id."""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from .common.enums import UserRole
from .common.exceptions import forbidden, unauthorized
from .config import get_settings
from .db import SessionLocal

_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/v1/auth/login", auto_error=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_request_id(
    request: Request,
    x_request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
) -> str:
    rid = x_request_id or getattr(request.state, "request_id", None)
    if rid:
        return rid
    rid = uuid.uuid4().hex
    request.state.request_id = rid
    return rid


def get_current_payload(
    token: Annotated[str | None, Depends(_oauth2_scheme)],
) -> dict:
    from .security import verify_jwt  # local import to avoid cycles

    if not token:
        raise unauthorized("UNAUTHENTICATED", "Missing bearer token")
    try:
        payload = verify_jwt(token, get_settings().JWT_SECRET)
    except ValueError:
        raise unauthorized("TOKEN_EXPIRED", "Access token is expired or invalid") from None
    if payload.get("type") != "access":
        raise unauthorized("INVALID_TOKEN_TYPE", "Invalid token type")
    return payload


def require_roles(*roles: UserRole):
    allowed = {r.value for r in roles}

    def _checker(payload: Annotated[dict, Depends(get_current_payload)]) -> dict:
        if payload.get("role") not in allowed:
            raise forbidden(
                "INSUFFICIENT_ROLE",
                f"This action requires one of: {', '.join(sorted(allowed))}",
            )
        return payload

    return _checker
