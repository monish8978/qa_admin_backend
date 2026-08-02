"""Response envelope matching apps/api/src/common/helpers/response.helper.ts."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ApiMeta(BaseModel):
    requestId: str
    timestamp: str


class ApiResponse(BaseModel, Generic[T]):
    data: T
    meta: ApiMeta


def build_response(data: Any, request_id: str) -> dict[str, Any]:
    return {
        "data": data,
        "meta": {
            "requestId": request_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    }
