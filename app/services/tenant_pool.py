"""Per-tenant Postgres connection pool.

Mirrors apps/api/src/database/tenant-connection-pool.service.ts:
  - lazily resolves a tenant's DB connection info from the master DB
  - decrypts dbPasswordEnc (supports PLAINTEXT: dev prefix)
  - builds an SQLAlchemy Engine sized to PLAN_LIMITS[plan][dbPoolSize]
  - reaps idle engines after 30 minutes
  - emits a 1-hour Redis heartbeat (pool:tenant:<id>) when redis is enabled
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import quote_plus

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..common.encryption import decrypt_db_password
from ..common.enums import PLAN_LIMITS, PlanType
from ..common.exceptions import not_found
from ..db import SessionLocal
from ..models.master import Tenant
from ..redis_client import get_redis

log = logging.getLogger("qa.tenant_pool")

_IDLE_REAP_SECONDS = 30 * 60  # 30 minutes
_HEARTBEAT_TTL = 60 * 60      # 1 hour


@dataclass
class _PoolEntry:
    engine: Engine
    sessionmaker: sessionmaker
    last_used: float


class TenantPool:
    def __init__(self) -> None:
        self._entries: dict[str, _PoolEntry] = {}
        self._lock = threading.Lock()

    # -------- public API ---------------------------------------------------

    def get_engine(self, tenant_id: str) -> Engine:
        return self._get_entry(tenant_id).engine

    @contextmanager
    def session(self, tenant_id: str) -> Iterator[Session]:
        entry = self._get_entry(tenant_id)
        session = entry.sessionmaker()
        try:
            yield session
        finally:
            session.close()

    def evict(self, tenant_id: str) -> None:
        with self._lock:
            entry = self._entries.pop(tenant_id, None)
        if entry is not None:
            try:
                entry.engine.dispose()
            except Exception:  # noqa: BLE001
                log.warning("evict: failed to dispose engine for %s", tenant_id, exc_info=True)

    def reap_idle(self, idle_seconds: int = _IDLE_REAP_SECONDS) -> int:
        cutoff = time.time() - idle_seconds
        evicted = 0
        with self._lock:
            stale = [tid for tid, e in self._entries.items() if e.last_used < cutoff]
        for tid in stale:
            self.evict(tid)
            evicted += 1
        if evicted:
            log.info("reap_idle: evicted %s tenant pools", evicted)
        return evicted

    def dispose_all(self) -> None:
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for e in entries:
            try:
                e.engine.dispose()
            except Exception:  # noqa: BLE001
                pass

    # -------- internal -----------------------------------------------------

    def _get_entry(self, tenant_id: str) -> _PoolEntry:
        with self._lock:
            entry = self._entries.get(tenant_id)
            if entry is not None:
                entry.last_used = time.time()
                self._touch_heartbeat(tenant_id)
                return entry

        # Build outside the lock to avoid blocking other tenants.
        entry = self._build_entry(tenant_id)
        with self._lock:
            existing = self._entries.get(tenant_id)
            if existing is not None:
                # Another thread won; discard ours.
                try:
                    entry.engine.dispose()
                except Exception:  # noqa: BLE001
                    pass
                existing.last_used = time.time()
                return existing
            self._entries[tenant_id] = entry
        self._touch_heartbeat(tenant_id)
        return entry

    def _build_entry(self, tenant_id: str) -> _PoolEntry:
        with SessionLocal() as master:
            tenant = master.execute(
                select(Tenant).where(Tenant.id == tenant_id)
            ).scalar_one_or_none()
            if tenant is None:
                raise not_found("TENANT_NOT_FOUND", f"Tenant {tenant_id} not found")
            
            if not tenant.dbPasswordEnc:
                from .provision_task import tenant_provision
                from ..models.master import User
                admin_user = master.scalar(
                    select(User).where(User.tenantId == tenant_id, User.role == "ADMIN")
                )
                admin_user_id = admin_user.id if admin_user else "system"
                log.info("Auto-provisioning tenant %s dynamically on connection demand", tenant_id)
                try:
                    tenant_provision(tenantId=tenant_id, adminUserId=admin_user_id)
                    master.refresh(tenant)
                except Exception as e:
                    log.error("Failed to dynamically provision tenant %s: %s", tenant_id, e)
            if tenant.status != "ACTIVE":
                log.warning("tenant_pool: tenant %s status=%s", tenant_id, tenant.status)
                if tenant.status == "PROVISIONING":
                    # Allow connecting during provisioning (e.g. for seeding/migrations) ONLY if DB details are ready.
                    if not tenant.dbName or not tenant.dbPasswordEnc:
                        from ..common.exceptions import CodedHTTPException
                        from fastapi import status
                        raise CodedHTTPException(
                            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            code="TENANT_PROVISIONING",
                            message="Tenant database is still being provisioned. Please try again in a few seconds."
                        )
                elif tenant.status in ("SUSPENDED", "CANCELLED"):
                    from ..common.exceptions import forbidden
                    raise forbidden("ACCOUNT_SUSPENDED", f"Tenant account is {tenant.status.lower()}")
                else:
                    from ..common.exceptions import bad_request
                    raise bad_request("TENANT_INACTIVE", f"Tenant is not active (status: {tenant.status})")

            password = decrypt_db_password(tenant.dbPasswordEnc)
            plan = tenant.plan
            host, port, db, user = (
                tenant.dbHost, tenant.dbPort, tenant.dbName, tenant.dbUser,
            )

        pool_size = PLAN_LIMITS.get(
            PlanType(plan) if plan in {p.value for p in PlanType} else PlanType.BASIC,
            PLAN_LIMITS[PlanType.BASIC],
        )["dbPoolSize"]
        
        # Override localhost to postgres so the Docker container can reach the database service
        if host in ("127.0.0.1", "localhost", "host.docker.internal"):
            host = "postgres"
            
        url = f"postgresql+psycopg://{user}:{quote_plus(password)}@{host}:{port}/{db}"
        engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=pool_size,
            max_overflow=max(pool_size, 5),
            pool_recycle=1800,
        )
        sm = sessionmaker(bind=engine, expire_on_commit=False)
        log.info(
            "tenant_pool: opened pool for tenant=%s host=%s db=%s pool_size=%s",
            tenant_id, host, db, pool_size,
        )
        return _PoolEntry(engine=engine, sessionmaker=sm, last_used=time.time())

    def _touch_heartbeat(self, tenant_id: str) -> None:
        r = get_redis()
        if r is None:
            return
        try:
            r.set(f"pool:tenant:{tenant_id}", "1", ex=_HEARTBEAT_TTL)
        except Exception:  # noqa: BLE001
            log.debug("redis heartbeat failed", exc_info=True)


@lru_cache(maxsize=1)
def get_tenant_pool() -> TenantPool:
    return TenantPool()


# ---------- FastAPI dependency ------------------------------------------------

def tenant_session_dep(tenant_id_payload_key: str = "tenantId"):
    """Returns a dependency that yields a tenant Session from the JWT payload."""
    from ..deps import get_current_payload  # local to avoid cycle
    from fastapi import Depends

    def _dep(payload: dict = Depends(get_current_payload)) -> Iterator[Session]:  # noqa: B008
        tid = payload[tenant_id_payload_key]
        pool = get_tenant_pool()
        with pool.session(tid) as s:
            yield s

    return _dep

