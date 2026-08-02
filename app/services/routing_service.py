"""Application-to-department routing — mirrors apps/api/src/routing/routing.service.ts."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..common.exceptions import bad_request, not_found
from ..models.master import (
    AppDepartmentMapping,
    AppDepartmentRoutingAudit,
    ConversationRoutingSetting,
    Department,
)
from .department_routing import resolve_conversation_department
from .tenant_pool import get_tenant_pool

ROUTING_FALLBACK_MODES = ("REJECT", "UNASSIGNED_QUEUE", "FALLBACK_DEPARTMENT")
ASSIGNMENT_MODES = ("ROUND_ROBIN", "MANUAL")
SUPPORTED_CHANNELS = ("CHAT", "EMAIL", "CALL", "SOCIAL")


@dataclass
class RoutingContext:
    settings: dict[str, Any]
    mapping_by_key: dict[str, dict[str, Any]] = field(default_factory=dict)
    mapping_by_dept_channel: dict[str, dict[str, Any]] = field(default_factory=dict)
    department_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)


# ─── helpers ─────────────────────────────────────────────────────────────────

def _clamp_pct(v: Any) -> int:
    try:
        n = round(float(v))
    except (TypeError, ValueError):
        return 100
    if n < 0:
        return 0
    if n > 100:
        return 100
    return n


def _norm_key(s: str) -> str:
    return s.strip().lower()


def _norm_channel(s: str) -> str:
    return s.strip().upper()


def _as_record(v: Any) -> dict[str, Any] | None:
    return v if isinstance(v, dict) else None


def _extract_source_key(metadata: Any) -> str | None:
    root = _as_record(metadata)
    if not root:
        return None
    sources = [root]
    for k in ("routing", "assignment", "application"):
        rec = _as_record(root.get(k))
        if rec:
            sources.append(rec)
    for src in sources:
        for k in ("source", "applicationKey", "applicationId", "sourceApplication"):
            v = src.get(k)
            if isinstance(v, str) and v.strip():
                return _norm_key(v)
    return None


def _assert_fallback_mode(mode: str | None) -> str | None:
    if mode is None:
        return None
    if mode not in ROUTING_FALLBACK_MODES:
        raise bad_request(
            "INVALID_ROUTING_FALLBACK_MODE",
            f"Fallback mode must be one of: {', '.join(ROUTING_FALLBACK_MODES)}",
        )
    return mode


def _assert_active_department(db: Session, tenant_id: str, department_id: str) -> Department:
    dept = db.execute(
        select(Department).where(
            (Department.tenantId == tenant_id)
            & (Department.id == department_id)
            & (Department.isActive.is_(True))
        )
    ).scalar_one_or_none()
    if dept is None:
        raise bad_request(
            "ROUTING_DEPARTMENT_NOT_ACTIVE",
            "Department must exist and be active to use routing mapping",
        )
    return dept


def _create_audit(
    db: Session,
    *,
    tenant_id: str,
    action: str,
    mapping_id: str | None = None,
    actor_id: str | None = None,
    before: Any = None,
    after: Any = None,
) -> None:
    db.add(
        AppDepartmentRoutingAudit(
            tenantId=tenant_id,
            mappingId=mapping_id,
            action=action,
            actorId=actor_id,
            before=before,
            after=after,
        )
    )


def _serialize_mapping(m: AppDepartmentMapping) -> dict[str, Any]:
    return {
        "id": m.id,
        "tenantId": m.tenantId,
        "applicationKey": m.applicationKey,
        "channel": m.channel,
        "displayName": m.displayName,
        "departmentId": m.departmentId,
        "isActive": m.isActive,
        "assignmentMode": m.assignmentMode,
        "aiProcessingPercentage": m.aiProcessingPercentage,
        "createdAt": m.createdAt.isoformat() if m.createdAt else None,
        "updatedAt": m.updatedAt.isoformat() if m.updatedAt else None,
    }


def _serialize_audit(a: AppDepartmentRoutingAudit) -> dict[str, Any]:
    return {
        "id": a.id,
        "tenantId": a.tenantId,
        "mappingId": a.mappingId,
        "action": a.action,
        "actorId": a.actorId,
        "before": a.before,
        "after": a.after,
        "createdAt": a.createdAt.isoformat() if a.createdAt else None,
    }


# ─── settings ────────────────────────────────────────────────────────────────

def get_settings(db: Session, tenant_id: str) -> dict[str, Any]:
    row = db.execute(
        select(ConversationRoutingSetting).where(
            ConversationRoutingSetting.tenantId == tenant_id
        )
    ).scalar_one_or_none()
    return {
        "enforceAppMapping": bool(row.enforceAppMapping) if row else False,
        "fallbackMode": row.fallbackMode if row else "REJECT",
        "fallbackDepartmentId": row.fallbackDepartmentId if row else None,
        "assignmentMode": row.assignmentMode if row else "ROUND_ROBIN",
    }


def upsert_settings(
    db: Session, tenant_id: str, actor_id: str, dto: dict[str, Any]
) -> dict[str, Any]:
    fallback_mode = _assert_fallback_mode(dto.get("fallbackMode"))
    existing = db.execute(
        select(ConversationRoutingSetting).where(
            ConversationRoutingSetting.tenantId == tenant_id
        )
    ).scalar_one_or_none()

    next_fallback_mode = fallback_mode or (existing.fallbackMode if existing else "REJECT")
    if "fallbackDepartmentId" in dto:
        next_fallback_dept = dto.get("fallbackDepartmentId") or None
    else:
        next_fallback_dept = existing.fallbackDepartmentId if existing else None

    if next_fallback_mode == "FALLBACK_DEPARTMENT":
        if not next_fallback_dept:
            raise bad_request(
                "FALLBACK_DEPARTMENT_REQUIRED",
                "fallbackDepartmentId is required when fallback mode is FALLBACK_DEPARTMENT",
            )
        _assert_active_department(db, tenant_id, next_fallback_dept)
    else:
        next_fallback_dept = None

    before_snapshot = None
    if existing is not None:
        before_snapshot = {
            "enforceAppMapping": existing.enforceAppMapping,
            "fallbackMode": existing.fallbackMode,
            "fallbackDepartmentId": existing.fallbackDepartmentId,
            "assignmentMode": existing.assignmentMode,
        }

    if existing is None:
        row = ConversationRoutingSetting(
            tenantId=tenant_id,
            enforceAppMapping=bool(dto.get("enforceAppMapping", False)),
            fallbackMode=next_fallback_mode,
            fallbackDepartmentId=next_fallback_dept,
            assignmentMode=dto.get("assignmentMode") or "ROUND_ROBIN",
        )
        db.add(row)
    else:
        if "enforceAppMapping" in dto:
            existing.enforceAppMapping = bool(dto["enforceAppMapping"])
        if fallback_mode is not None:
            existing.fallbackMode = next_fallback_mode
        existing.fallbackDepartmentId = next_fallback_dept
        if "assignmentMode" in dto and dto["assignmentMode"]:
            existing.assignmentMode = dto["assignmentMode"]
        row = existing

    db.flush()
    after_snapshot = {
        "enforceAppMapping": row.enforceAppMapping,
        "fallbackMode": row.fallbackMode,
        "fallbackDepartmentId": row.fallbackDepartmentId,
        "assignmentMode": row.assignmentMode,
    }
    _create_audit(
        db,
        tenant_id=tenant_id,
        action="SETTINGS_UPSERT",
        actor_id=actor_id,
        before=before_snapshot,
        after=after_snapshot,
    )
    db.commit()
    db.refresh(row)
    return after_snapshot


# ─── mappings ────────────────────────────────────────────────────────────────

def list_mappings(db: Session, tenant_id: str) -> list[dict[str, Any]]:
    rows = list(
        db.execute(
            select(AppDepartmentMapping, Department)
            .join(Department, AppDepartmentMapping.departmentId == Department.id)
            .where(AppDepartmentMapping.tenantId == tenant_id)
            .order_by(
                AppDepartmentMapping.isActive.desc(),
                AppDepartmentMapping.applicationKey.asc(),
                AppDepartmentMapping.channel.asc(),
            )
        ).all()
    )
    return [
        {
            **_serialize_mapping(m),
            "department": {
                "id": d.id,
                "name": d.name,
                "slug": d.slug,
                "isActive": d.isActive,
            },
        }
        for m, d in rows
    ]


def list_mapping_llm_coverage_stats(
    master: Session, tenant_id: str
) -> list[dict[str, Any]]:
    mappings = list(
        master.execute(
            select(AppDepartmentMapping).where(
                (AppDepartmentMapping.tenantId == tenant_id)
                & (AppDepartmentMapping.isActive.is_(True))
            )
        ).scalars()
    )
    out: list[dict[str, Any]] = []
    pool = get_tenant_pool()
    with pool.session(tenant_id) as ts:
        from sqlalchemy import text

        for m in mappings:
            total = ts.execute(
                text(
                    """
                    SELECT COUNT(*) FROM conversations c
                    JOIN evaluations e ON e."conversationId" = c.id
                    WHERE c.channel = :ch
                      AND c."departmentId" = :did
                      AND (c.metadata->>'source') = :ak
                    """
                ),
                {"ch": m.channel, "did": m.departmentId, "ak": m.applicationKey},
            ).scalar_one()
            ai_routed = ts.execute(
                text(
                    """
                    SELECT COUNT(*) FROM conversations c
                    JOIN evaluations e ON e."conversationId" = c.id
                    WHERE c.channel = :ch
                      AND c."departmentId" = :did
                      AND (c.metadata->>'source') = :ak
                      AND (
                        e."workflowState" IN ('AI_PENDING','AI_IN_PROGRESS','AI_COMPLETED','AI_FAILED')
                        OR e."aiResponseData" IS NOT NULL
                        OR e."aiMetadata" IS NOT NULL
                      )
                    """
                ),
                {"ch": m.channel, "did": m.departmentId, "ak": m.applicationKey},
            ).scalar_one()
            out.append(
                {
                    "mappingId": m.id,
                    "applicationKey": m.applicationKey,
                    "channel": m.channel,
                    "departmentId": m.departmentId,
                    "totalEvaluated": int(total),
                    "aiRouted": int(ai_routed),
                    "qaDirect": max(0, int(total) - int(ai_routed)),
                }
            )
    return out


def create_mapping(
    db: Session, tenant_id: str, actor_id: str, dto: dict[str, Any]
) -> dict[str, Any]:
    application_key = _norm_key(dto["applicationKey"])
    channel = _norm_channel(dto["channel"])
    _assert_active_department(db, tenant_id, dto["departmentId"])

    existing = db.execute(
        select(AppDepartmentMapping).where(
            (AppDepartmentMapping.tenantId == tenant_id)
            & (AppDepartmentMapping.channel == channel)
            & (AppDepartmentMapping.departmentId == dto["departmentId"])
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise bad_request(
            "APP_MAPPING_EXISTS",
            f'Channel {channel} and selected department already have mapping key "{existing.applicationKey}"',
        )

    dup_key = db.execute(
        select(AppDepartmentMapping).where(
            (AppDepartmentMapping.tenantId == tenant_id)
            & (AppDepartmentMapping.applicationKey == application_key)
        )
    ).scalar_one_or_none()
    if dup_key is not None:
        raise bad_request(
            "APP_KEY_ALREADY_USED",
            f'Application key "{application_key}" is already used for another channel or department',
        )

    mapping = AppDepartmentMapping(
        tenantId=tenant_id,
        applicationKey=application_key,
        channel=channel,
        displayName=(dto.get("displayName") or "").strip() or None,
        departmentId=dto["departmentId"],
        isActive=bool(dto.get("isActive", True)),
        assignmentMode=dto.get("assignmentMode") or "ROUND_ROBIN",
        aiProcessingPercentage=_clamp_pct(dto.get("aiProcessingPercentage", 100)),
        createdById=actor_id,
        updatedById=actor_id,
    )
    db.add(mapping)
    db.flush()
    payload = _serialize_mapping(mapping)
    _create_audit(
        db,
        tenant_id=tenant_id,
        mapping_id=mapping.id,
        action="MAPPING_CREATE",
        actor_id=actor_id,
        after=payload,
    )
    db.commit()
    db.refresh(mapping)
    return list_mappings_for_id(db, tenant_id, mapping.id)


def list_mappings_for_id(db: Session, tenant_id: str, mapping_id: str) -> dict[str, Any]:
    row = db.execute(
        select(AppDepartmentMapping, Department)
        .join(Department, AppDepartmentMapping.departmentId == Department.id)
        .where(
            (AppDepartmentMapping.id == mapping_id)
            & (AppDepartmentMapping.tenantId == tenant_id)
        )
    ).first()
    if row is None:
        raise not_found("APP_MAPPING_NOT_FOUND", "Application mapping not found")
    m, d = row
    return {
        **_serialize_mapping(m),
        "department": {"id": d.id, "name": d.name, "slug": d.slug, "isActive": d.isActive},
    }


def update_mapping(
    db: Session,
    tenant_id: str,
    actor_id: str,
    mapping_id: str,
    dto: dict[str, Any],
) -> dict[str, Any]:
    existing = db.execute(
        select(AppDepartmentMapping).where(
            (AppDepartmentMapping.id == mapping_id)
            & (AppDepartmentMapping.tenantId == tenant_id)
        )
    ).scalar_one_or_none()
    if existing is None:
        raise not_found("APP_MAPPING_NOT_FOUND", "Application mapping not found")

    before = _serialize_mapping(existing)

    next_app_key = (
        _norm_key(dto["applicationKey"])
        if "applicationKey" in dto and dto["applicationKey"] is not None
        else existing.applicationKey
    )
    next_channel = (
        _norm_channel(dto["channel"])
        if "channel" in dto and dto["channel"] is not None
        else existing.channel
    )
    next_dept_id = dto.get("departmentId") or existing.departmentId

    if next_dept_id != existing.departmentId:
        _assert_active_department(db, tenant_id, next_dept_id)

    if next_channel != existing.channel or next_dept_id != existing.departmentId:
        conflict = db.execute(
            select(AppDepartmentMapping).where(
                (AppDepartmentMapping.tenantId == tenant_id)
                & (AppDepartmentMapping.channel == next_channel)
                & (AppDepartmentMapping.departmentId == next_dept_id)
                & (AppDepartmentMapping.id != existing.id)
            )
        ).scalar_one_or_none()
        if conflict is not None:
            raise bad_request(
                "APP_MAPPING_EXISTS",
                f'Channel {next_channel} and selected department already have mapping key "{conflict.applicationKey}"',
            )

    if next_app_key != existing.applicationKey:
        dup = db.execute(
            select(AppDepartmentMapping).where(
                (AppDepartmentMapping.tenantId == tenant_id)
                & (AppDepartmentMapping.applicationKey == next_app_key)
                & (AppDepartmentMapping.id != existing.id)
            )
        ).scalar_one_or_none()
        if dup is not None:
            raise bad_request(
                "APP_KEY_ALREADY_USED",
                f'Application key "{next_app_key}" is already used for another channel or department',
            )

    if "applicationKey" in dto:
        existing.applicationKey = next_app_key
    if "channel" in dto:
        existing.channel = next_channel
    if "displayName" in dto:
        v = (dto["displayName"] or "").strip()
        existing.displayName = v or None
    if "departmentId" in dto:
        existing.departmentId = next_dept_id
    if "isActive" in dto:
        existing.isActive = bool(dto["isActive"])
    if "assignmentMode" in dto and dto["assignmentMode"]:
        existing.assignmentMode = dto["assignmentMode"]
    if "aiProcessingPercentage" in dto:
        existing.aiProcessingPercentage = _clamp_pct(dto["aiProcessingPercentage"])
    existing.updatedById = actor_id

    db.flush()
    after = _serialize_mapping(existing)
    _create_audit(
        db,
        tenant_id=tenant_id,
        mapping_id=existing.id,
        action="MAPPING_UPDATE",
        actor_id=actor_id,
        before=before,
        after=after,
    )
    db.commit()
    return list_mappings_for_id(db, tenant_id, existing.id)


def delete_mapping(db: Session, tenant_id: str, actor_id: str, mapping_id: str) -> None:
    existing = db.execute(
        select(AppDepartmentMapping).where(
            (AppDepartmentMapping.id == mapping_id)
            & (AppDepartmentMapping.tenantId == tenant_id)
        )
    ).scalar_one_or_none()
    if existing is None:
        raise not_found("APP_MAPPING_NOT_FOUND", "Application mapping not found")
    snapshot = _serialize_mapping(existing)
    db.delete(existing)
    db.flush()
    _create_audit(
        db,
        tenant_id=tenant_id,
        mapping_id=None,
        action="MAPPING_DELETE",
        actor_id=actor_id,
        before=snapshot,
    )
    db.commit()


def list_audits(
    db: Session,
    tenant_id: str,
    *,
    page: int = 1,
    limit: int = 20,
    action: str | None = None,
    mapping_id: str | None = None,
) -> dict[str, Any]:
    page = max(page, 1)
    limit = max(min(limit, 100), 1)
    base = select(AppDepartmentRoutingAudit).where(
        AppDepartmentRoutingAudit.tenantId == tenant_id
    )
    count_q = select(func.count()).select_from(AppDepartmentRoutingAudit).where(
        AppDepartmentRoutingAudit.tenantId == tenant_id
    )
    if action:
        base = base.where(AppDepartmentRoutingAudit.action == action)
        count_q = count_q.where(AppDepartmentRoutingAudit.action == action)
    if mapping_id:
        base = base.where(AppDepartmentRoutingAudit.mappingId == mapping_id)
        count_q = count_q.where(AppDepartmentRoutingAudit.mappingId == mapping_id)

    total = db.execute(count_q).scalar_one()
    skip = (page - 1) * limit
    items = list(
        db.execute(
            base.order_by(AppDepartmentRoutingAudit.createdAt.desc())
            .offset(skip)
            .limit(limit)
        ).scalars()
    )
    return {
        "items": [_serialize_audit(a) for a in items],
        "pagination": {
            "page": page,
            "limit": limit,
            "total": int(total),
            "totalPages": (int(total) + limit - 1) // limit,
        },
    }


# ─── routing context + resolution ────────────────────────────────────────────

def load_routing_context(
    db: Session, tenant_id: str, active_departments: list[dict[str, Any]]
) -> RoutingContext:
    settings = get_settings(db, tenant_id)
    mappings = list(
        db.execute(
            select(AppDepartmentMapping).where(
                (AppDepartmentMapping.tenantId == tenant_id)
                & (AppDepartmentMapping.isActive.is_(True))
            )
        ).scalars()
    )

    mapping_by_key: dict[str, dict[str, Any]] = {}
    mapping_by_dept_channel: dict[str, dict[str, Any]] = {}
    for m in mappings:
        norm_key = _norm_key(m.applicationKey)
        norm_channel = m.channel.upper()
        ai_pct = _clamp_pct(m.aiProcessingPercentage)
        composite = f"{norm_key}:{norm_channel}"
        mapping_by_key[composite] = {
            "id": m.id,
            "applicationKey": norm_key,
            "channel": norm_channel,
            "departmentId": m.departmentId,
            "isActive": bool(m.isActive),
            "assignmentMode": m.assignmentMode,
            "aiProcessingPercentage": ai_pct,
        }
        mapping_by_dept_channel[f"{m.departmentId}:{norm_channel}"] = {
            "id": m.id,
            "departmentId": m.departmentId,
            "channel": norm_channel,
            "aiProcessingPercentage": ai_pct,
        }

    department_by_id = {d["id"]: d for d in active_departments}
    return RoutingContext(
        settings=settings,
        mapping_by_key=mapping_by_key,
        mapping_by_dept_channel=mapping_by_dept_channel,
        department_by_id=department_by_id,
    )


def resolve_llm_auto_processing_percent(
    ctx: RoutingContext, *, channel: str, department_id: str | None
) -> int:
    if not department_id:
        return 100
    mapping = ctx.mapping_by_dept_channel.get(
        f"{department_id}:{_norm_channel(channel)}"
    )
    if not mapping:
        return 100
    return _clamp_pct(mapping["aiProcessingPercentage"])


def _validate_department_channel(department: dict[str, Any], channel: str) -> None:
    norm = _norm_channel(channel)
    if norm not in (department.get("channels") or []):
        raise bad_request(
            "APP_MAPPING_CHANNEL_NOT_ALLOWED",
            f"Mapped department does not support channel {norm}",
        )


def resolve_department_for_conversation(
    ctx: RoutingContext,
    *,
    channel: str,
    metadata: Any,
    legacy_department: dict[str, Any] | None,
) -> dict[str, Any] | None:
    source_key = _extract_source_key(metadata)
    norm_channel = _norm_channel(channel)
    composite = f"{source_key}:{norm_channel}" if source_key else None
    mapping = ctx.mapping_by_key.get(composite) if composite else None

    if mapping:
        mapped = ctx.department_by_id.get(mapping["departmentId"])
        if mapped is None:
            raise bad_request(
                "APP_MAPPING_DEPARTMENT_INACTIVE",
                "Mapped department is not active for this tenant",
            )
        _validate_department_channel(mapped, channel)
        return mapped

    if not ctx.settings["enforceAppMapping"]:
        return legacy_department

    mode = ctx.settings["fallbackMode"]
    if mode == "UNASSIGNED_QUEUE":
        return None
    if mode == "FALLBACK_DEPARTMENT":
        fid = ctx.settings.get("fallbackDepartmentId")
        if not fid:
            raise bad_request(
                "FALLBACK_DEPARTMENT_NOT_CONFIGURED",
                "Fallback department is not configured",
            )
        dept = ctx.department_by_id.get(fid)
        if dept is None:
            raise bad_request(
                "FALLBACK_DEPARTMENT_INACTIVE",
                "Fallback department is inactive or unavailable",
            )
        _validate_department_channel(dept, channel)
        return dept

    # REJECT
    code = "APP_MAPPING_NOT_FOUND" if source_key else "APP_MAPPING_SOURCE_REQUIRED"
    msg = (
        f'No department mapping configured for application source "{source_key}"'
        if source_key
        else "metadata.source is required when strict application routing is enabled"
    )
    raise bad_request(code, msg)


def resolve_department_with_assignment_mode(
    ctx: RoutingContext,
    *,
    channel: str,
    metadata: Any,
    legacy_department: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str]:
    source_key = _extract_source_key(metadata)
    composite = f"{source_key}:{_norm_channel(channel)}" if source_key else None
    mapping = ctx.mapping_by_key.get(composite) if composite else None

    if mapping:
        mapped = ctx.department_by_id.get(mapping["departmentId"])
        if mapped is None:
            raise bad_request(
                "APP_MAPPING_DEPARTMENT_INACTIVE",
                "Mapped department is not active for this tenant",
            )
        _validate_department_channel(mapped, channel)
        return mapped, mapping["assignmentMode"]

    dept = resolve_department_for_conversation(
        ctx, channel=channel, metadata=metadata, legacy_department=legacy_department
    )
    return dept, ctx.settings["assignmentMode"]


def preview_route(
    db: Session,
    tenant_id: str,
    *,
    channel: str,
    application_key: str | None,
    metadata: dict[str, Any] | None,
    active_departments: list[dict[str, Any]],
    legacy_department: dict[str, Any] | None,
) -> dict[str, Any]:
    meta: dict[str, Any] | None
    if application_key and not metadata:
        meta = {"source": application_key}
    elif application_key:
        meta = {**(metadata or {}), "source": application_key}
    else:
        meta = metadata

    ctx = load_routing_context(db, tenant_id, active_departments)
    department = resolve_department_for_conversation(
        ctx, channel=channel, metadata=meta, legacy_department=legacy_department
    )
    source_key = _extract_source_key(meta)
    composite = f"{source_key}:{_norm_channel(channel)}" if source_key else None
    mapping = ctx.mapping_by_key.get(composite) if composite else None
    return {
        "source": source_key,
        "strictMode": ctx.settings["enforceAppMapping"],
        "fallbackMode": ctx.settings["fallbackMode"],
        "matchedMappingId": mapping["id"] if mapping else None,
        "department": None
        if department is None
        else {"id": department["id"], "name": department["name"], "slug": department["slug"]},
    }
