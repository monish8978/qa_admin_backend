"""Coded HTTP exceptions matching the `{ code, message }` shape used by Nest."""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status


class CodedHTTPException(HTTPException):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: list[Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"code": code, "message": message}
        if details is not None:
            payload["details"] = details
        super().__init__(status_code=status_code, detail=payload)


def conflict(code: str, message: str, details: Any | None = None) -> CodedHTTPException:
    return CodedHTTPException(status.HTTP_409_CONFLICT, code, message, details)


def unauthorized(code: str, message: str, details: Any | None = None) -> CodedHTTPException:
    return CodedHTTPException(status.HTTP_401_UNAUTHORIZED, code, message, details)


def forbidden(code: str, message: str, details: Any | None = None) -> CodedHTTPException:
    return CodedHTTPException(status.HTTP_403_FORBIDDEN, code, message, details)


def not_found(code: str, message: str, details: Any | None = None) -> CodedHTTPException:
    return CodedHTTPException(status.HTTP_404_NOT_FOUND, code, message, details)


def bad_request(code: str, message: str, details: Any | None = None) -> CodedHTTPException:
    return CodedHTTPException(status.HTTP_400_BAD_REQUEST, code, message, details)
