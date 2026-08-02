"""Enums mirrored from packages/shared/src/index.ts. Keep values in sync."""
from __future__ import annotations

from enum import Enum


class UserRole(str, Enum):
    ADMIN = "ADMIN"
    QA = "QA"
    VERIFIER = "VERIFIER"


class UserStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    INVITED = "INVITED"


class PlanType(str, Enum):
    BASIC = "BASIC"
    PRO = "PRO"
    ENTERPRISE = "ENTERPRISE"


class TenantStatus(str, Enum):
    PROVISIONING = "PROVISIONING"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    CANCELLED = "CANCELLED"


class SubscriptionStatus(str, Enum):
    TRIALING = "TRIALING"
    ACTIVE = "ACTIVE"
    PAST_DUE = "PAST_DUE"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class WorkflowState(str, Enum):
    AI_PENDING = "AI_PENDING"
    AI_IN_PROGRESS = "AI_IN_PROGRESS"
    AI_FAILED = "AI_FAILED"
    QA_PENDING = "QA_PENDING"
    QA_IN_PROGRESS = "QA_IN_PROGRESS"
    QA_COMPLETED = "QA_COMPLETED"
    VERIFIER_PENDING = "VERIFIER_PENDING"
    VERIFIER_IN_PROGRESS = "VERIFIER_IN_PROGRESS"
    LOCKED = "LOCKED"


class DeviationType(str, Enum):
    AI_VS_QA = "AI_VS_QA"
    QA_VS_VERIFIER = "QA_VS_VERIFIER"
    AI_VS_VERIFIER = "AI_VS_VERIFIER"


QUEUE_NAMES = {
    "TENANT_PROVISION": "tenant.provision",
    "EVAL_PROCESS": "eval.process",
    "EVAL_ESCALATE": "eval.escalate",
    "NOTIFY_SEND": "notify.send",
    "BILLING_USAGE_SYNC": "billing.usage.sync",
    "REPORT_EXPORT": "report.export",
}


PLAN_LIMITS: dict[PlanType, dict[str, int]] = {
    PlanType.BASIC: {"conversationsPerMonth": 500, "forms": 3, "users": 5, "dbPoolSize": 2},
    PlanType.PRO: {"conversationsPerMonth": 5000, "forms": 20, "users": 25, "dbPoolSize": 5},
    PlanType.ENTERPRISE: {
        "conversationsPerMonth": 999_999,
        "forms": 999_999,
        "users": 999_999,
        "dbPoolSize": 10,
    },
}
