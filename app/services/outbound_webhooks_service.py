"""Outbound HMAC-signed webhook deliveries.

Mirrors apps/api/src/webhooks/outbound-webhooks.service.ts.
The signing secret is generated server-side, encrypted at rest with
AES-256-GCM, and returned to the caller exactly once (on create/rotate).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..common.encryption import decrypt, encrypt
from ..common.exceptions import bad_request, not_found
from ..models.master import OutboundWebhook, OutboundWebhookDelivery

log = logging.getLogger("qa.outbound_webhooks")

VALID_EVENTS = {"evaluation.completed", "evaluation.escalated", "evaluation.failed"}
_TIMEOUT = 5.0


def _validate_url(url: str) -> None:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError
    except Exception as e:  # noqa: BLE001
        raise bad_request("INVALID_URL", "Webhook URL must be a valid http/https URL") from e


def _validate_events(events: list[str]) -> None:
    if not events:
        raise bad_request("INVALID_EVENTS", "At least one event must be specified")
    invalid = [e for e in events if e not in VALID_EVENTS]
    if invalid:
        raise bad_request(
            "INVALID_EVENTS",
            f"Unknown events: {', '.join(invalid)}. Valid: {', '.join(sorted(VALID_EVENTS))}",
        )


def _serialize(hook: OutboundWebhook, *, with_dates: bool = True) -> dict[str, Any]:
    out = {
        "id": hook.id,
        "url": hook.url,
        "events": list(hook.events or []),
        "status": hook.status,
    }
    if with_dates:
        out["createdAt"] = hook.createdAt.isoformat() if hook.createdAt else None
        out["updatedAt"] = hook.updatedAt.isoformat() if hook.updatedAt else None
    return out


# ─── Management ──────────────────────────────────────────────────────────────

def create(db: Session, tenant_id: str, *, url: str, events: list[str]) -> dict[str, Any]:
    _validate_url(url)
    _validate_events(events)
    raw_secret = secrets.token_hex(32)
    hook = OutboundWebhook(
        tenantId=tenant_id, url=url, secretEnc=encrypt(raw_secret), events=events
    )
    db.add(hook)
    db.commit()
    db.refresh(hook)
    return {**_serialize(hook), "secret": raw_secret}


def list_hooks(db: Session, tenant_id: str) -> list[dict[str, Any]]:
    rows = db.execute(
        select(OutboundWebhook)
        .where(OutboundWebhook.tenantId == tenant_id)
        .order_by(OutboundWebhook.createdAt.desc())
    ).scalars()
    return [_serialize(r) for r in rows]


def _get_hook(db: Session, tenant_id: str, hook_id: str) -> OutboundWebhook:
    hook = db.execute(
        select(OutboundWebhook).where(
            (OutboundWebhook.id == hook_id) & (OutboundWebhook.tenantId == tenant_id)
        )
    ).scalar_one_or_none()
    if hook is None:
        raise not_found("WEBHOOK_NOT_FOUND", "Webhook not found")
    return hook


def update_status(db: Session, tenant_id: str, hook_id: str, status: str) -> dict[str, Any]:
    if status not in ("ACTIVE", "INACTIVE"):
        raise bad_request("INVALID_STATUS", "status must be ACTIVE or INACTIVE")
    hook = _get_hook(db, tenant_id, hook_id)
    hook.status = status
    db.commit()
    db.refresh(hook)
    return _serialize(hook, with_dates=False)


def remove(db: Session, tenant_id: str, hook_id: str) -> None:
    hook = _get_hook(db, tenant_id, hook_id)
    db.delete(hook)
    db.commit()


def rotate_secret(db: Session, tenant_id: str, hook_id: str) -> dict[str, str]:
    hook = _get_hook(db, tenant_id, hook_id)
    raw_secret = secrets.token_hex(32)
    hook.secretEnc = encrypt(raw_secret)
    db.commit()
    return {"secret": raw_secret}


def list_deliveries(
    db: Session,
    tenant_id: str,
    *,
    page: int = 1,
    limit: int = 50,
    webhook_id: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    page = max(page, 1)
    limit = max(min(limit, 200), 1)
    skip = (page - 1) * limit

    base = select(OutboundWebhookDelivery).where(OutboundWebhookDelivery.tenantId == tenant_id)
    count_q = select(func.count()).select_from(OutboundWebhookDelivery).where(
        OutboundWebhookDelivery.tenantId == tenant_id
    )
    if webhook_id:
        base = base.where(OutboundWebhookDelivery.webhookId == webhook_id)
        count_q = count_q.where(OutboundWebhookDelivery.webhookId == webhook_id)
    if status:
        if status not in ("PENDING", "DELIVERED", "FAILED"):
            raise bad_request("INVALID_STATUS", "status filter must be PENDING|DELIVERED|FAILED")
        base = base.where(OutboundWebhookDelivery.status == status)
        count_q = count_q.where(OutboundWebhookDelivery.status == status)

    total = db.execute(count_q).scalar_one()
    rows = db.execute(
        base.order_by(OutboundWebhookDelivery.createdAt.desc()).offset(skip).limit(limit)
    ).scalars()

    items = [
        {
            "id": d.id,
            "webhookId": d.webhookId,
            "tenantId": d.tenantId,
            "event": d.event,
            "status": d.status,
            "attemptCount": d.attemptCount,
            "httpStatus": d.httpStatus,
            "errorMessage": d.errorMessage,
            "deliveredAt": d.deliveredAt.isoformat() if d.deliveredAt else None,
            "createdAt": d.createdAt.isoformat() if d.createdAt else None,
            "updatedAt": d.updatedAt.isoformat() if d.updatedAt else None,
        }
        for d in rows
    ]
    return {
        "items": items,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "totalPages": (total + limit - 1) // limit,
        },
    }


# ─── Delivery ───────────────────────────────────────────────────────────────

def _sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _post_signed(url: str, secret: str, payload: dict[str, Any]) -> int:
    body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-QA-Signature": _sign(body, secret),
        "X-QA-Event": payload["event"],
        "User-Agent": "QA-Platform/1.0",
    }
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.post(url, content=body, headers=headers)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")
    return resp.status_code


def deliver(
    db: Session,
    *,
    tenant_id: str,
    event: str,
    data: dict[str, Any],
) -> None:
    """Synchronously fan out the event to all matching active hooks.

    Errors are caught + logged into ``outbound_webhook_deliveries`` and never
    bubble up to the caller (matches the fire-and-forget Nest contract).
    """
    if event not in VALID_EVENTS:
        log.warning("deliver: unknown event %s", event)
        return

    from datetime import datetime, timezone

    hooks = list(
        db.execute(
            select(OutboundWebhook).where(
                (OutboundWebhook.tenantId == tenant_id)
                & (OutboundWebhook.status == "ACTIVE")
            )
        ).scalars()
    )

    payload_base = {
        "event": event,
        "tenantId": tenant_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **data,
    }

    for hook in hooks:
        if event not in (hook.events or []):
            continue
        delivery = OutboundWebhookDelivery(
            webhookId=hook.id,
            tenantId=tenant_id,
            event=event,
            payload=payload_base,
            status="PENDING",
        )
        db.add(delivery)
        db.commit()
        db.refresh(delivery)
        try:
            secret = decrypt(hook.secretEnc)
            http_status = _post_signed(hook.url, secret, payload_base)
            delivery.status = "DELIVERED"
            delivery.httpStatus = http_status
            delivery.deliveredAt = datetime.now(timezone.utc)
            delivery.errorMessage = None
        except Exception as e:  # noqa: BLE001
            delivery.status = "FAILED"
            delivery.errorMessage = str(e)[:500]
            log.warning(
                "outbound webhook delivery failed [%s] → %s: %s", hook.id, hook.url, e
            )
        db.commit()


def retry_delivery(db: Session, tenant_id: str, delivery_id: str) -> dict[str, Any]:
    delivery = db.execute(
        select(OutboundWebhookDelivery).where(
            (OutboundWebhookDelivery.id == delivery_id)
            & (OutboundWebhookDelivery.tenantId == tenant_id)
        )
    ).scalar_one_or_none()
    if delivery is None:
        raise not_found("DELIVERY_NOT_FOUND", "Delivery not found")
    hook = db.execute(
        select(OutboundWebhook).where(OutboundWebhook.id == delivery.webhookId)
    ).scalar_one_or_none()
    if hook is None:
        raise not_found("WEBHOOK_NOT_FOUND", "Webhook not found")

    secret = decrypt(hook.secretEnc)
    from datetime import datetime, timezone

    try:
        http_status = _post_signed(hook.url, secret, delivery.payload)
        delivery.status = "DELIVERED"
        delivery.attemptCount = (delivery.attemptCount or 0) + 1
        delivery.httpStatus = http_status
        delivery.errorMessage = None
        delivery.deliveredAt = datetime.now(timezone.utc)
        db.commit()
        return {"id": delivery.id, "status": "DELIVERED"}
    except Exception as e:  # noqa: BLE001
        delivery.status = "FAILED"
        delivery.attemptCount = (delivery.attemptCount or 0) + 1
        delivery.errorMessage = str(e)[:500]
        db.commit()
        raise bad_request("DELIVERY_RETRY_FAILED", f"Retry failed: {e}") from e
