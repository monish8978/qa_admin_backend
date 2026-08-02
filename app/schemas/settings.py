"""Pydantic v2 request/response schemas for departments, settings, llm, forms."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field


# ─── Departments ─────────────────────────────────────────────────────────────

class CreateDepartmentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str | None = Field(default=None, max_length=40)
    description: str | None = None
    channels: list[str] | dict[str, bool] | None = None
    autoAssignEnabled: bool = False
    isActive: bool = True


class UpdateDepartmentRequest(BaseModel):
    name: str | None = None
    slug: str | None = None
    description: str | None = None
    channels: list[str] | dict[str, bool] | None = None
    autoAssignEnabled: bool | None = None
    isActive: bool | None = None


class DepartmentResponse(BaseModel):
    id: str
    name: str
    slug: str
    description: str | None
    channels: dict[str, bool]
    autoAssignEnabled: bool
    isActive: bool
    createdAt: datetime
    updatedAt: datetime


# ─── Tenant settings ─────────────────────────────────────────────────────────

class EscalationRuleResponse(BaseModel):
    qaDeviationThreshold: float
    verifierDeviationThreshold: float
    verifierMinRangeStart: float
    verifierMinRangeEnd: float
    verifierMaxRangeStart: float
    verifierMaxRangeEnd: float
    staleQueueHours: int


class PatchEscalationRequest(BaseModel):
    qaDeviationThreshold: float | None = None
    verifierDeviationThreshold: float | None = None
    verifierMinRangeStart: float | None = None
    verifierMinRangeEnd: float | None = None
    verifierMaxRangeStart: float | None = None
    verifierMaxRangeEnd: float | None = None
    staleQueueHours: int | None = None


class ScoreBucket(BaseModel):
    id: str
    name: str
    min: float
    max: float
    color: str | None = "blue"


class BlindReviewResponse(BaseModel):
    hideAgentFromQA: bool
    hideQAFromVerifier: bool
    bestThreshold: float
    goodThreshold: float
    avgThreshold: float
    poorThreshold: float
    scoreBuckets: list[ScoreBucket]


class PatchBlindReviewRequest(BaseModel):
    hideAgentFromQA: bool | None = None
    hideQAFromVerifier: bool | None = None
    bestThreshold: float | None = None
    goodThreshold: float | None = None
    avgThreshold: float | None = None
    poorThreshold: float | None = None
    scoreBuckets: list[ScoreBucket] | None = None


class UpsertEmailSettingsRequest(BaseModel):
    smtpHost: str | None = None
    smtpPort: int | None = Field(default=None, ge=1, le=65535)
    encryption: Literal["NONE", "TLS", "SSL"] | None = None
    smtpUser: str | None = None
    smtpPassword: str | None = None
    fromEmail: EmailStr | None = None
    fromName: str | None = None
    notificationsEnabled: bool | None = None
    forgotPasswordEnabled: bool | None = None


class SendTestEmailRequest(BaseModel):
    to: EmailStr


# ─── LLM config ──────────────────────────────────────────────────────────────

class UpsertLlmConfigRequest(BaseModel):
    provider: Literal["OPENAI", "AZURE_OPENAI", "CUSTOM"]
    model: str = Field(min_length=1)
    endpoint: str | None = None
    apiKey: str | None = None
    enabled: bool = True
    backupProvider: Literal["OPENAI", "AZURE_OPENAI", "CUSTOM"] | None = None
    backupModel: str | None = None
    backupApiKey: str | None = None
    maxTokens: int = Field(default=4000, ge=1, le=200_000)
    temperature: float = Field(default=0.1, ge=0, le=2)


# ─── Forms ───────────────────────────────────────────────────────────────────

class CreateFormRequest(BaseModel):
    formKey: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    channels: list[str]
    scoringStrategy: dict[str, Any]
    sections: list[Any]
    questions: list[Any]
    departmentId: str | None = None
    metadata: dict[str, Any] | None = None


class UpdateFormRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    channels: list[str] | None = None
    scoringStrategy: dict[str, Any] | None = None
    sections: list[Any] | None = None
    questions: list[Any] | None = None
    metadata: dict[str, Any] | None = None


class FormStatusActionRequest(BaseModel):
    action: Literal["publish", "unpublish", "deprecate", "archive", "restore"]
