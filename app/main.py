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
    prod_origins = list(dict.fromkeys(o for o in (settings.WEB_URL, settings.API_URL) if o))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=prod_origins if is_prod else ["*"],
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
        try:
            Base.metadata.create_all(engine)
            log.info("Master database schema initialized successfully")
        except Exception:
            log.warning("Failed to initialize master database schema", exc_info=True)

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
