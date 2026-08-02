"""SQLAlchemy 2.0 models mapped to the existing Prisma-managed master DB.

Column names are camelCase because Prisma's default mapping is identity
(field name == column name). Do NOT run ``Base.metadata.create_all`` against
the live DB — Prisma owns the schema today.

Only the models required by auth + users are modelled here; additional
tables will be added as the rest of the Nest modules are ported.
"""
from __future__ import annotations

from datetime import datetime

from cuid2 import Cuid
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
    Column,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base

_cuid = Cuid(length=25)


def new_cuid() -> str:
    return _cuid.generate()


def _pg_enum(name: str, *values: str) -> SAEnum:
    """Bind to an existing Postgres enum type owned by Prisma (no DDL)."""
    return SAEnum(
        *values,
        name=name,
        native_enum=True,
        create_type=True,
        validate_strings=True,
    )


_PLAN_TYPE = _pg_enum("PlanType", "BASIC", "PRO", "ENTERPRISE")
_TENANT_STATUS = _pg_enum("TenantStatus", "PROVISIONING", "ACTIVE", "SUSPENDED", "CANCELLED")
_USER_ROLE = _pg_enum("UserRole", "ADMIN", "QA", "VERIFIER")
_USER_STATUS = _pg_enum("UserStatus", "ACTIVE", "INACTIVE", "INVITED")
_SUBSCRIPTION_STATUS = _pg_enum(
    "SubscriptionStatus", "TRIALING", "ACTIVE", "PAST_DUE", "CANCELLED", "EXPIRED"
)
_LLM_PROVIDER = _pg_enum("LlmProvider", "OPENAI", "AZURE_OPENAI", "CUSTOM")
_SMTP_ENCRYPTION = _pg_enum("SmtpEncryption", "NONE", "TLS", "SSL")
_ROUTING_FALLBACK_MODE = _pg_enum("RoutingFallbackMode", "REJECT", "UNASSIGNED_QUEUE", "FALLBACK_DEPARTMENT")
_ASSIGNMENT_MODE = _pg_enum("AssignmentMode", "ROUND_ROBIN", "MANUAL")
_OUTBOUND_WEBHOOK_STATUS = _pg_enum("OutboundWebhookStatus", "ACTIVE", "INACTIVE")
_OUTBOUND_WEBHOOK_DELIVERY_STATUS = _pg_enum("OutboundWebhookDeliveryStatus", "PENDING", "DELIVERED", "FAILED")
_STRIPE_WEBHOOK_EVENT_STATUS = _pg_enum("StripeWebhookEventStatus", "PROCESSING", "PROCESSED", "FAILED")
_INVOICE_STATUS = _pg_enum("InvoiceStatus", "DRAFT", "OPEN", "PAID", "VOID", "UNCOLLECTIBLE")


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    plan: Mapped[str] = mapped_column(_PLAN_TYPE, nullable=False, default="BASIC")
    status: Mapped[str] = mapped_column(_TENANT_STATUS, nullable=False, default="PROVISIONING")

    dbHost: Mapped[str] = mapped_column("dbHost", String, nullable=False, default="")
    dbPort: Mapped[int] = mapped_column("dbPort", Integer, nullable=False, default=5432)
    dbName: Mapped[str] = mapped_column("dbName", String, nullable=False, default="")
    dbUser: Mapped[str] = mapped_column("dbUser", String, nullable=False, default="")
    dbPasswordEnc: Mapped[str] = mapped_column(
        "dbPasswordEnc", String, nullable=False, default=""
    )

    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        "updatedAt", DateTime(timezone=True), default=func.now(), server_default=func.now(), onupdate=func.now()
    )

    users: Mapped[list["User"]] = relationship(back_populates="tenant", cascade="all, delete")

    customConversationsLimit: Mapped[int | None] = mapped_column("customConversationsLimit", Integer, nullable=True)
    customFormsLimit: Mapped[int | None] = mapped_column("customFormsLimit", Integer, nullable=True)
    customUsersLimit: Mapped[int | None] = mapped_column("customUsersLimit", Integer, nullable=True)
    featureFlags: Mapped[dict | None] = mapped_column("featureFlags", JSONB, nullable=True, default=dict)
    pendingPlan: Mapped[str | None] = mapped_column("pendingPlan", _PLAN_TYPE, nullable=True)
    deletedAt: Mapped[datetime | None] = mapped_column("deletedAt", DateTime(timezone=True), nullable=True)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenantId", "email", name="users_tenantId_email_key"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    tenantId: Mapped[str] = mapped_column(
        "tenantId", String, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    departmentId: Mapped[str | None] = mapped_column(
        "departmentId",
        String,
        ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
    )
    email: Mapped[str] = mapped_column(String, nullable=False)
    passwordHash: Mapped[str] = mapped_column("passwordHash", String, nullable=False, default="")
    name: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(_USER_ROLE, nullable=False)
    status: Mapped[str] = mapped_column(_USER_STATUS, nullable=False, default="INVITED")
    lastLoginAt: Mapped[datetime | None] = mapped_column(
        "lastLoginAt", DateTime(timezone=True), nullable=True
    )
    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        "updatedAt", DateTime(timezone=True), default=func.now(), server_default=func.now(), onupdate=func.now()
    )
    deletedAt: Mapped[datetime | None] = mapped_column(
        "deletedAt", DateTime(timezone=True), nullable=True
    )

    tenant: Mapped[Tenant] = relationship(back_populates="users")
    department: Mapped["Department | None"] = relationship(lazy="joined")
    refreshTokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    userId: Mapped[str] = mapped_column(
        "userId", String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tokenHash: Mapped[str] = mapped_column("tokenHash", String, unique=True, nullable=False)
    expiresAt: Mapped[datetime] = mapped_column(
        "expiresAt", DateTime(timezone=True), nullable=False
    )
    revokedAt: Mapped[datetime | None] = mapped_column(
        "revokedAt", DateTime(timezone=True), nullable=True
    )
    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="refreshTokens")


class Department(Base):
    __tablename__ = "departments"
    __table_args__ = (
        UniqueConstraint("tenantId", "slug", name="departments_tenantId_slug_key"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    tenantId: Mapped[str] = mapped_column(
        "tenantId", String, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    channels: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    autoAssignEnabled: Mapped[bool] = mapped_column(
        "autoAssignEnabled", Boolean, nullable=False, default=False
    )
    isActive: Mapped[bool] = mapped_column("isActive", Boolean, nullable=False, default=True)
    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        "updatedAt", DateTime(timezone=True), default=func.now(), server_default=func.now(), onupdate=func.now()
    )


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    tenantId: Mapped[str] = mapped_column(
        "tenantId",
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    plan: Mapped[str] = mapped_column(_PLAN_TYPE, nullable=False)
    status: Mapped[str] = mapped_column(_SUBSCRIPTION_STATUS, nullable=False, default="TRIALING")
    currentPeriodStart: Mapped[datetime] = mapped_column(
        "currentPeriodStart", DateTime(timezone=True), nullable=False
    )
    currentPeriodEnd: Mapped[datetime] = mapped_column(
        "currentPeriodEnd", DateTime(timezone=True), nullable=False
    )
    trialEndsAt: Mapped[datetime | None] = mapped_column(
        "trialEndsAt", DateTime(timezone=True), nullable=True
    )
    cancelledAt: Mapped[datetime | None] = mapped_column(
        "cancelledAt", DateTime(timezone=True), nullable=True
    )
    stripeSubscriptionId: Mapped[str | None] = mapped_column(
        "stripeSubscriptionId", String, unique=True, nullable=True
    )
    stripeCustomerId: Mapped[str | None] = mapped_column(
        "stripeCustomerId", String, nullable=True
    )
    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        "updatedAt", DateTime(timezone=True), default=func.now(), server_default=func.now(), onupdate=func.now()
    )


class UsageMetric(Base):
    __tablename__ = "usage_metrics"
    __table_args__ = (
        UniqueConstraint(
            "tenantId",
            "periodStart",
            "periodEnd",
            name="usage_metrics_tenantId_periodStart_periodEnd_key",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    tenantId: Mapped[str] = mapped_column(
        "tenantId", String, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    periodStart: Mapped[datetime] = mapped_column(
        "periodStart", DateTime(timezone=True), nullable=False
    )
    periodEnd: Mapped[datetime] = mapped_column(
        "periodEnd", DateTime(timezone=True), nullable=False
    )
    conversationsProcessed: Mapped[int] = mapped_column(
        "conversationsProcessed", Integer, nullable=False, default=0
    )
    aiTokensUsed: Mapped[int] = mapped_column(
        "aiTokensUsed", BigInteger, nullable=False, default=0
    )
    aiCostCents: Mapped[int] = mapped_column("aiCostCents", Integer, nullable=False, default=0)
    activeUsers: Mapped[int] = mapped_column("activeUsers", Integer, nullable=False, default=0)
    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        "updatedAt", DateTime(timezone=True), default=func.now(), server_default=func.now(), onupdate=func.now()
    )


class PlatformPlan(Base):
    __tablename__ = "platform_plans"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    priceMonthly: Mapped[int] = mapped_column("priceMonthly", Integer, nullable=False, default=0)
    priceYearly: Mapped[int] = mapped_column("priceYearly", Integer, nullable=False, default=0)
    conversationsLimit: Mapped[int | None] = mapped_column("conversationsLimit", Integer, nullable=True)
    formsLimit: Mapped[int | None] = mapped_column("formsLimit", Integer, nullable=True)
    usersLimit: Mapped[int | None] = mapped_column("usersLimit", Integer, nullable=True)
    features: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default='[]')
    isActive: Mapped[bool] = mapped_column("isActive", Boolean, nullable=False, default=True)
    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        "updatedAt", DateTime(timezone=True), default=func.now(), server_default=func.now(), onupdate=func.now()
    )


# ─── extended master models (LLM / settings / billing / webhooks / routing) ──


class LlmConfig(Base):
    __tablename__ = "llm_configs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    tenantId: Mapped[str] = mapped_column(
        "tenantId", String, ForeignKey("tenants.id", ondelete="CASCADE"),
        unique=True, nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    provider: Mapped[str] = mapped_column(_LLM_PROVIDER, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    endpoint: Mapped[str | None] = mapped_column(String, nullable=True)
    apiKeyEnc: Mapped[str] = mapped_column("apiKeyEnc", String, nullable=False)
    backupProvider: Mapped[str | None] = mapped_column("backupProvider", _LLM_PROVIDER, nullable=True)
    backupModel: Mapped[str | None] = mapped_column("backupModel", String, nullable=True)
    backupApiKeyEnc: Mapped[str | None] = mapped_column(
        "backupApiKeyEnc", String, nullable=True
    )
    maxTokens: Mapped[int] = mapped_column("maxTokens", Integer, nullable=False, default=4000)
    temperature: Mapped[float] = mapped_column(Float, nullable=False, default=0.1)
    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        "updatedAt", DateTime(timezone=True), default=func.now(), server_default=func.now(), onupdate=func.now()
    )


class EscalationRule(Base):
    __tablename__ = "escalation_rules"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    tenantId: Mapped[str] = mapped_column(
        "tenantId", String, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    qaDeviationThreshold: Mapped[float] = mapped_column(
        "qaDeviationThreshold", Float, nullable=False, default=15
    )
    verifierDeviationThreshold: Mapped[float] = mapped_column(
        "verifierDeviationThreshold", Float, nullable=False, default=10
    )
    verifierMinRangeStart: Mapped[float] = mapped_column(
        "verifierMinRangeStart", Float, nullable=False, default=0
    )
    verifierMinRangeEnd: Mapped[float] = mapped_column(
        "verifierMinRangeEnd", Float, nullable=False, default=40
    )
    verifierMaxRangeStart: Mapped[float] = mapped_column(
        "verifierMaxRangeStart", Float, nullable=False, default=90
    )
    verifierMaxRangeEnd: Mapped[float] = mapped_column(
        "verifierMaxRangeEnd", Float, nullable=False, default=100
    )
    staleQueueHours: Mapped[int] = mapped_column(
        "staleQueueHours", Integer, nullable=False, default=24
    )
    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        "updatedAt", DateTime(timezone=True), default=func.now(), server_default=func.now(), onupdate=func.now()
    )


class BlindReviewSettings(Base):
    __tablename__ = "blind_review_settings"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    tenantId: Mapped[str] = mapped_column(
        "tenantId", String, ForeignKey("tenants.id", ondelete="CASCADE"),
        unique=True, nullable=False,
    )
    hideAgentFromQA: Mapped[bool] = mapped_column(
        "hideAgentFromQA", Boolean, nullable=False, default=False
    )
    hideQAFromVerifier: Mapped[bool] = mapped_column(
        "hideQAFromVerifier", Boolean, nullable=False, default=False
    )
    bestThreshold: Mapped[float] = mapped_column(
        "bestThreshold", Float, nullable=False, default=90.0
    )
    goodThreshold: Mapped[float] = mapped_column(
        "goodThreshold", Float, nullable=False, default=75.0
    )
    avgThreshold: Mapped[float] = mapped_column(
        "avgThreshold", Float, nullable=False, default=60.0
    )
    poorThreshold: Mapped[float] = mapped_column(
        "poorThreshold", Float, nullable=False, default=0.0
    )
    scoreBuckets: Mapped[list | None] = mapped_column(
        "scoreBuckets", JSONB, nullable=True, default=list
    )
    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        "updatedAt", DateTime(timezone=True), default=func.now(), server_default=func.now(), onupdate=func.now()
    )


class TenantEmailSettings(Base):
    __tablename__ = "tenant_email_settings"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    tenantId: Mapped[str] = mapped_column(
        "tenantId", String, ForeignKey("tenants.id", ondelete="CASCADE"),
        unique=True, nullable=False,
    )
    smtpHost: Mapped[str | None] = mapped_column("smtpHost", String, nullable=True)
    smtpPort: Mapped[int | None] = mapped_column("smtpPort", Integer, nullable=True)
    encryption: Mapped[str] = mapped_column(_SMTP_ENCRYPTION, nullable=False, default="TLS")
    smtpUser: Mapped[str | None] = mapped_column("smtpUser", String, nullable=True)
    smtpPassEnc: Mapped[str | None] = mapped_column("smtpPassEnc", String, nullable=True)
    fromEmail: Mapped[str | None] = mapped_column("fromEmail", String, nullable=True)
    fromName: Mapped[str | None] = mapped_column("fromName", String, nullable=True)
    notificationsEnabled: Mapped[bool] = mapped_column(
        "notificationsEnabled", Boolean, nullable=False, default=True
    )
    forgotPasswordEnabled: Mapped[bool] = mapped_column(
        "forgotPasswordEnabled", Boolean, nullable=False, default=True
    )
    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        "updatedAt", DateTime(timezone=True), default=func.now(), server_default=func.now(), onupdate=func.now()
    )


class ConversationRoutingSetting(Base):
    __tablename__ = "conversation_routing_settings"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    tenantId: Mapped[str] = mapped_column(
        "tenantId", String, ForeignKey("tenants.id", ondelete="CASCADE"),
        unique=True, nullable=False,
    )
    enforceAppMapping: Mapped[bool] = mapped_column(
        "enforceAppMapping", Boolean, nullable=False, default=False
    )
    fallbackMode: Mapped[str] = mapped_column(
        "fallbackMode", _ROUTING_FALLBACK_MODE, nullable=False, default="REJECT"
    )
    fallbackDepartmentId: Mapped[str | None] = mapped_column(
        "fallbackDepartmentId", String,
        ForeignKey("departments.id", ondelete="SET NULL"), nullable=True,
    )
    assignmentMode: Mapped[str] = mapped_column(
        "assignmentMode", _ASSIGNMENT_MODE, nullable=False, default="ROUND_ROBIN"
    )
    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        "updatedAt", DateTime(timezone=True), default=func.now(), server_default=func.now(), onupdate=func.now()
    )


class AppDepartmentMapping(Base):
    __tablename__ = "app_department_mappings"
    __table_args__ = (
        UniqueConstraint("tenantId", "applicationKey",
                         name="app_department_mappings_tenant_app_key"),
        UniqueConstraint("tenantId", "channel", "departmentId",
                         name="app_department_mappings_tenant_channel_dept_key"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    tenantId: Mapped[str] = mapped_column(
        "tenantId", String, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    applicationKey: Mapped[str] = mapped_column("applicationKey", String, nullable=False)
    channel: Mapped[str] = mapped_column(String, nullable=False)
    displayName: Mapped[str | None] = mapped_column("displayName", String, nullable=True)
    departmentId: Mapped[str] = mapped_column(
        "departmentId", String,
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False,
    )
    isActive: Mapped[bool] = mapped_column("isActive", Boolean, nullable=False, default=True)
    assignmentMode: Mapped[str] = mapped_column(
        "assignmentMode", _ASSIGNMENT_MODE, nullable=False, default="ROUND_ROBIN"
    )
    aiProcessingPercentage: Mapped[int] = mapped_column(
        "aiProcessingPercentage", Integer, nullable=False, default=100
    )
    createdById: Mapped[str | None] = mapped_column("createdById", String, nullable=True)
    updatedById: Mapped[str | None] = mapped_column("updatedById", String, nullable=True)
    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        "updatedAt", DateTime(timezone=True), default=func.now(), server_default=func.now(), onupdate=func.now()
    )


class AppDepartmentRoutingAudit(Base):
    __tablename__ = "app_department_routing_audits"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    tenantId: Mapped[str] = mapped_column(
        "tenantId", String, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    mappingId: Mapped[str | None] = mapped_column(
        "mappingId", String,
        ForeignKey("app_department_mappings.id", ondelete="SET NULL"), nullable=True,
    )
    action: Mapped[str] = mapped_column(String, nullable=False)
    actorId: Mapped[str | None] = mapped_column("actorId", String, nullable=True)
    before: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )


class OutboundWebhook(Base):
    __tablename__ = "outbound_webhooks"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    tenantId: Mapped[str] = mapped_column(
        "tenantId", String, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(String, nullable=False)
    secretEnc: Mapped[str] = mapped_column("secretEnc", String, nullable=False)
    events: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    status: Mapped[str] = mapped_column(_OUTBOUND_WEBHOOK_STATUS, nullable=False, default="ACTIVE")
    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        "updatedAt", DateTime(timezone=True), default=func.now(), server_default=func.now(), onupdate=func.now()
    )


class OutboundWebhookDelivery(Base):
    __tablename__ = "outbound_webhook_deliveries"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    webhookId: Mapped[str] = mapped_column(
        "webhookId", String,
        ForeignKey("outbound_webhooks.id", ondelete="CASCADE"), nullable=False,
    )
    tenantId: Mapped[str] = mapped_column(
        "tenantId", String, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    event: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(_OUTBOUND_WEBHOOK_DELIVERY_STATUS, nullable=False, default="PENDING")
    attemptCount: Mapped[int] = mapped_column(
        "attemptCount", Integer, nullable=False, default=1
    )
    httpStatus: Mapped[int | None] = mapped_column("httpStatus", Integer, nullable=True)
    errorMessage: Mapped[str | None] = mapped_column("errorMessage", String, nullable=True)
    deliveredAt: Mapped[datetime | None] = mapped_column(
        "deliveredAt", DateTime(timezone=True), nullable=True
    )
    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        "updatedAt", DateTime(timezone=True), default=func.now(), server_default=func.now(), onupdate=func.now()
    )


class StripeWebhookEvent(Base):
    __tablename__ = "stripe_webhook_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    stripeEventId: Mapped[str] = mapped_column(
        "stripeEventId", String, unique=True, nullable=False
    )
    eventType: Mapped[str] = mapped_column("eventType", String, nullable=False)
    status: Mapped[str] = mapped_column(_STRIPE_WEBHOOK_EVENT_STATUS, nullable=False, default="PROCESSING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lastError: Mapped[str | None] = mapped_column("lastError", String, nullable=True)
    processedAt: Mapped[datetime | None] = mapped_column(
        "processedAt", DateTime(timezone=True), nullable=True
    )
    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        "updatedAt", DateTime(timezone=True), default=func.now(), server_default=func.now(), onupdate=func.now()
    )


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    subscriptionId: Mapped[str] = mapped_column(
        "subscriptionId", String, ForeignKey("subscriptions.id"), nullable=False
    )
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False, default="USD")
    status: Mapped[str] = mapped_column(_INVOICE_STATUS, nullable=False)
    stripeInvoiceId: Mapped[str | None] = mapped_column(
        "stripeInvoiceId", String, unique=True, nullable=True
    )
    paidAt: Mapped[datetime | None] = mapped_column(
        "paidAt", DateTime(timezone=True), nullable=True
    )
    dueAt: Mapped[datetime] = mapped_column("dueAt", DateTime(timezone=True), nullable=False)
    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )




class PlatformAuditLog(Base):
    __tablename__ = "platform_audit_logs"

    id = Column(String(50), primary_key=True, default=lambda: f"aud_{new_cuid()}")
    user_id = Column(String(100), nullable=True) # ID of admin who performed action
    user_email = Column(String(255), nullable=True)
    action = Column(String(100), nullable=False) # e.g. "plan.updated", "tenant.deleted"
    resource_type = Column(String(50), nullable=False) # e.g. "plan", "tenant"
    resource_id = Column(String(100), nullable=True)
    details = Column(JSONB, nullable=True)
    ip_address = Column(String(45), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class PlatformNotification(Base):
    __tablename__ = "platform_notifications"

    id = Column(String(50), primary_key=True, default=lambda: f"notif_{new_cuid()}")
    title = Column(String(200), nullable=False)
    message = Column(Text, nullable=False)
    type = Column(String(50), nullable=False, default="info") # info, warning, success
    target_audience = Column(String(50), nullable=False, default="all") # all, active, enterprise
    sent_by = Column(String(255), nullable=True) # Admin email
    created_at = Column(DateTime(timezone=True), server_default=func.now())
