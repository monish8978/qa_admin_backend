"""Health + readiness endpoints."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..common.exceptions import forbidden
from ..common.metrics import metrics_content_type, render_metrics_text
from ..config import get_settings
from ..deps import get_db
from ..redis_client import get_redis

log = logging.getLogger("qa.health")

router = APIRouter(prefix="/health", tags=["Health"])


def _require_platform_admin(
    x_platform_admin_token: Annotated[str | None, Header(alias="X-Platform-Admin-Token")] = None,
) -> None:
    """Guard for cross-tenant diagnostic endpoints.

    Requires PLATFORM_ADMIN_TOKEN to be configured AND supplied. When the token
    is not configured the endpoint is disabled entirely (never open in prod).
    """
    expected = get_settings().PLATFORM_ADMIN_TOKEN
    if not expected or x_platform_admin_token != expected:
        raise forbidden("FORBIDDEN", "Platform admin token required")


@router.get("")
def liveness() -> dict:
    return {"status": "ok"}


@router.get("/ready")
def readiness(db: Annotated[Session, Depends(get_db)]) -> dict:
    checks: dict[str, str] = {}
    try:
        db.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception:  # pragma: no cover
        log.warning("readiness db check failed", exc_info=True)
        checks["db"] = "error"

    redis = get_redis()
    if redis is None:
        checks["redis"] = "disabled"
    else:
        try:
            redis.ping()
            checks["redis"] = "ok"
        except Exception:  # pragma: no cover
            log.warning("readiness redis check failed", exc_info=True)
            checks["redis"] = "error"

    status_value = "ok" if all(v in ("ok", "disabled") for v in checks.values()) else "degraded"
    return {"status": status_value, "checks": checks}


@router.get("/metrics")
def metrics() -> Response:
    """Prometheus scrape endpoint — mirrors Nest /api/v1/health/metrics."""
    return Response(content=render_metrics_text(), media_type=metrics_content_type())


@router.get("/diagnose", dependencies=[Depends(_require_platform_admin)])
def diagnose(db: Annotated[Session, Depends(get_db)]) -> dict:
    """Diagnostic endpoint to inspect LLM credentials and recent evaluation states.

    Cross-tenant diagnostic — restricted to platform admins via
    X-Platform-Admin-Token. Never returns tracebacks or raw error strings.
    """
    from app.models.master import Tenant
    from app.services.llm_config_service import test_connectivity
    from app.services.tenant_pool import get_tenant_pool
    from sqlalchemy import select, text

    diag: dict = {}
    try:
        tenants = db.execute(select(Tenant)).scalars().all()
        pool = get_tenant_pool()
        for t in tenants:
            diag[t.slug] = {"connectivity": None, "llm_config": None, "evaluations": []}
            try:
                diag[t.slug]["connectivity"] = test_connectivity(db, t.id)
            except Exception:
                log.warning("diagnose connectivity failed tenant=%s", t.slug, exc_info=True)
                diag[t.slug]["connectivity"] = {"error": "connectivity_check_failed"}

            try:
                from app.models.master import LlmConfig
                cfg = db.execute(select(LlmConfig).where(LlmConfig.tenantId == t.id)).scalar_one_or_none()
                if cfg:
                    diag[t.slug]["llm_config"] = {
                        "provider": cfg.provider,
                        "model": cfg.model,
                        "enabled": cfg.enabled,
                        "endpoint": cfg.endpoint,
                    }
            except Exception:
                log.warning("diagnose llm_config failed tenant=%s", t.slug, exc_info=True)
                diag[t.slug]["llm_config_error"] = "llm_config_lookup_failed"

            try:
                with pool.session(t.id) as ts:
                    evals = ts.execute(text(
                        'SELECT id, "workflowState", "aiScore", "createdAt" '
                        'FROM evaluations ORDER BY "createdAt" DESC LIMIT 10'
                    )).fetchall()
                    for ev in evals:
                        diag[t.slug]["evaluations"].append({
                            "id": ev[0],
                            "workflowState": ev[1],
                            "aiScore": ev[2],
                            "createdAt": ev[3].isoformat() if ev[3] else None,
                        })
            except Exception:
                log.warning("diagnose evaluations failed tenant=%s", t.slug, exc_info=True)
                diag[t.slug]["evaluations_error"] = "evaluations_lookup_failed"
    except Exception:
        log.exception("diagnose failed")
        return {"status": "error", "error": "diagnose_failed"}

    return {"status": "ok", "diagnostics": diag}

