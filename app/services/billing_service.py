"""Stripe-backed billing — mirrors apps/api/src/billing/billing.service.ts.

`stripe` package is loaded lazily so the API still boots without it; any
Stripe-dependent endpoint raises STRIPE_NOT_CONFIGURED when the secret key
is missing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..common.exceptions import bad_request, not_found
from ..config import get_settings
from ..models.master import (
    Invoice,
    StripeWebhookEvent,
    Subscription,
    Tenant,
    UsageMetric,
    User,
)
from .tenant_pool import get_tenant_pool

log = logging.getLogger("qa.billing")

PLAN_LIMITS_BILLING: dict[str, dict[str, int]] = {
    "BASIC": {"conversations": 500, "users": 5, "forms": 3},
    "PRO": {"conversations": 5000, "users": 25, "forms": 20},
    "ENTERPRISE": {"conversations": -1, "users": -1, "forms": -1},
}

_PLAN_AMOUNTS = {"BASIC": 2900, "PRO": 9900, "ENTERPRISE": 29900}
PlanType = Literal["BASIC", "PRO", "ENTERPRISE"]
ProrationBehavior = Literal["create_prorations", "always_invoice", "none"]


def _stripe_client():
    cfg = get_settings()
    if not cfg.STRIPE_SECRET_KEY:
        raise bad_request("STRIPE_NOT_CONFIGURED", "Stripe is not configured for this environment")
    import stripe  # type: ignore[import-untyped]
    stripe.api_key = cfg.STRIPE_SECRET_KEY
    stripe.api_version = "2024-04-10"
    return stripe


def _to_sub_status(status: str) -> str:
    if status == "trialing":
        return "TRIALING"
    if status == "active":
        return "ACTIVE"
    if status in ("past_due", "unpaid", "incomplete"):
        return "PAST_DUE"
    if status in ("canceled", "incomplete_expired"):
        return "CANCELLED"
    return "ACTIVE"


def _to_invoice_status(status: str | None) -> str:
    if status == "paid":
        return "PAID"
    if status == "open":
        return "OPEN"
    if status == "void":
        return "VOID"
    if status == "uncollectible":
        return "UNCOLLECTIBLE"
    return "DRAFT"


def _dt(ts: int | None) -> datetime | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


# ─── public service API ───────────────────────────────────────────────────────

def get_subscription(db: Session, tenant_id: str) -> dict[str, Any]:
    tenant = db.execute(select(Tenant).where(Tenant.id == tenant_id)).scalar_one_or_none()
    if tenant is None:
        raise not_found("TENANT_NOT_FOUND", "Tenant not found")

    sub = db.execute(
        select(Subscription).where(Subscription.tenantId == tenant_id)
    ).scalar_one_or_none()

    invoices: list[Invoice] = []
    if sub is not None:
        invoices = list(
            db.execute(
                select(Invoice)
                .where(Invoice.subscriptionId == sub.id)
                .order_by(Invoice.createdAt.desc())
                .limit(12)
            ).scalars()
        )

    return {
        "tenant": {
            "id": tenant.id,
            "name": tenant.name,
            "plan": tenant.plan,
            "status": tenant.status,
            "pendingPlan": tenant.pendingPlan,
        },
        "subscription": None
        if sub is None
        else {
            "id": sub.id,
            "plan": sub.plan,
            "status": sub.status,
            "currentPeriodStart": sub.currentPeriodStart.isoformat()
            if sub.currentPeriodStart else None,
            "currentPeriodEnd": sub.currentPeriodEnd.isoformat()
            if sub.currentPeriodEnd else None,
            "trialEndsAt": sub.trialEndsAt.isoformat() if sub.trialEndsAt else None,
            "cancelledAt": sub.cancelledAt.isoformat() if sub.cancelledAt else None,
            "createdAt": sub.createdAt.isoformat() if sub.createdAt else None,
        },
        "invoices": [
            {
                "id": i.id,
                "amount": i.amount,
                "currency": i.currency,
                "status": i.status,
                "paidAt": i.paidAt.isoformat() if i.paidAt else None,
                "dueAt": i.dueAt.isoformat() if i.dueAt else None,
                "createdAt": i.createdAt.isoformat() if i.createdAt else None,
            }
            for i in invoices
        ],
    }


def get_usage(db: Session, tenant_id: str) -> dict[str, Any]:
    tenant = db.execute(select(Tenant).where(Tenant.id == tenant_id)).scalar_one_or_none()
    if tenant is None:
        raise not_found("TENANT_NOT_FOUND", "Tenant not found")

    now = datetime.now(timezone.utc)
    # Shared period helper keeps the billing display aligned with the usage
    # meter / limit-enforcement period (single source of truth).
    from .usage_meter_service import current_period

    period_start_default, period_end_default = current_period(now)

    usage = db.execute(
        select(UsageMetric)
        .where(
            (UsageMetric.tenantId == tenant_id)
            & (UsageMetric.periodStart <= now)
            & (UsageMetric.periodEnd >= now)
        )
        .order_by(UsageMetric.periodEnd.desc())
    ).scalars().first()

    user_count = db.execute(
        select(func.count()).select_from(User).where(
            (User.tenantId == tenant_id) & (User.status != "INACTIVE")
        )
    ).scalar_one()

    form_count = 0
    try:
        pool = get_tenant_pool()
        from sqlalchemy import text  # local to avoid top-level cost if pool not used
        with pool.session(tenant_id) as ts:
            form_count = ts.execute(
                text("SELECT COUNT(*) FROM form_definitions WHERE status <> 'ARCHIVED'")
            ).scalar_one()
    except Exception:  # noqa: BLE001
        pass

    limits = PLAN_LIMITS_BILLING.get(tenant.plan, PLAN_LIMITS_BILLING["BASIC"]).copy()
    if tenant.customConversationsLimit is not None:
        limits["conversations"] = tenant.customConversationsLimit
    if tenant.customUsersLimit is not None:
        limits["users"] = tenant.customUsersLimit
    if tenant.customFormsLimit is not None:
        limits["forms"] = tenant.customFormsLimit


    return {
        "period": {
            "start": (usage.periodStart if usage else period_start_default).isoformat(),
            "end": (usage.periodEnd if usage else period_end_default).isoformat(),
        },
        "conversations": {
            "used": usage.conversationsProcessed if usage else 0,
            "limit": limits["conversations"],
        },
        "users": {"used": user_count, "limit": limits["users"]},
        "forms": {"used": form_count, "limit": limits["forms"]},
        "ai": {
            "tokensUsed": int(usage.aiTokensUsed) if usage else 0,
            "costCents": usage.aiCostCents if usage else 0,
        },
        "plan": tenant.plan,
    }


# ─── Stripe interactions ──────────────────────────────────────────────────────

def create_checkout_session(
    db: Session,
    tenant_id: str,
    *,
    plan: PlanType,
    success_url: str,
    cancel_url: str,
) -> dict[str, Any]:
    stripe = _stripe_client()
    tenant = db.execute(select(Tenant).where(Tenant.id == tenant_id)).scalar_one_or_none()
    if tenant is None:
        raise not_found("TENANT_NOT_FOUND", "Tenant not found")

    sub = db.execute(
        select(Subscription).where(Subscription.tenantId == tenant_id)
    ).scalar_one_or_none()
    customer_id = sub.stripeCustomerId if sub else None
    if not customer_id:
        customer = stripe.Customer.create(name=tenant.name, metadata={"tenantId": tenant.id})
        customer_id = customer["id"]
        if sub is None:
            # No subscription row yet — create one so the Stripe customer id is
            # persisted and not orphaned (checkout completion needs to find it).
            sub = Subscription(
                tenantId=tenant_id,
                plan=tenant.plan,
                status="TRIALING",
                stripeCustomerId=customer_id,
            )
            db.add(sub)
        else:
            sub.stripeCustomerId = customer_id
        db.commit()

    amount = _PLAN_AMOUNTS.get(plan, _PLAN_AMOUNTS["BASIC"])
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={"tenantId": tenant_id, "plan": plan},
        line_items=[
            {
                "quantity": 1,
                "price_data": {
                    "currency": "usd",
                    "recurring": {"interval": "month"},
                    "unit_amount": amount,
                    "product_data": {"name": f"QA Platform {plan} Plan"},
                },
            }
        ],
    )
    return {"id": session["id"], "url": session["url"], "plan": plan}


def change_plan(
    db: Session,
    tenant_id: str,
    *,
    plan: PlanType,
    proration_behavior: ProrationBehavior = "create_prorations",
) -> dict[str, Any]:
    stripe = _stripe_client()
    sub = db.execute(
        select(Subscription).where(Subscription.tenantId == tenant_id)
    ).scalar_one_or_none()
    if not sub or not sub.stripeSubscriptionId:
        raise bad_request(
            "SUBSCRIPTION_NOT_LINKED", "No Stripe subscription is linked to this tenant"
        )

    stripe_sub = stripe.Subscription.retrieve(sub.stripeSubscriptionId)
    items = stripe_sub.get("items", {}).get("data", [])
    if not items:
        raise bad_request(
            "INVALID_STRIPE_SUBSCRIPTION", "Stripe subscription has no billable items"
        )
    item = items[0]

    price = stripe.Price.create(
        currency="usd",
        unit_amount=_PLAN_AMOUNTS.get(plan, _PLAN_AMOUNTS["BASIC"]),
        recurring={"interval": "month"},
        product_data={"name": f"QA Platform {plan} Plan"},
        metadata={"tenantId": tenant_id, "plan": plan},
    )

    updated = stripe.Subscription.modify(
        sub.stripeSubscriptionId,
        proration_behavior=proration_behavior,
        items=[{"id": item["id"], "price": price["id"]}],
        metadata={**(stripe_sub.get("metadata") or {}), "tenantId": tenant_id, "plan": plan},
    )

    sub.plan = plan
    sub.status = _to_sub_status(updated["status"])
    sub.currentPeriodStart = _dt(updated["current_period_start"]) or sub.currentPeriodStart
    sub.currentPeriodEnd = _dt(updated["current_period_end"]) or sub.currentPeriodEnd
    sub.cancelledAt = _dt(updated.get("cancel_at")) or _dt(updated.get("canceled_at"))

    tenant = db.execute(select(Tenant).where(Tenant.id == tenant_id)).scalar_one()
    tenant.plan = plan
    db.commit()

    return {
        "plan": plan,
        "status": sub.status,
        "prorationBehavior": proration_behavior,
        "currentPeriodEnd": sub.currentPeriodEnd.isoformat() if sub.currentPeriodEnd else None,
    }


def _toggle_cancel(db: Session, tenant_id: str, cancel_at_period_end: bool) -> dict[str, Any]:
    stripe = _stripe_client()
    sub = db.execute(
        select(Subscription).where(Subscription.tenantId == tenant_id)
    ).scalar_one_or_none()
    if not sub or not sub.stripeSubscriptionId:
        raise bad_request(
            "SUBSCRIPTION_NOT_LINKED", "No Stripe subscription is linked to this tenant"
        )
    updated = stripe.Subscription.modify(
        sub.stripeSubscriptionId, cancel_at_period_end=cancel_at_period_end
    )
    sub.status = _to_sub_status(updated["status"])
    sub.currentPeriodStart = _dt(updated["current_period_start"]) or sub.currentPeriodStart
    sub.currentPeriodEnd = _dt(updated["current_period_end"]) or sub.currentPeriodEnd
    sub.cancelledAt = (
        _dt(updated.get("cancel_at")) or _dt(updated.get("canceled_at"))
        if cancel_at_period_end
        else None
    )
    db.commit()
    return {
        "status": sub.status,
        "cancelAtPeriodEnd": bool(updated.get("cancel_at_period_end")),
        "currentPeriodEnd": sub.currentPeriodEnd.isoformat() if sub.currentPeriodEnd else None,
        "cancelledAt": sub.cancelledAt.isoformat() if sub.cancelledAt else None,
    }


def cancel_subscription(db: Session, tenant_id: str) -> dict[str, Any]:
    return _toggle_cancel(db, tenant_id, True)


def resume_subscription(db: Session, tenant_id: str) -> dict[str, Any]:
    return _toggle_cancel(db, tenant_id, False)


def create_portal_session(db: Session, tenant_id: str, return_url: str) -> dict[str, Any]:
    stripe = _stripe_client()
    sub = db.execute(
        select(Subscription).where(Subscription.tenantId == tenant_id)
    ).scalar_one_or_none()
    if not sub or not sub.stripeCustomerId:
        raise bad_request("CUSTOMER_NOT_LINKED", "No Stripe customer is linked to this tenant")
    session = stripe.billing_portal.Session.create(
        customer=sub.stripeCustomerId, return_url=return_url
    )
    return {"url": session["url"]}


# ─── Stripe webhook handling ──────────────────────────────────────────────────

def handle_stripe_webhook(
    db: Session, signature: str | None, raw_body: bytes
) -> dict[str, Any]:
    cfg = get_settings()
    if not cfg.STRIPE_WEBHOOK_SECRET:
        raise bad_request("STRIPE_NOT_CONFIGURED", "Stripe webhook secret is not configured")
    if not signature:
        raise bad_request("INVALID_STRIPE_SIGNATURE", "Missing Stripe signature header")

    stripe = _stripe_client()
    try:
        event = stripe.Webhook.construct_event(raw_body, signature, cfg.STRIPE_WEBHOOK_SECRET)
    except Exception as e:  # noqa: BLE001
        log.warning("stripe webhook signature check failed", exc_info=True)
        raise bad_request("INVALID_STRIPE_SIGNATURE", "Stripe signature verification failed") from e

    event_id = event["id"]
    event_type = event["type"]

    existing = db.execute(
        select(StripeWebhookEvent).where(StripeWebhookEvent.stripeEventId == event_id)
    ).scalar_one_or_none()
    if existing and existing.status == "PROCESSED":
        return {"received": True, "eventType": event_type, "duplicate": True}
    if existing and existing.status == "PROCESSING":
        return {"received": True, "eventType": event_type, "inProgress": True}

    if existing is None:
        row = StripeWebhookEvent(
            stripeEventId=event_id, eventType=event_type, status="PROCESSING"
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    else:
        existing.status = "PROCESSING"
        existing.attempts = (existing.attempts or 0) + 1
        existing.lastError = None
        db.commit()
        row = existing

    try:
        _process_stripe_event(db, event)
        row.status = "PROCESSED"
        row.processedAt = datetime.now(timezone.utc)
        row.lastError = None
        db.commit()
    except Exception as e:  # noqa: BLE001
        row.status = "FAILED"
        row.lastError = str(e)[:500]
        db.commit()
        raise

    return {"received": True, "eventType": event_type}


def _process_stripe_event(db: Session, event: dict[str, Any]) -> None:
    event_type = event["type"]
    obj = event["data"]["object"]

    if event_type == "checkout.session.completed":
        tenant_id = (obj.get("metadata") or {}).get("tenantId")
        plan = (obj.get("metadata") or {}).get("plan")
        sub_id = obj.get("subscription")
        customer = obj.get("customer")
        if tenant_id and sub_id and customer and plan:
            sub = db.execute(
                select(Subscription).where(Subscription.tenantId == tenant_id)
            ).scalar_one_or_none()
            if sub:
                sub.plan = plan
                sub.status = "ACTIVE"
                sub.stripeSubscriptionId = str(sub_id)
                sub.stripeCustomerId = str(customer)
            tenant = db.execute(select(Tenant).where(Tenant.id == tenant_id)).scalar_one_or_none()
            if tenant:
                tenant.plan = plan
            db.commit()
        return

    if event_type in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        sub_id = obj.get("id")
        if not sub_id:
            return
        sub = db.execute(
            select(Subscription).where(Subscription.stripeSubscriptionId == sub_id)
        ).scalar_one_or_none()
        if sub is None:
            return
        sub.status = _to_sub_status(obj["status"])
        sub.currentPeriodStart = _dt(obj["current_period_start"]) or sub.currentPeriodStart
        sub.currentPeriodEnd = _dt(obj["current_period_end"]) or sub.currentPeriodEnd
        sub.cancelledAt = _dt(obj.get("canceled_at"))
        db.commit()
        return

    if event_type in ("invoice.payment_succeeded", "invoice.payment_failed"):
        sub_id = obj.get("subscription")
        if isinstance(sub_id, dict):
            sub_id = sub_id.get("id")
        if not sub_id:
            return
        sub = db.execute(
            select(Subscription).where(Subscription.stripeSubscriptionId == sub_id)
        ).scalar_one_or_none()
        if sub is None:
            return

        amount = obj.get("amount_paid") or obj.get("amount_due") or 0
        stripe_invoice_id = obj.get("id")
        st_trans = obj.get("status_transitions") or {}
        paid_at = _dt(st_trans.get("paid_at"))
        due_at = _dt(obj.get("due_date")) or datetime.now(timezone.utc)
        status = _to_invoice_status(obj.get("status"))
        currency = (obj.get("currency") or "usd").upper()

        existing_inv = db.execute(
            select(Invoice).where(Invoice.stripeInvoiceId == stripe_invoice_id)
        ).scalar_one_or_none()
        if existing_inv is None:
            db.add(
                Invoice(
                    subscriptionId=sub.id,
                    amount=amount,
                    currency=currency,
                    status=status,
                    stripeInvoiceId=stripe_invoice_id,
                    paidAt=paid_at,
                    dueAt=due_at,
                )
            )
        else:
            existing_inv.amount = amount
            existing_inv.currency = currency
            existing_inv.status = status
            existing_inv.paidAt = paid_at
            existing_inv.dueAt = due_at

        sub.status = "PAST_DUE" if event_type == "invoice.payment_failed" else "ACTIVE"
        db.commit()
        return
