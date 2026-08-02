"""Pydantic models for the evaluations module."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AnswerAdjustment(BaseModel):
    value: Any
    overrideReason: str | None = None


class QaSubmitRequest(BaseModel):
    adjustedAnswers: dict[str, AnswerAdjustment]
    feedback: str | None = None
    flags: list[str] | None = None


class VerifierAnswerModification(BaseModel):
    value: Any
    overrideReason: str


class VerifierModifyRequest(BaseModel):
    modifiedAnswers: dict[str, VerifierAnswerModification]
    feedback: str | None = None


class VerifierRejectRequest(BaseModel):
    reason: str = Field(..., min_length=5)
    # Optional per-question changes/comments the verifier wants QA to see on
    # re-review. Each entry requires an overrideReason (the verifier's comment).
    modifiedAnswers: dict[str, VerifierAnswerModification] | None = None


class PreviewScoreRequest(BaseModel):
    formId: str
    answers: dict[str, Any]


class ManualAssignRequest(BaseModel):
    evaluationId: str
    userId: str


class BulkRoundRobinRequest(BaseModel):
    queueType: str
    departmentId: str | None = None
    limit: int | None = Field(None, ge=1, le=1000)


class ReassignRequest(BaseModel):
    evaluationId: str
    newUserId: str
    reason: str | None = None


class ReAuditAiRequest(BaseModel):
    reason: str | None = None


class BulkReAuditAiRequest(BaseModel):
    evaluationIds: list[str] = Field(..., min_length=1, max_length=100)
    reason: str | None = None


class ResolveAuditCaseRequest(BaseModel):
    dismiss: bool = False
    note: str | None = None
