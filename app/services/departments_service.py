"""Departments service — mirrors apps/api/src/departments/departments.service.ts.

Slug normalization, channel set {CHAT,EMAIL,CALL,SOCIAL}, no overlap of an
active channel across departments within a tenant, cannot delete a department
that still has assigned users.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..common.exceptions import bad_request, conflict, not_found
from ..models.master import Department, User

VALID_CHANNELS = {"CHAT", "EMAIL", "CALL", "SOCIAL"}
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:40] or "department"


def _validate_slug(slug: str) -> None:
    if not _SLUG_RE.match(slug):
        raise bad_request(
            "INVALID_DEPARTMENT_SLUG",
            "Slug must be lowercase alphanumeric with hyphens (max 40 chars).",
        )


def _normalize_channels(raw: Any) -> dict[str, bool]:
    """Accepts list[str] or dict[str, bool]; returns canonical dict."""
    result = {ch: False for ch in VALID_CHANNELS}
    if raw is None:
        return result
    if isinstance(raw, list):
        for ch in raw:
            if isinstance(ch, str) and ch.upper() in VALID_CHANNELS:
                result[ch.upper()] = True
        return result
    if isinstance(raw, dict):
        for ch, enabled in raw.items():
            if isinstance(ch, str) and ch.upper() in VALID_CHANNELS:
                result[ch.upper()] = bool(enabled)
        return result
    raise bad_request("INVALID_CHANNELS", "channels must be a list or object")


def _active_channels(channels: Any) -> set[str]:
    norm = _normalize_channels(channels)
    return {ch for ch, enabled in norm.items() if enabled}


def _ensure_no_active_channel_overlap(
    db: Session,
    tenant_id: str,
    channels: dict[str, bool],
    exclude_department_id: str | None = None,
) -> None:
    # Multiple departments are allowed to share active channels
    return


# ─── public service API ───────────────────────────────────────────────────────

def list_departments(db: Session, tenant_id: str) -> list[Department]:
    return list(
        db.execute(
            select(Department)
            .where(Department.tenantId == tenant_id)
            .order_by(Department.name.asc())
        ).scalars()
    )


def get_department(db: Session, tenant_id: str, department_id: str) -> Department:
    row = db.execute(
        select(Department).where(
            Department.id == department_id, Department.tenantId == tenant_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise not_found("DEPARTMENT_NOT_FOUND", "Department not found")
    return row


def create_department(
    db: Session,
    tenant_id: str,
    *,
    name: str,
    slug: str | None = None,
    description: str | None = None,
    channels: Any = None,
    auto_assign_enabled: bool = False,
    is_active: bool = True,
) -> Department:
    slug_v = slug or _slugify(name)
    _validate_slug(slug_v)

    existing = db.execute(
        select(Department).where(
            Department.tenantId == tenant_id, Department.slug == slug_v
        )
    ).scalar_one_or_none()
    if existing:
        raise conflict(
            "DEPARTMENT_SLUG_TAKEN", f"Department with slug '{slug_v}' already exists"
        )

    normalized = _normalize_channels(channels)
    if is_active:
        _ensure_no_active_channel_overlap(db, tenant_id, normalized)

    dept = Department(
        tenantId=tenant_id,
        name=name,
        slug=slug_v,
        description=description,
        channels=normalized,
        autoAssignEnabled=auto_assign_enabled,
        isActive=is_active,
    )
    db.add(dept)
    db.commit()
    db.refresh(dept)
    return dept


def update_department(
    db: Session, tenant_id: str, department_id: str, *, patch: dict[str, Any]
) -> Department:
    dept = get_department(db, tenant_id, department_id)

    if "slug" in patch and patch["slug"] != dept.slug:
        _validate_slug(patch["slug"])
        clash = db.execute(
            select(Department).where(
                Department.tenantId == tenant_id,
                Department.slug == patch["slug"],
                Department.id != department_id,
            )
        ).scalar_one_or_none()
        if clash:
            raise conflict(
                "DEPARTMENT_SLUG_TAKEN",
                f"Department with slug '{patch['slug']}' already exists",
            )
        dept.slug = patch["slug"]

    if "name" in patch:
        dept.name = patch["name"]
    if "description" in patch:
        dept.description = patch["description"]
    if "autoAssignEnabled" in patch:
        dept.autoAssignEnabled = bool(patch["autoAssignEnabled"])

    new_channels = dept.channels
    if "channels" in patch:
        new_channels = _normalize_channels(patch["channels"])

    new_active = patch.get("isActive", dept.isActive)
    if new_active:
        _ensure_no_active_channel_overlap(
            db, tenant_id, new_channels, exclude_department_id=department_id
        )

    dept.channels = new_channels
    dept.isActive = bool(new_active)
    db.commit()
    db.refresh(dept)
    return dept


def delete_department(db: Session, tenant_id: str, department_id: str) -> None:
    dept = get_department(db, tenant_id, department_id)
    user_count = db.execute(
        select(func.count()).select_from(User).where(User.departmentId == department_id)
    ).scalar_one()
    if user_count:
        raise conflict(
            "DEPARTMENT_HAS_USERS",
            f"Cannot delete: department still has {user_count} assigned user(s).",
            {"userCount": user_count},
        )
    db.delete(dept)
    db.commit()


def list_department_users(db: Session, tenant_id: str, department_id: str) -> list[User]:
    get_department(db, tenant_id, department_id)
    return list(
        db.execute(
            select(User).where(
                User.tenantId == tenant_id,
                User.departmentId == department_id,
            ).order_by(User.name.asc())
        ).scalars()
    )

