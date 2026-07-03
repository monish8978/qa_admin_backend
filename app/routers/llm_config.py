"""LLM provider configuration — /api/v1/llm-config."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..common.enums import UserRole
from ..common.exceptions import not_found
from ..common.responses import build_response
from ..deps import get_db, get_request_id, require_roles
from ..schemas.settings import UpsertLlmConfigRequest
from ..services import llm_config_service as svc

router = APIRouter(prefix="/llm-config", tags=["llm-config"])
log = logging.getLogger("qa.api.routers.llm_config")


@router.get("")
def get_config(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Fetching LLM config for tenant: %s", rid, tenant_id)
    cfg = svc.get_config(db, tenant_id)
    if cfg is None:
        log.warning("[%s] LLM configuration not found for tenant: %s", rid, tenant_id)
        raise not_found("LLM_CONFIG_NOT_FOUND", "LLM configuration not found")
    log.info("[%s] Successfully retrieved LLM config for tenant: %s", rid, tenant_id)
    return build_response(svc.to_public(cfg), rid)


@router.put("")
def upsert_config(
    body: UpsertLlmConfigRequest,
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info(
        "[%s] Upserting LLM config for tenant: %s (provider=%s, model=%s)",
        rid,
        tenant_id,
        body.provider,
        body.model,
    )
    cfg = svc.upsert_config(
        db,
        tenant_id,
        provider=body.provider,
        model=body.model,
        api_key=body.apiKey,
        endpoint=body.endpoint,
        enabled=body.enabled,
        backup_provider=body.backupProvider,
        backup_model=body.backupModel,
        backup_api_key=body.backupApiKey,
        max_tokens=body.maxTokens,
        temperature=body.temperature,
    )
    log.info("[%s] Successfully upserted LLM config for tenant: %s", rid, tenant_id)
    return build_response(svc.to_public(cfg), rid)


@router.post("/test")
def test_connectivity(
    payload: Annotated[dict, Depends(require_roles(UserRole.ADMIN))],
    db: Annotated[Session, Depends(get_db)],
    rid: Annotated[str, Depends(get_request_id)],
):
    tenant_id = payload["tenantId"]
    log.info("[%s] Testing LLM connectivity for tenant: %s", rid, tenant_id)
    res = svc.test_connectivity(db, tenant_id)
    log.info(
        "[%s] Completed LLM connectivity test for tenant: %s (status=%s)",
        rid,
        tenant_id,
        res.get("status"),
    )
    return build_response(res, rid)

