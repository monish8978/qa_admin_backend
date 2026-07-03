"""LLM provider configuration — mirrors apps/api/src/llm-config/llm-config.service.ts.

Stores per-tenant provider credentials (encrypted) and exposes a test endpoint
that performs a tiny chat-completion against the configured provider.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..common.encryption import decrypt, encrypt, mask_secret
from ..common.exceptions import bad_request, not_found
from ..models.master import LlmConfig

log = logging.getLogger("qa.llm_config")

VALID_PROVIDERS = {"OPENAI", "AZURE_OPENAI", "CUSTOM"}
_TEST_TIMEOUT = 12.0


def _validate_endpoint_shape(provider: str, endpoint: str | None) -> str | None:
    if provider == "OPENAI":
        return "https://api.openai.com/v1/chat/completions"
    if provider == "AZURE_OPENAI":
        if not endpoint:
            raise bad_request(
                "INVALID_ENDPOINT", "AZURE_OPENAI requires a base resource URL"
            )
        parsed = urlparse(endpoint)
        if not parsed.scheme or not parsed.netloc:
            raise bad_request("INVALID_ENDPOINT", "Endpoint must be a valid URL")
        if "/deployments/" in endpoint or "/chat/completions" in endpoint:
            raise bad_request(
                "INVALID_ENDPOINT",
                "Provide only the base Azure resource URL (e.g. https://my-resource.openai.azure.com)",
            )
        return endpoint.rstrip("/")
    if provider == "CUSTOM":
        if not endpoint:
            raise bad_request("INVALID_ENDPOINT", "CUSTOM provider requires an endpoint URL")
        parsed = urlparse(endpoint)
        if not parsed.scheme or not parsed.netloc:
            raise bad_request("INVALID_ENDPOINT", "Endpoint must be a valid URL")
        return endpoint.rstrip("/")
    raise bad_request("INVALID_PROVIDER", f"Unsupported provider: {provider}")


def _build_test_request(
    provider: str, endpoint: str, model: str, api_key: str
) -> tuple[str, dict[str, str], dict[str, Any]]:
    body = {
        "model": model,
        "max_tokens": 1,
        "temperature": 0,
        "messages": [{"role": "user", "content": "healthcheck"}],
    }
    # Only append HuggingFace custom proxy fields if using the local scalable proxy
    if endpoint and ("172.16.3.215:8000" in endpoint or "/chat/sync" in endpoint):
        body["query"] = "healthcheck"
        body["hf_model"] = model
        body["hf_token"] = api_key
    if provider == "OPENAI":
        return (
            "https://api.openai.com/v1/chat/completions",
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            body,
        )
    if provider == "AZURE_OPENAI":
        url = (
            f"{endpoint.rstrip('/')}/openai/deployments/{model}"
            f"/chat/completions?api-version=2024-02-15-preview"
        )
        return url, {"api-key": api_key, "Content-Type": "application/json"}, body
    # CUSTOM: assume OpenAI-compatible
    # If the user specified a custom path (like /chat/sync), preserve it exactly as-is
    from urllib.parse import urlparse
    parsed = urlparse(endpoint)
    if parsed.path and parsed.path.rstrip("/") not in ("", "/"):
        url = endpoint
    else:
        url = endpoint if endpoint.endswith("/chat/completions") else endpoint.rstrip("/") + "/chat/completions"
    return url, {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, body


def get_config(db: Session, tenant_id: str) -> LlmConfig | None:
    return db.execute(
        select(LlmConfig).where(LlmConfig.tenantId == tenant_id)
    ).scalar_one_or_none()


def to_public(cfg: LlmConfig) -> dict[str, Any]:
    """Strip secrets — return display data only."""
    try:
        api_key = decrypt(cfg.apiKeyEnc) if cfg.apiKeyEnc else ""
    except Exception:  # noqa: BLE001
        api_key = ""
    try:
        backup_key = decrypt(cfg.backupApiKeyEnc) if cfg.backupApiKeyEnc else ""
    except Exception:  # noqa: BLE001
        backup_key = ""
    return {
        "id": cfg.id,
        "tenantId": cfg.tenantId,
        "enabled": cfg.enabled,
        "provider": cfg.provider,
        "model": cfg.model,
        "endpoint": cfg.endpoint,
        "apiKeyMasked": mask_secret(api_key) if api_key else None,
        "backupProvider": cfg.backupProvider,
        "backupModel": cfg.backupModel,
        "backupApiKeyMasked": mask_secret(backup_key) if backup_key else None,
        "maxTokens": cfg.maxTokens,
        "temperature": cfg.temperature,
        "createdAt": cfg.createdAt.isoformat() if cfg.createdAt else None,
        "updatedAt": cfg.updatedAt.isoformat() if cfg.updatedAt else None,
    }


def upsert_config(
    db: Session,
    tenant_id: str,
    *,
    provider: str,
    model: str,
    api_key: str | None,
    endpoint: str | None = None,
    enabled: bool = True,
    backup_provider: str | None = None,
    backup_model: str | None = None,
    backup_api_key: str | None = None,
    max_tokens: int = 4000,
    temperature: float = 0.1,
) -> LlmConfig:
    if provider not in VALID_PROVIDERS:
        raise bad_request("INVALID_PROVIDER", f"Unsupported provider: {provider}")
    endpoint = _validate_endpoint_shape(provider, endpoint)
    if backup_provider and backup_provider not in VALID_PROVIDERS:
        raise bad_request(
            "INVALID_PROVIDER", f"Unsupported backup provider: {backup_provider}"
        )

    cfg = get_config(db, tenant_id)
    if cfg is None:
        if not api_key:
            raise bad_request("API_KEY_REQUIRED", "apiKey is required on first save")
        cfg = LlmConfig(
            tenantId=tenant_id,
            provider=provider,
            model=model,
            endpoint=endpoint,
            apiKeyEnc=encrypt(api_key),
            enabled=enabled,
            backupProvider=backup_provider,
            backupModel=backup_model,
            backupApiKeyEnc=encrypt(backup_api_key) if backup_api_key else None,
            maxTokens=max_tokens,
            temperature=temperature,
        )
        db.add(cfg)
    else:
        cfg.provider = provider
        cfg.model = model
        cfg.endpoint = endpoint
        cfg.enabled = enabled
        cfg.backupProvider = backup_provider
        cfg.backupModel = backup_model
        cfg.maxTokens = max_tokens
        cfg.temperature = temperature
        if api_key:
            cfg.apiKeyEnc = encrypt(api_key)
        if backup_api_key is not None:
            cfg.backupApiKeyEnc = encrypt(backup_api_key) if backup_api_key else None
    db.commit()
    db.refresh(cfg)
    return cfg


def test_connectivity(db: Session, tenant_id: str) -> dict[str, Any]:
    cfg = get_config(db, tenant_id)
    if cfg is None:
        raise not_found("LLM_CONFIG_NOT_FOUND", "LLM configuration not found")
    api_key = decrypt(cfg.apiKeyEnc)
    url, headers, body = _build_test_request(
        cfg.provider, cfg.endpoint or "", cfg.model, api_key
    )
    try:
        with httpx.Client(timeout=_TEST_TIMEOUT) as client:
            resp = client.post(url, headers=headers, json=body)
        ok = 200 <= resp.status_code < 300
        return {
            "ok": ok,
            "status": resp.status_code,
            "provider": cfg.provider,
            "model": cfg.model,
            "message": "ok" if ok else resp.text[:500],
        }
    except httpx.TimeoutException:
        return {"ok": False, "status": 0, "message": "Timed out after 12s"}
    except Exception as e:  # noqa: BLE001
        log.warning("llm_config test failed: %s", e)
        return {"ok": False, "status": 0, "message": str(e)[:500]}
