"""Lazy Redis client. Returns None when REDIS_ENABLED=false."""
from __future__ import annotations

from functools import lru_cache

import redis

from .config import get_settings


@lru_cache(maxsize=1)
def get_redis() -> redis.Redis | None:
    s = get_settings()
    if not s.redis_enabled:
        return None
    return redis.Redis(
        host=s.REDIS_HOST,
        port=s.REDIS_PORT,
        password=s.REDIS_PASSWORD or None,
        decode_responses=True,
        socket_connect_timeout=3,
    )
