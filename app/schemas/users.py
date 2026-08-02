"""User CRUD schemas — mirror apps/api/src/users/dto/users.dto.ts."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from ..common.enums import UserRole, UserStatus


class ListUsersQuery(BaseModel):
    page: int = Field(default=1, ge=1)
    limit: int = Field(default=20, ge=1, le=500)
    search: str | None = None
    role: UserRole | None = None
    status: UserStatus | None = None
    departmentId: str | None = None


class DepartmentSummary(BaseModel):
    id: str
    name: str
    slug: str
    autoAssignEnabled: bool
    isActive: bool


class UserSummary(BaseModel):
    id: str
    email: EmailStr
    name: str
    role: UserRole
    status: UserStatus
    departmentId: str | None = None
    department: DepartmentSummary | None = None
    lastLoginAt: datetime | None = None
    createdAt: datetime


class Pagination(BaseModel):
    page: int
    limit: int
    total: int
    totalPages: int


class ListUsersResponse(BaseModel):
    items: list[UserSummary]
    pagination: Pagination


class CreateUserRequest(BaseModel):
    email: EmailStr
    name: str = Field(min_length=2)
    role: UserRole
    password: str | None = Field(default=None, min_length=8)
    departmentId: str | None = None


class CreateUserResponse(BaseModel):
    user: UserSummary
    password: str


class InviteUserRequest(BaseModel):
    email: EmailStr
    name: str = Field(min_length=2)
    role: UserRole


class InviteUserResponse(BaseModel):
    user: UserSummary
    inviteToken: str


class UpdateUserRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2)
    role: UserRole | None = None
    status: UserStatus | None = None
    departmentId: str | None = None


class UpdateUserResponse(BaseModel):
    user: UserSummary
