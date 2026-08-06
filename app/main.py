"""FastAPI application entrypoint.

Run with:   uvicorn app.main:app --reload --port 3000

Mirrors apps/api/src/main.ts:
  - global prefix /api/v1
  - CORS
  - request-id middleware
  - structured error response envelope
  - swagger at /api/docs in non-production
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from .common.metrics import record_http_request
from .common.queue_metrics import run_forever as run_queue_metrics
from .config import get_settings
from .routers import (
    analytics,
    auth,
    billing,
    conversations,
    departments,
    evaluations,
    forms,
    health,
    llm_config,
    outbound_webhooks,
    platform_admin,
    routing,
    tenant_settings,
    users,
    webhooks_ingest,
)
from .services.tenant_pool import get_tenant_pool

from .logger import setup_app_logging
setup_app_logging()
log = logging.getLogger("qa.api")

settings = get_settings()


def _sanitize_validation_details(details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for err in details:
        item: dict[str, Any] = dict(err)
        ctx = item.get("ctx")
        if isinstance(ctx, dict):
            item["ctx"] = {
                k: (v if isinstance(v, (str, int, float, bool, type(None))) else str(v))
                for k, v in ctx.items()
            }
        sanitized.append(item)
    return sanitized


def _build_app() -> FastAPI:
    is_prod = settings.NODE_ENV == "production"
    app = FastAPI(
        title="QA Platform API",
        description="Multi-tenant SaaS QA evaluation platform — Python port",
        version="1.0.0",
        docs_url=None if is_prod else "/api/docs",
        redoc_url=None,
        openapi_url=None if is_prod else "/api/v1/openapi.json",
    )

    # In production, allow the deployed web app (WEB_URL) and the API origin.
    # Using "*" with allow_credentials=True is invalid per the CORS spec, so we
    # enumerate explicit origins instead.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"https?://.*",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    class RequestIdMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex
            request.state.request_id = rid
            response = await call_next(request)
            response.headers["X-Request-Id"] = rid
            return response

    app.add_middleware(RequestIdMiddleware)

    class MetricsMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            start = time.perf_counter()
            response = await call_next(request)
            duration_ms = (time.perf_counter() - start) * 1000.0
            # Prefer the matched route template (e.g. /api/v1/forms/{form_id}) over the raw
            # request path to avoid label cardinality explosions.
            route = request.scope.get("route")
            route_path = getattr(route, "path", None) or request.url.path
            if route_path == "/api/v1/health/metrics":
                return response
            try:
                record_http_request(request.method, route_path, response.status_code, duration_ms)
            except Exception:  # noqa: BLE001
                log.debug("metrics record failed", exc_info=True)
            return response

    app.add_middleware(MetricsMiddleware)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        rid = getattr(request.state, "request_id", uuid.uuid4().hex)
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            body = {
                "error": detail,
                "meta": {"requestId": rid, "timestamp": _now_iso()},
            }
        else:
            body = {
                "error": {"code": "HTTP_ERROR", "message": str(detail)},
                "meta": {"requestId": rid, "timestamp": _now_iso()},
            }
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        rid = getattr(request.state, "request_id", uuid.uuid4().hex)
        details = _sanitize_validation_details(exc.errors())
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Invalid request payload",
                    "details": details,
                },
                "meta": {"requestId": rid, "timestamp": _now_iso()},
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled_error(request: Request, exc: Exception):
        rid = getattr(request.state, "request_id", uuid.uuid4().hex)
        log.exception("Unhandled API error requestId=%s path=%s", rid, request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_SERVER_ERROR",
                    "message": "An unexpected error occurred. Please try again.",
                },
                "meta": {"requestId": rid, "timestamp": _now_iso()},
            },
        )

    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(users.router, prefix="/api/v1")
    app.include_router(departments.router, prefix="/api/v1")
    app.include_router(tenant_settings.router, prefix="/api/v1")
    app.include_router(llm_config.router, prefix="/api/v1")
    app.include_router(forms.router, prefix="/api/v1")
    app.include_router(conversations.router, prefix="/api/v1")
    app.include_router(evaluations.router, prefix="/api/v1")
    app.include_router(platform_admin.router, prefix="/api/v1")
    app.include_router(routing.router, prefix="/api/v1")
    app.include_router(billing.router, prefix="/api/v1")
    app.include_router(outbound_webhooks.router, prefix="/api/v1")
    app.include_router(webhooks_ingest.router, prefix="/api/v1")
    app.include_router(analytics.router, prefix="/api/v1")
    app.include_router(health.router, prefix="/api/v1")

    _reaper_task: dict[str, object] = {}
    _queue_metrics_stop: dict[str, object] = {}

    @app.on_event("startup")
    async def _startup():
        log.info("API running on port %s [%s]", settings.PORT, settings.NODE_ENV)
        
        # Auto-create master database schema using SQLAlchemy models
        from .db import engine
        from .models.master import Base
        from sqlalchemy import text
        try:
            Base.metadata.create_all(engine)
            with engine.begin() as conn:
                conn.execute(text('ALTER TABLE tenants ADD COLUMN IF NOT EXISTS "featureFlags" JSONB DEFAULT \'{}\'::jsonb'))
                conn.execute(text('ALTER TABLE tenants ADD COLUMN IF NOT EXISTS "pendingPlan" "PlanType"'))
                conn.execute(text('ALTER TABLE tenants ADD COLUMN IF NOT EXISTS "deletedAt" TIMESTAMP WITH TIME ZONE NULL'))
                conn.execute(text('ALTER TABLE blind_review_settings ADD COLUMN IF NOT EXISTS "bestThreshold" DOUBLE PRECISION DEFAULT 90.0'))
                conn.execute(text('ALTER TABLE blind_review_settings ADD COLUMN IF NOT EXISTS "goodThreshold" DOUBLE PRECISION DEFAULT 75.0'))
                conn.execute(text('ALTER TABLE blind_review_settings ADD COLUMN IF NOT EXISTS "avgThreshold" DOUBLE PRECISION DEFAULT 60.0'))
                conn.execute(text('ALTER TABLE blind_review_settings ADD COLUMN IF NOT EXISTS "poorThreshold" DOUBLE PRECISION DEFAULT 0.0'))
                conn.execute(text('ALTER TABLE blind_review_settings ADD COLUMN IF NOT EXISTS "scoreBuckets" JSONB DEFAULT \'[]\'::jsonb'))
                conn.execute(text('ALTER TABLE users ADD COLUMN IF NOT EXISTS "deletedAt" TIMESTAMP WITH TIME ZONE NULL'))
                
                # Create PlatformPlan table if not exists and seed it
                conn.execute(text('''
                    CREATE TABLE IF NOT EXISTS platform_plans (
                        id VARCHAR(25) PRIMARY KEY,
                        code VARCHAR(50) UNIQUE NOT NULL,
                        name VARCHAR(100) NOT NULL,
                        description TEXT,
                        "priceMonthly" INTEGER NOT NULL DEFAULT 0,
                        "priceYearly" INTEGER NOT NULL DEFAULT 0,
                        "conversationsLimit" INTEGER,
                        "formsLimit" INTEGER,
                        "usersLimit" INTEGER,
                        "features" JSONB NOT NULL DEFAULT '[]'::jsonb,
                        "isActive" BOOLEAN NOT NULL DEFAULT TRUE,
                        "createdAt" TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        "updatedAt" TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    )
                '''))
                
                # Ensure features column exists on already created tables
                conn.execute(text('ALTER TABLE platform_plans ADD COLUMN IF NOT EXISTS "features" JSONB NOT NULL DEFAULT \'[]\'::jsonb'))
                
                # Insert default plans if table is empty
                conn.execute(text('''
                    INSERT INTO platform_plans (id, code, name, "priceMonthly", "priceYearly", "conversationsLimit", "formsLimit", "usersLimit", "features", "isActive")
                    SELECT 'plan_basic', 'BASIC', 'Free Plan', 0, 0, 500, 3, 5, '["500 Conversations / month", "3 Form Templates", "5 Users & Team Members", "Standard QA Review Queue", "Basic Dashboard Analytics"]'::jsonb, true
                    WHERE NOT EXISTS (SELECT 1 FROM platform_plans WHERE code = 'BASIC')
                '''))
                conn.execute(text('''
                    INSERT INTO platform_plans (id, code, name, "priceMonthly", "priceYearly", "conversationsLimit", "formsLimit", "usersLimit", "features", "isActive")
                    SELECT 'plan_pro', 'PRO', 'Pro Plan', 49, 490, 5000, 20, 25, '["5,000 Conversations / month", "20 Form Templates", "25 Users & Team Members", "Advanced AI Evaluation & LLM", "Escalation & Audit Queues"]'::jsonb, true
                    WHERE NOT EXISTS (SELECT 1 FROM platform_plans WHERE code = 'PRO')
                '''))
                conn.execute(text('''
                    INSERT INTO platform_plans (id, code, name, "priceMonthly", "priceYearly", "conversationsLimit", "formsLimit", "usersLimit", "features", "isActive")
                    SELECT 'plan_ent', 'ENTERPRISE', 'Enterprise', 199, 1990, NULL, NULL, NULL, '["Unlimited Conversations", "Unlimited Form Templates", "Unlimited Users & Team Members", "Custom LLM Endpoint & Azure OpenAI", "24/7 Dedicated Account Support"]'::jsonb, true
                    WHERE NOT EXISTS (SELECT 1 FROM platform_plans WHERE code = 'ENTERPRISE')
                '''))

                # Increase id length for new tables in case they were already created with length 25
                conn.execute(text('ALTER TABLE platform_audit_logs ALTER COLUMN id TYPE VARCHAR(50)'))
                conn.execute(text('ALTER TABLE platform_notifications ALTER COLUMN id TYPE VARCHAR(50)'))

            log.info("Master database schema initialized successfully")
        except Exception:
            log.warning("Failed to initialize master database schema", exc_info=True)

        # Auto-provision super admin if environment variables are set
        if settings.AUTO_PROVISION_ADMIN_EMAIL and settings.AUTO_PROVISION_ADMIN_TENANT_SLUG:
            from .db import SessionLocal
            from .models.master import Tenant, User, Subscription
            from .security import hash_password
            from datetime import datetime, timezone

            session = SessionLocal()
            try:
                # Check if Tenant exists
                tenant = session.query(Tenant).filter(Tenant.slug == settings.AUTO_PROVISION_ADMIN_TENANT_SLUG).first()
                if not tenant:
                    log.info("Auto-provisioning workspace '%s' (slug: %s)...", settings.AUTO_PROVISION_ADMIN_TENANT_NAME, settings.AUTO_PROVISION_ADMIN_TENANT_SLUG)
                    tenant = Tenant(
                        slug=settings.AUTO_PROVISION_ADMIN_TENANT_SLUG,
                        name=settings.AUTO_PROVISION_ADMIN_TENANT_NAME,
                        plan="ENTERPRISE",
                        status="ACTIVE"
                    )
                    session.add(tenant)
                    session.flush()

                    # Create subscription
                    now = datetime.now(timezone.utc)
                    subscription = Subscription(
                        tenantId=tenant.id,
                        plan="ENTERPRISE",
                        status="ACTIVE",
                        currentPeriodStart=now,
                        currentPeriodEnd=now,
                    )
                    session.add(subscription)
                    session.flush()
                
                # Check if User exists
                user = session.query(User).filter(User.tenantId == tenant.id, User.email == settings.AUTO_PROVISION_ADMIN_EMAIL).first()
                if user:
                    log.info("Super admin user '%s' already exists. Updating credentials/role...", settings.AUTO_PROVISION_ADMIN_EMAIL)
                    user.role = "ADMIN"
                    user.status = "ACTIVE"
                    if not user.passwordHash and settings.AUTO_PROVISION_ADMIN_PASSWORD:
                        user.passwordHash = hash_password(settings.AUTO_PROVISION_ADMIN_PASSWORD)
                else:
                    log.info("Auto-provisioning Super Admin user '%s'...", settings.AUTO_PROVISION_ADMIN_EMAIL)
                    user = User(
                        tenantId=tenant.id,
                        email=settings.AUTO_PROVISION_ADMIN_EMAIL,
                        name=settings.AUTO_PROVISION_ADMIN_NAME,
                        passwordHash=hash_password(settings.AUTO_PROVISION_ADMIN_PASSWORD or "SuperAdmin123!"),
                        role="ADMIN",
                        status="ACTIVE"
                    )
                    session.add(user)

                session.commit()
                log.info("Super Admin auto-provisioned successfully.")
            except Exception as e:
                session.rollback()
                log.warning("Failed to auto-provision Super Admin", exc_info=True)
            finally:
                session.close()

            # Run ALTER TYPE for AUDIT status on all tenant databases
            try:
                from .models.master import Tenant
                from sqlalchemy import text, select
                with SessionLocal() as session:
                    tenants = session.execute(select(Tenant.id)).scalars().all()
                    
                pool = get_tenant_pool()
                for tenant_id in tenants:
                    try:
                        engine = pool.get_engine(tenant_id)
                        with engine.connect() as conn:
                            try:
                                conn.execution_options(isolation_level="AUTOCOMMIT").execute(
                                    text('ALTER TYPE "ConvStatus" ADD VALUE \'AUDIT\';')
                                )
                            except Exception:
                                pass
                        
                        with engine.begin() as conn:
                            # Update conversations with open audit cases to 'AUDIT' status
                            conn.execute(
                                text(
                                    'UPDATE conversations SET status = \'AUDIT\', "updatedAt" = now() '
                                    'WHERE id IN ('
                                    '  SELECT e."conversationId" FROM evaluations e '
                                    '  JOIN audit_cases ac ON ac."evaluationId" = e.id '
                                    '  WHERE ac.status = \'OPEN\''
                                    ')'
                                )
                            )
                            # Update conversations with closed (RESOLVED/DISMISSED) audit cases to 'COMPLETED' status
                            conn.execute(
                                text(
                                    'UPDATE conversations SET status = \'COMPLETED\', "updatedAt" = now() '
                                    'WHERE id IN ('
                                    '  SELECT e."conversationId" FROM evaluations e '
                                    '  JOIN audit_cases ac ON ac."evaluationId" = e.id '
                                    '  WHERE ac.status IN (\'RESOLVED\', \'DISMISSED\')'
                                    ')'
                                )
                            )
                            log.info("Successfully added 'AUDIT' to ConvStatus enum and updated audit case conversations for tenant %s", tenant_id)
                    except Exception as te:
                        log.debug("Failed or skipped ALTER TYPE ConvStatus for tenant %s: %s", tenant_id, te)
            except Exception as e:
                log.warning("Failed to run ConvStatus enum migration: %s", e)

        import asyncio
        async def _reap_loop():
            while True:
                try:
                    get_tenant_pool().reap_idle()
                except Exception:  # noqa: BLE001
                    log.warning("tenant_pool reaper failed", exc_info=True)
                await asyncio.sleep(15 * 60)  # every 15 minutes
        _reaper_task["t"] = asyncio.create_task(_reap_loop())

        # Background queue-metrics collector — feeds the /health/metrics gauges.
        stop_event = asyncio.Event()
        _queue_metrics_stop["e"] = stop_event
        _queue_metrics_stop["t"] = asyncio.create_task(run_queue_metrics(stop_event))

    @app.on_event("shutdown")
    async def _shutdown():
        task = _reaper_task.get("t")
        if task is not None:
            task.cancel()  # type: ignore[union-attr]
        stop_event = _queue_metrics_stop.get("e")
        if stop_event is not None:
            stop_event.set()  # type: ignore[union-attr]
        qmt = _queue_metrics_stop.get("t")
        if qmt is not None:
            qmt.cancel()  # type: ignore[union-attr]
        try:
            get_tenant_pool().dispose_all()
        except Exception:  # noqa: BLE001
            pass

    return app


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


app = _build_app()
