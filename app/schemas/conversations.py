"""Pydantic schemas for conversations + routing."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Channel = Literal["CHAT", "EMAIL", "CALL", "SOCIAL"]
FallbackMode = Literal["REJECT", "UNASSIGNED_QUEUE", "FALLBACK_DEPARTMENT"]
AssignmentMode = Literal["ROUND_ROBIN", "MANUAL"]


class ListConversationsQuery(BaseModel):
    status: str | None = None
    agentId: str | None = None
    search: str | None = None
    page: int = Field(1, ge=1)
    limit: int = Field(20, ge=1, le=100)


class UploadConversationItem(BaseModel):
    externalId: str | None = None
    agentId: str | None = None
    agentName: str | None = None
    customerRef: str | None = None
    content: Any
    metadata: dict[str, Any] | None = None
    receivedAt: str | None = None


class UploadConversationsRequest(BaseModel):
    channel: str
    conversations: list[UploadConversationItem] = Field(min_length=1, max_length=500)

    @field_validator("channel")
    @classmethod
    def normalize_channel(cls, value: str) -> str:
        normalized = (value or "").strip().upper()
        if normalized not in {"CHAT", "EMAIL", "CALL", "SOCIAL"}:
            raise ValueError("Input should be 'CHAT', 'EMAIL', 'CALL' or 'SOCIAL'")
        return normalized


class UpsertRoutingSettingsRequest(BaseModel):
    enforceAppMapping: bool | None = None
    fallbackMode: FallbackMode | None = None
    fallbackDepartmentId: str | None = None
    assignmentMode: AssignmentMode | None = None


class CreateAppDepartmentMappingRequest(BaseModel):
    applicationKey: str = Field(min_length=2, max_length=120)
    channel: Channel
    displayName: str | None = Field(default=None, max_length=120)
    departmentId: str
    isActive: bool | None = True
    assignmentMode: AssignmentMode | None = "ROUND_ROBIN"
    aiProcessingPercentage: int | None = Field(default=100, ge=0, le=100)


class UpdateAppDepartmentMappingRequest(BaseModel):
    applicationKey: str | None = Field(default=None, min_length=2, max_length=120)
    channel: Channel | None = None
    displayName: str | None = Field(default=None, max_length=120)
    departmentId: str | None = None
    isActive: bool | None = None
    assignmentMode: AssignmentMode | None = None
    aiProcessingPercentage: int | None = Field(default=None, ge=0, le=100)


class RoutingPreviewRequest(BaseModel):
    channel: Channel
    applicationKey: str | None = None
    metadata: dict[str, Any] | None = None


class DirectUploadConversationsRequest(BaseModel):
    email: str
    password: str
    tenantSlug: str
    channel: str
    conversations: list[UploadConversationItem] = Field(min_length=1, max_length=500)

    @field_validator("channel")
    @classmethod
    def normalize_channel(cls, value: str) -> str:
        normalized = (value or "").strip().upper()
        if normalized not in {"CHAT", "EMAIL", "CALL", "SOCIAL"}:
            raise ValueError("Input should be 'CHAT', 'EMAIL', 'CALL' or 'SOCIAL'")
        return normalized

