"""Pydantic schemas for billing and webhooks."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


class CreateCheckoutSessionRequest(BaseModel):
    plan: Literal["BASIC", "PRO", "ENTERPRISE"]
    successUrl: str
    cancelUrl: str


class ChangePlanRequest(BaseModel):
    plan: Literal["BASIC", "PRO", "ENTERPRISE"]
    prorationBehavior: Literal["create_prorations", "always_invoice", "none"] = (
        "create_prorations"
    )


class CreatePortalSessionRequest(BaseModel):
    returnUrl: str


class CreateOutboundWebhookRequest(BaseModel):
    url: HttpUrl
    events: list[str] = Field(min_length=1)


class UpdateWebhookStatusRequest(BaseModel):
    status: Literal["ACTIVE", "INACTIVE"]


class IngestConversationItem(BaseModel):
    externalId: str | None = None
    agentId: str | None = None
    agentName: str | None = None
    customerRef: str | None = None
    content: dict
    metadata: dict | None = None
    receivedAt: str | None = None


class IngestRequest(BaseModel):
    channel: Literal["CHAT", "EMAIL", "CALL", "SOCIAL"]
    conversations: list[IngestConversationItem] = Field(min_length=1, max_length=500)
