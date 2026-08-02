"""Tenant settings — escalation rules, blind review, email settings, onboarding.

Mirrors apps/api/src/tenant-settings/tenant-settings.service.ts.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..common.encryption import decrypt, encrypt, mask_secret
from ..common.exceptions import bad_request, not_found
from ..models.master import (
    BlindReviewSettings,
    EscalationRule,
    LlmConfig,
    Tenant,
    TenantEmailSettings,
    User,
)
from .notify_service import send_notification
from .tenant_pool import get_tenant_pool

VALID_ENCRYPTIONS = {"NONE", "TLS", "SSL"}


# ─── escalation ───────────────────────────────────────────────────────────────

def get_escalation(db: Session, tenant_id: str) -> EscalationRule:
    row = db.execute(
        select(EscalationRule).where(EscalationRule.tenantId == tenant_id)
    ).scalar_one_or_none()
    if row is None:
        row = EscalationRule(tenantId=tenant_id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def patch_escalation(db: Session, tenant_id: str, patch: dict[str, Any]) -> EscalationRule:
    row = get_escalation(db, tenant_id)
    for key in (
        "qaDeviationThreshold",
        "verifierDeviationThreshold",
        "verifierMinRangeStart",
        "verifierMinRangeEnd",
        "verifierMaxRangeStart",
        "verifierMaxRangeEnd",
        "staleQueueHours",
    ):
        if key in patch and patch[key] is not None:
            setattr(row, key, patch[key])
    if row.verifierMinRangeStart > row.verifierMinRangeEnd:
        raise bad_request("INVALID_RANGE", "verifierMinRangeStart must be <= verifierMinRangeEnd")
    if row.verifierMaxRangeStart > row.verifierMaxRangeEnd:
        raise bad_request("INVALID_RANGE", "verifierMaxRangeStart must be <= verifierMaxRangeEnd")
    db.commit()
    db.refresh(row)
    return row


DEFAULT_BUCKETS = [
    { "id": "best", "name": "Best Performance", "min": 90.0, "max": 100.0, "color": "emerald" },
    { "id": "good", "name": "Good Performance", "min": 70.0, "max": 89.9, "color": "blue" },
    { "id": "avg", "name": "Average Performance", "min": 60.0, "max": 69.9, "color": "amber" },
    { "id": "poor", "name": "Poor Performance", "min": 0.0, "max": 59.9, "color": "red" }
]


def get_blind_review(db: Session, tenant_id: str) -> BlindReviewSettings:
    row = db.execute(
        select(BlindReviewSettings).where(BlindReviewSettings.tenantId == tenant_id)
    ).scalar_one_or_none()
    if row is None:
        row = BlindReviewSettings(tenantId=tenant_id, scoreBuckets=DEFAULT_BUCKETS)
        db.add(row)
        db.commit()
        db.refresh(row)
    elif not row.scoreBuckets or len(row.scoreBuckets) == 0:
        row.scoreBuckets = DEFAULT_BUCKETS
        db.commit()
        db.refresh(row)
    return row


def patch_blind_review(
    db: Session, tenant_id: str, patch: dict[str, Any]
) -> BlindReviewSettings:
    row = get_blind_review(db, tenant_id)
    if "hideAgentFromQA" in patch:
        row.hideAgentFromQA = bool(patch["hideAgentFromQA"])
    if "hideQAFromVerifier" in patch:
        row.hideQAFromVerifier = bool(patch["hideQAFromVerifier"])
    if "bestThreshold" in patch and patch["bestThreshold"] is not None:
        row.bestThreshold = float(patch["bestThreshold"])
    if "goodThreshold" in patch and patch["goodThreshold"] is not None:
        row.goodThreshold = float(patch["goodThreshold"])
    if "avgThreshold" in patch and patch["avgThreshold"] is not None:
        row.avgThreshold = float(patch["avgThreshold"])
    if "poorThreshold" in patch and patch["poorThreshold"] is not None:
        row.poorThreshold = float(patch["poorThreshold"])
    if "scoreBuckets" in patch and patch["scoreBuckets"] is not None:
        row.scoreBuckets = [
            b.model_dump() if hasattr(b, "model_dump") else b
            for b in patch["scoreBuckets"]
        ]
    db.commit()
    db.refresh(row)
    return row


# ─── email -------------------------------------------------------------------

def get_email_settings(db: Session, tenant_id: str) -> dict[str, Any]:
    row = db.execute(
        select(TenantEmailSettings).where(TenantEmailSettings.tenantId == tenant_id)
    ).scalar_one_or_none()
    if row is None:
        return {
            "configured": False,
            "notificationsEnabled": False,
            "forgotPasswordEnabled": False,
        }
    return {
        "configured": True,
        "smtpHost": row.smtpHost,
        "smtpPort": row.smtpPort,
        "encryption": row.encryption,
        "smtpUser": row.smtpUser,
        "smtpPasswordMasked": mask_secret(decrypt(row.smtpPassEnc))
        if row.smtpPassEnc
        else None,
        "fromEmail": row.fromEmail,
        "fromName": row.fromName,
        "notificationsEnabled": row.notificationsEnabled,
        "forgotPasswordEnabled": row.forgotPasswordEnabled,
    }


def upsert_email_settings(
    db: Session, tenant_id: str, patch: dict[str, Any]
) -> dict[str, Any]:
    if "encryption" in patch and patch["encryption"] not in VALID_ENCRYPTIONS:
        raise bad_request("INVALID_ENCRYPTION", f"encryption must be in {sorted(VALID_ENCRYPTIONS)}")

    row = db.execute(
        select(TenantEmailSettings).where(TenantEmailSettings.tenantId == tenant_id)
    ).scalar_one_or_none()
    if row is None:
        row = TenantEmailSettings(tenantId=tenant_id)
        db.add(row)

    for key in (
        "smtpHost",
        "smtpPort",
        "encryption",
        "smtpUser",
        "fromEmail",
        "fromName",
    ):
        if key in patch:
            setattr(row, key, patch[key])
    if "smtpPassword" in patch and patch["smtpPassword"] is not None:
        row.smtpPassEnc = encrypt(patch["smtpPassword"]) if patch["smtpPassword"] else None
    if "notificationsEnabled" in patch:
        row.notificationsEnabled = bool(patch["notificationsEnabled"])
    if "forgotPasswordEnabled" in patch:
        row.forgotPasswordEnabled = bool(patch["forgotPasswordEnabled"])

    db.commit()
    db.refresh(row)
    return get_email_settings(db, tenant_id)


def send_test_email(db: Session, tenant_id: str, to: str) -> dict[str, Any]:
    if not to or "@" not in to:
        raise bad_request("INVALID_EMAIL", "A valid recipient address is required")
    ok = send_notification(
        db,
        template="tenant_ready",
        to=to,
        context={"name": "Admin", "loginUrl": "https://example.com"},
        tenant_id=tenant_id,
    )
    return {"sent": ok}


# ─── onboarding status ────────────────────────────────────────────────────────

def get_onboarding_status(db: Session, tenant_id: str) -> dict[str, Any]:
    tenant = db.execute(select(Tenant).where(Tenant.id == tenant_id)).scalar_one_or_none()
    if tenant is None:
        raise not_found("TENANT_NOT_FOUND", "Tenant not found")

    has_email = (
        db.execute(
            select(func.count()).select_from(TenantEmailSettings).where(
                TenantEmailSettings.tenantId == tenant_id,
                TenantEmailSettings.smtpHost.is_not(None),
            )
        ).scalar_one()
        > 0
    )

    forms_count = 0
    conversations_count = 0
    try:
        pool = get_tenant_pool()
        with pool.session(tenant_id) as ts:
            forms_count = ts.execute(
                text(
                    "SELECT COUNT(*) FROM form_definitions WHERE status = 'PUBLISHED'"
                )
            ).scalar_one()
            conversations_count = ts.execute(
                text("SELECT COUNT(*) FROM conversations")
            ).scalar_one()
    except Exception:  # noqa: BLE001
        # Tenant DB not yet provisioned — onboarding is incomplete by definition.
        pass

    has_llm_config = (
        db.execute(
            select(func.count()).select_from(LlmConfig).where(
                LlmConfig.tenantId == tenant_id,
                LlmConfig.enabled.is_(True),
            )
        ).scalar_one()
        > 0
    )
    has_non_admin_users = (
        db.execute(
            select(func.count()).select_from(User).where(
                User.tenantId == tenant_id,
                User.role != "ADMIN",
                User.status != "INACTIVE",
            )
        ).scalar_one()
        > 0
    )

    has_published_form = forms_count > 0
    has_conversations = conversations_count > 0
    is_complete = (
        has_llm_config and has_published_form and has_non_admin_users and has_conversations
    )

    return {
        # Fields consumed by the onboarding wizard (frontend contract).
        "hasLlmConfig": has_llm_config,
        "hasNonAdminUsers": has_non_admin_users,
        "hasPublishedForm": has_published_form,
        "hasConversations": has_conversations,
        "isComplete": is_complete,
        # Legacy fields retained for backward compatibility.
        "tenantStatus": tenant.status,
        "emailConfigured": has_email,
        "publishedForms": forms_count,
        "conversationsIngested": conversations_count,
        "complete": is_complete,
    }
