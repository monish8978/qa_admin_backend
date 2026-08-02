"""FastAPI dependencies: DB session, current user, role guard, request id."""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from .common.enums import UserRole
from .common.exceptions import forbidden, unauthorized, too_many_requests
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


import time
import threading

_in_memory_limit_cache: dict[str, list[float]] = {}
_limit_cache_lock = threading.Lock()


def rate_limit_ip(limit: int, window_seconds: int):
    def dependency(request: Request) -> None:
        client_ip = request.client.host if request.client else "127.0.0.1"
        route_path = request.url.path

        from .redis_client import get_redis
        redis_client = get_redis()
        if redis_client:
            redis_key = f"rate:{route_path}:{client_ip}"
            try:
                # Use Redis pipeline for atomic zset rate limiting (sliding window)
                pipe = redis_client.pipeline()
                now = time.time()
                clear_before = now - window_seconds
                
                pipe.zremrangebyscore(redis_key, 0, clear_before)
                pipe.zcard(redis_key)
                pipe.zadd(redis_key, {str(now): now})
                pipe.expire(redis_key, window_seconds)
                _, card, _, _ = pipe.execute()
                
                if card >= limit:
                    raise too_many_requests(
                        "TOO_MANY_REQUESTS",
                        "Too many requests. Please try again later."
                    )
                return
            except Exception:
                # Redis failure fallback to memory cache
                pass

        # Fallback to local in-memory sliding window rate limiter
        now = time.time()
        key = f"{route_path}:{client_ip}"
        with _limit_cache_lock:
            timestamps = _in_memory_limit_cache.setdefault(key, [])
            while timestamps and timestamps[0] < now - window_seconds:
                timestamps.pop(0)
            
            if len(timestamps) >= limit:
                raise too_many_requests(
                    "TOO_MANY_REQUESTS",
                    "Too many requests. Please try again later."
                )
            timestamps.append(now)

    return dependency
