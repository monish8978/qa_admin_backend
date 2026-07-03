"""Department resolution + auto-assignment helpers.

Ports apps/api/src/departments/department-routing.ts. Used by routing_service
and conversations_service.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.master import Department, User
from ..models.tenant import WorkflowQueue

SUPPORTED_CHANNELS = {"CHAT", "EMAIL", "CALL", "SOCIAL"}


def normalize_channels(value: Any) -> list[str]:
    seen: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            if v and isinstance(k, str):
                norm = k.strip().upper()
                if norm in SUPPORTED_CHANNELS and norm not in seen:
                    seen.append(norm)
        return seen
    if not isinstance(value, list):
        return []
    for entry in value:
        if not isinstance(entry, str):
            continue
        norm = entry.strip().upper()
        if norm in SUPPORTED_CHANNELS and norm not in seen:
            seen.append(norm)
    return seen


def _as_record(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _read_str(src: dict[str, Any] | None, key: str) -> str | None:
    if not src:
        return None
    v = src.get(key)
    if isinstance(v, str):
        t = v.strip()
        return t or None
    return None


def _metadata_sources(metadata: Any) -> list[dict[str, Any]]:
    root = _as_record(metadata)
    if not root:
        return []
    nested = [
        _as_record(root.get("department")),
        _as_record(root.get("routing")),
        _as_record(root.get("assignment")),
    ]
    return [root, *(n for n in nested if n)]


def _department_hint(metadata: Any) -> dict[str, str | None]:
    for src in _metadata_sources(metadata):
        did = _read_str(src, "departmentId")
        slug = (
            _read_str(src, "departmentSlug")
            or _read_str(src, "slug")
            or _read_str(src, "department")
        )
        name = _read_str(src, "departmentName") or _read_str(src, "name")
        if did or slug or name:
            return {"departmentId": did, "departmentSlug": slug, "departmentName": name}
    return {"departmentId": None, "departmentSlug": None, "departmentName": None}


def get_active_departments(master: Session, tenant_id: str) -> list[dict[str, Any]]:
    rows = list(
        master.execute(
            select(Department)
            .where((Department.tenantId == tenant_id) & (Department.isActive.is_(True)))
            .order_by(Department.createdAt.asc(), Department.name.asc())
        ).scalars()
    )
    return [
        {
            "id": d.id,
            "name": d.name,
            "slug": d.slug,
            "channels": normalize_channels(d.channels),
            "autoAssignEnabled": bool(d.autoAssignEnabled),
        }
        for d in rows
    ]


def resolve_conversation_department(
    departments: list[dict[str, Any]], channel: str, metadata: Any
) -> dict[str, Any] | None:
    normalized = channel.strip().upper()
    hint = _department_hint(metadata)

    if hint["departmentId"]:
        for d in departments:
            if d["id"] == hint["departmentId"]:
                return d
    if hint["departmentSlug"]:
        s = hint["departmentSlug"].strip().lower()
        for d in departments:
            if d["slug"].lower() == s:
                return d
    if hint["departmentName"]:
        n = hint["departmentName"].strip().lower()
        for d in departments:
            if d["name"].lower() == n:
                return d

    by_channel = [d for d in departments if normalized in d["channels"]]
    return by_channel[0] if len(by_channel) == 1 else None


def select_least_loaded_user(
    master: Session,
    tenant: Session,
    *,
    tenant_id: str,
    department_id: str,
    queue_type: str,
) -> dict[str, Any] | None:
    target_role = "QA" if queue_type == "QA_QUEUE" else "VERIFIER"
    eligible = list(
        master.execute(
            select(User.id, User.name, User.createdAt)
            .where(
                (User.tenantId == tenant_id)
                & (User.departmentId == department_id)
                & (User.status == "ACTIVE")
                & (User.role.in_([target_role, "ADMIN"]))
            )
            .order_by(User.createdAt.asc())
        ).mappings()
    )
    if not eligible:
        return None

    from sqlalchemy import func as sa_func

    counts_rows = tenant.execute(
        select(WorkflowQueue.assignedTo, sa_func.count())
        .where(
            (WorkflowQueue.queueType == queue_type)
            & (WorkflowQueue.departmentId == department_id)
            & (WorkflowQueue.assignedTo.is_not(None))
        )
        .group_by(WorkflowQueue.assignedTo)
    ).all()
    load = {u["id"]: 0 for u in eligible}
    for assigned_to, cnt in counts_rows:
        if assigned_to in load:
            load[assigned_to] = int(cnt)

    sorted_users = sorted(
        eligible, key=lambda u: (load.get(u["id"], 0), u["createdAt"])
    )
    chosen = sorted_users[0]
    return {"id": chosen["id"], "name": chosen["name"]}
