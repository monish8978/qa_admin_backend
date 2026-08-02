"""External conversation ingestion via tenant API key.

Mirrors apps/api/src/webhooks/webhooks.service.ts. API keys are stored as
``webhook_apikey:<sha256(rawKey)>`` Redis entries pointing to the tenantId.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..common.enums import PLAN_LIMITS, PlanType
from ..common.exceptions import bad_request, unauthorized
from ..models.master import Tenant
from ..models.tenant import Conversation, Evaluation, FormDefinition
from ..redis_client import get_redis
from .tenant_pool import get_tenant_pool
from .usage_meter_service import (
    get_monthly_conversation_count,
    record_conversation,
)

log = logging.getLogger("qa.webhooks_ingest")

_API_KEY_PREFIX = "webhook_apikey:"


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def register_api_key(tenant_id: str, raw_key: str, ttl_seconds: int = 0) -> None:
    r = get_redis()
    if r is None:
        raise bad_request(
            "REDIS_REQUIRED", "Redis must be enabled to register tenant API keys"
        )
    key = f"{_API_KEY_PREFIX}{_hash_key(raw_key)}"
    if ttl_seconds > 0:
        r.set(key, tenant_id, ex=ttl_seconds)
    else:
        r.set(key, tenant_id)


_API_KEY_CURRENT_PREFIX = "webhook_apikey_current:"


def rotate_api_key(tenant_id: str) -> str:
    """Generate a new ingest API key for a tenant, revoking the previous one.

    The raw key is returned once (caller must display it; only the hash is
    stored). Requires Redis. A per-tenant pointer tracks the current key hash so
    rotation can revoke the prior key.
    """
    r = get_redis()
    if r is None:
        raise bad_request(
            "REDIS_REQUIRED", "Redis must be enabled to manage tenant API keys"
        )
    pointer_key = f"{_API_KEY_CURRENT_PREFIX}{tenant_id}"
    prev = r.get(pointer_key)
    if prev is not None:
        prev_hash = prev.decode() if isinstance(prev, bytes) else str(prev)
        r.delete(f"{_API_KEY_PREFIX}{prev_hash}")

    raw_key = f"qaik_{secrets.token_urlsafe(32)}"
    key_hash = _hash_key(raw_key)
    r.set(f"{_API_KEY_PREFIX}{key_hash}", tenant_id)
    r.set(pointer_key, key_hash)
    return raw_key


def resolve_tenant_by_api_key(raw_key: str) -> str:
    r = get_redis()
    if r is None:
        raise unauthorized("INVALID_API_KEY", "Invalid API key")
    val = r.get(f"{_API_KEY_PREFIX}{_hash_key(raw_key)}")
    if not val:
        raise unauthorized("INVALID_API_KEY", "Invalid API key")
    return val.decode() if isinstance(val, bytes) else str(val)


def ingest_conversations(
    master: Session,
    tenant_id: str,
    *,
    channel: str,
    conversations: list[dict[str, Any]],
) -> dict[str, Any]:
    if channel.upper() not in {"CHAT", "EMAIL", "CALL", "SOCIAL"}:
        raise bad_request("INVALID_CHANNEL", f"Unknown channel: {channel}")
    channel = channel.upper()

    tenant = master.execute(
        select(Tenant).where(Tenant.id == tenant_id)
    ).scalar_one_or_none()
    if tenant is None:
        raise unauthorized("INVALID_API_KEY", "Invalid API key")

    # Plan-limit pre-check
    try:
        limits = PLAN_LIMITS[PlanType(tenant.plan)]
    except (ValueError, KeyError):
        limits = PLAN_LIMITS[PlanType.BASIC]
    monthly_cap = tenant.customConversationsLimit if tenant.customConversationsLimit is not None else limits["conversationsPerMonth"]
    if monthly_cap != 999_999:
        used = get_monthly_conversation_count(master, tenant_id)
        remaining = monthly_cap - used
        if remaining <= 0:
            raise bad_request(
                "PLAN_LIMIT_EXCEEDED",
                f"Monthly conversation limit of {monthly_cap} reached.",
            )
        if len(conversations) > remaining:
            raise bad_request(
                "PLAN_LIMIT_WOULD_EXCEED",
                f"Upload would exceed monthly limit. {remaining} conversations remaining.",
            )

    pool = get_tenant_pool()
    accepted = 0
    evaluated = 0
    to_enqueue: list[dict[str, Any]] = []
    with pool.session(tenant_id) as ts:
        # Find the active published form for this channel
        active_form = ts.execute(
            text(
                "SELECT id, version FROM form_definitions "
                "WHERE status = 'PUBLISHED' "
                "  AND CAST(channels AS JSONB) @> CAST(:ch AS JSONB) "
                "ORDER BY version DESC LIMIT 1"
            ),
            {"ch": f'["{channel}"]'},
        ).first()

        for c in conversations:
            external_id = c.get("externalId")
            if external_id:
                existing = ts.execute(
                    select(Conversation).where(Conversation.externalId == external_id)
                ).scalar_one_or_none()
                if existing is not None:
                    continue

            conv = Conversation(
                externalId=external_id,
                channel=channel,
                agentId=c.get("agentId"),
                agentName=c.get("agentName"),
                customerRef=c.get("customerRef"),
                content=c.get("content") or {},
                cmetadata=c.get("metadata"),
                receivedAt=_parse_dt(c.get("receivedAt")) or datetime.now(timezone.utc),
                status="PENDING",
            )
            ts.add(conv)
            ts.flush()
            accepted += 1

            if active_form is not None:
                existing_eval = ts.execute(
                    select(Evaluation).where(Evaluation.conversationId == conv.id)
                ).scalar_one_or_none()
                if existing_eval is None:
                    new_eval = Evaluation(
                        conversationId=conv.id,
                        formDefinitionId=active_form[0],
                        formVersion=active_form[1],
                        workflowState="AI_PENDING",
                    )
                    ts.add(new_eval)
                    ts.flush()
                    evaluated += 1
                    to_enqueue.append(
                        {
                            "conversation_id": conv.id,
                            "evaluation_id": new_eval.id,
                            "form_definition_id": active_form[0],
                            "form_version": active_form[1],
                        }
                    )
        ts.commit()

    try:
        if accepted:
            record_conversation(master, tenant_id, accepted)
    except Exception:  # noqa: BLE001
        log.warning("usage_meter.record_conversation failed", exc_info=True)

    # Dispatch a Celery `eval.process` task per new evaluation (after commit so
    # the worker can read the rows). The worker shortcuts to the QA queue when
    # the tenant has no/disabled LLM, so this drives both LLM and non-LLM flows.
    enqueued = 0
    from .conversations_service import _enqueue_eval

    for item in to_enqueue:
        if _enqueue_eval(
            tenant_id=tenant_id,
            conversation_id=item["conversation_id"],
            evaluation_id=item["evaluation_id"],
            form_definition_id=item["form_definition_id"],
            form_version=item["form_version"],
        ):
            enqueued += 1

    return {"accepted": accepted, "evaluated": evaluated, "enqueued": enqueued}


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# Side-loaded reference so the FormDefinition import isn't dead code in the
# import graph when this module is imported by Pydantic schema scanners.
_unused = FormDefinition  # noqa: F841
