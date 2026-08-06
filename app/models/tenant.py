"""SQLAlchemy 2.0 models for the per-tenant Postgres DB (Prisma-managed).

The tenant DB is dynamically chosen per request via TenantPool; these models
attach to an engine at session time, not at import time. They share a separate
``TenantBase`` so the master metadata is never accidentally created here.
"""
from __future__ import annotations

from datetime import datetime

from cuid2 import Cuid
from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class TenantBase(DeclarativeBase):
    """Separate metadata registry — never run create_all() in production."""


_cuid = Cuid(length=25)


def new_cuid() -> str:
    return _cuid.generate()


def _pg_enum(name: str, *values: str) -> SAEnum:
    """Bind to a Prisma-owned Postgres enum type without re-declaring it."""
    return SAEnum(*values, name=name, native_enum=True, create_type=True, validate_strings=True)


_CHANNEL = _pg_enum("Channel", "CHAT", "EMAIL", "CALL", "SOCIAL")
_CONV_STATUS = _pg_enum(
    "ConvStatus", "PENDING", "EVALUATING", "QA_REVIEW", "VERIFIER_REVIEW", "COMPLETED", "FAILED", "AUDIT"
)
_FORM_STATUS = _pg_enum("FormStatus", "DRAFT", "PUBLISHED", "DEPRECATED", "ARCHIVED")
_WORKFLOW_STATE = _pg_enum(
    "WorkflowState",
    "AI_PENDING",
    "AI_IN_PROGRESS",
    "AI_COMPLETED",
    "AI_FAILED",
    "QA_PENDING",
    "QA_IN_PROGRESS",
    "QA_COMPLETED",
    "VERIFIER_PENDING",
    "VERIFIER_IN_PROGRESS",
    "VERIFIER_COMPLETED",
    "LOCKED",
    "ESCALATED",
)
_DEVIATION_TYPE = _pg_enum("DeviationType", "AI_VS_QA", "QA_VS_VERIFIER")
_QUEUE_TYPE = _pg_enum(
    "QueueType", "QA_QUEUE", "VERIFIER_QUEUE", "ESCALATION_QUEUE", "AUDIT_QUEUE"
)


class Conversation(TenantBase):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    externalId: Mapped[str | None] = mapped_column("externalId", String, unique=True, nullable=True)
    departmentId: Mapped[str | None] = mapped_column("departmentId", String, nullable=True)
    channel: Mapped[str] = mapped_column(_CHANNEL, nullable=False)
    agentId: Mapped[str | None] = mapped_column("agentId", String, nullable=True)
    agentHash: Mapped[str | None] = mapped_column("agentHash", String, nullable=True)
    agentName: Mapped[str | None] = mapped_column("agentName", String, nullable=True)
    customerRef: Mapped[str | None] = mapped_column("customerRef", String, nullable=True)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    cmetadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    status: Mapped[str] = mapped_column(_CONV_STATUS, nullable=False, default="PENDING")
    receivedAt: Mapped[datetime] = mapped_column(
        "receivedAt", DateTime(timezone=True), server_default=func.now()
    )
    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        "updatedAt", DateTime(timezone=True), default=func.now(), server_default=func.now(), onupdate=func.now()
    )

    evaluation: Mapped["Evaluation | None"] = relationship(back_populates="conversation")


class FormDefinition(TenantBase):
    __tablename__ = "form_definitions"
    __table_args__ = (
        UniqueConstraint("formKey", "version", name="form_definitions_formKey_version_key"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    formKey: Mapped[str] = mapped_column("formKey", String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    departmentId: Mapped[str | None] = mapped_column("departmentId", String, nullable=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(_FORM_STATUS, nullable=False, default="DRAFT")
    channels: Mapped[list] = mapped_column(JSONB, nullable=False)
    scoringStrategy: Mapped[dict] = mapped_column("scoringStrategy", JSONB, nullable=False)
    sections: Mapped[list] = mapped_column(JSONB, nullable=False)
    questions: Mapped[list] = mapped_column(JSONB, nullable=False)
    fmetadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    publishedAt: Mapped[datetime | None] = mapped_column(
        "publishedAt", DateTime(timezone=True), nullable=True
    )
    deprecatedAt: Mapped[datetime | None] = mapped_column(
        "deprecatedAt", DateTime(timezone=True), nullable=True
    )
    archivedAt: Mapped[datetime | None] = mapped_column(
        "archivedAt", DateTime(timezone=True), nullable=True
    )
    createdById: Mapped[str] = mapped_column("createdById", String, nullable=False)
    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )


class Evaluation(TenantBase):
    __tablename__ = "evaluations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    conversationId: Mapped[str] = mapped_column(
        "conversationId",
        String,
        ForeignKey("conversations.id"),
        unique=True,
        nullable=False,
    )
    formDefinitionId: Mapped[str] = mapped_column(
        "formDefinitionId",
        String,
        ForeignKey("form_definitions.id"),
        nullable=False,
    )
    formVersion: Mapped[int] = mapped_column("formVersion", Integer, nullable=False)
    departmentId: Mapped[str | None] = mapped_column("departmentId", String, nullable=True)
    workflowState: Mapped[str] = mapped_column(
        "workflowState", _WORKFLOW_STATE, nullable=False, default="AI_PENDING"
    )

    aiResponseData: Mapped[dict | None] = mapped_column("aiResponseData", JSONB, nullable=True)
    qaAdjustedData: Mapped[dict | None] = mapped_column("qaAdjustedData", JSONB, nullable=True)
    verifierFinalData: Mapped[dict | None] = mapped_column(
        "verifierFinalData", JSONB, nullable=True
    )
    finalResponseData: Mapped[dict | None] = mapped_column(
        "finalResponseData", JSONB, nullable=True
    )

    aiScore: Mapped[float | None] = mapped_column("aiScore", Float, nullable=True)
    qaScore: Mapped[float | None] = mapped_column("qaScore", Float, nullable=True)
    verifierScore: Mapped[float | None] = mapped_column("verifierScore", Float, nullable=True)
    finalScore: Mapped[float | None] = mapped_column("finalScore", Float, nullable=True)
    passFail: Mapped[bool | None] = mapped_column("passFail", Boolean, nullable=True)

    aiMetadata: Mapped[dict | None] = mapped_column("aiMetadata", JSONB, nullable=True)
    aiCompletedAt: Mapped[datetime | None] = mapped_column(
        "aiCompletedAt", DateTime(timezone=True), nullable=True
    )

    qaUserId: Mapped[str | None] = mapped_column("qaUserId", String, nullable=True)
    qaStartedAt: Mapped[datetime | None] = mapped_column(
        "qaStartedAt", DateTime(timezone=True), nullable=True
    )
    qaCompletedAt: Mapped[datetime | None] = mapped_column(
        "qaCompletedAt", DateTime(timezone=True), nullable=True
    )

    verifierUserId: Mapped[str | None] = mapped_column("verifierUserId", String, nullable=True)
    verifierStartedAt: Mapped[datetime | None] = mapped_column(
        "verifierStartedAt", DateTime(timezone=True), nullable=True
    )
    verifierCompletedAt: Mapped[datetime | None] = mapped_column(
        "verifierCompletedAt", DateTime(timezone=True), nullable=True
    )
    verifierRejectedAt: Mapped[datetime | None] = mapped_column(
        "verifierRejectedAt", DateTime(timezone=True), nullable=True
    )
    verifierRejectReason: Mapped[str | None] = mapped_column(
        "verifierRejectReason", String, nullable=True
    )

    lockedAt: Mapped[datetime | None] = mapped_column(
        "lockedAt", DateTime(timezone=True), nullable=True
    )
    confidenceScore: Mapped[float | None] = mapped_column(
        "confidenceScore", Float, nullable=True
    )
    isEscalated: Mapped[bool] = mapped_column(
        "isEscalated", Boolean, nullable=False, default=False
    )
    escalationReason: Mapped[str | None] = mapped_column(
        "escalationReason", String, nullable=True
    )

    flags: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    feedback: Mapped[str | None] = mapped_column(String, nullable=True)

    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        "updatedAt", DateTime(timezone=True), default=func.now(), server_default=func.now(), onupdate=func.now()
    )

    conversation: Mapped[Conversation] = relationship(back_populates="evaluation")


class DeviationRecord(TenantBase):
    __tablename__ = "deviation_records"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    evaluationId: Mapped[str] = mapped_column(
        "evaluationId",
        String,
        ForeignKey("evaluations.id", ondelete="CASCADE"),
        nullable=False,
    )
    type: Mapped[str] = mapped_column(_DEVIATION_TYPE, nullable=False)
    scoreA: Mapped[float] = mapped_column("scoreA", Float, nullable=False)
    scoreB: Mapped[float] = mapped_column("scoreB", Float, nullable=False)
    deviation: Mapped[float] = mapped_column(Float, nullable=False)
    questionKey: Mapped[str | None] = mapped_column("questionKey", String, nullable=True)
    sectionId: Mapped[str | None] = mapped_column("sectionId", String, nullable=True)
    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )


class AuditCase(TenantBase):
    __tablename__ = "audit_cases"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    evaluationId: Mapped[str] = mapped_column(
        "evaluationId",
        String,
        ForeignKey("evaluations.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="OPEN")
    deviation: Mapped[float] = mapped_column(Float, nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    resolutionNote: Mapped[str | None] = mapped_column("resolutionNote", String, nullable=True)
    resolvedBy: Mapped[str | None] = mapped_column("resolvedBy", String, nullable=True)
    resolvedAt: Mapped[datetime | None] = mapped_column(
        "resolvedAt", DateTime(timezone=True), nullable=True
    )
    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        "updatedAt", DateTime(timezone=True), default=func.now(), server_default=func.now(), onupdate=func.now()
    )


class WorkflowQueue(TenantBase):
    __tablename__ = "workflow_queues"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    evaluationId: Mapped[str] = mapped_column(
        "evaluationId",
        String,
        ForeignKey("evaluations.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    departmentId: Mapped[str | None] = mapped_column("departmentId", String, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    queueType: Mapped[str] = mapped_column("queueType", _QUEUE_TYPE, nullable=False)
    assignedTo: Mapped[str | None] = mapped_column("assignedTo", String, nullable=True)
    dueBy: Mapped[datetime | None] = mapped_column(
        "dueBy", DateTime(timezone=True), nullable=True
    )
    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )
    updatedAt: Mapped[datetime] = mapped_column(
        "updatedAt", DateTime(timezone=True), default=func.now(), server_default=func.now(), onupdate=func.now()
    )


class AuditLog(TenantBase):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_cuid)
    evaluationId: Mapped[str | None] = mapped_column(
        "evaluationId",
        String,
        ForeignKey("evaluations.id"),
        nullable=True,
    )
    entityType: Mapped[str] = mapped_column("entityType", String, nullable=False)
    entityId: Mapped[str] = mapped_column("entityId", String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    actorId: Mapped[str] = mapped_column("actorId", String, nullable=False)
    actorRole: Mapped[str] = mapped_column("actorRole", String, nullable=False)
    before: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    lmetadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    createdAt: Mapped[datetime] = mapped_column(
        "createdAt", DateTime(timezone=True), server_default=func.now()
    )
