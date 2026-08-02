"""Forms (FormDefinition) service — mirrors apps/api/src/forms/forms.service.ts.

Implements:
  - list / get / create / update
  - status transitions  DRAFT -> PUBLISHED -> DEPRECATED -> ARCHIVED
  - one-published-form-per-(department, channel) invariant
  - legacy ``departmentId`` column presence check (older tenant DBs)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from ..common.enums import PLAN_LIMITS, PlanType
from ..common.exceptions import bad_request, conflict, not_found
from ..models.tenant import FormDefinition

VALID_STATUSES = {"DRAFT", "PUBLISHED", "DEPRECATED", "ARCHIVED"}
VALID_CHANNELS = {"CHAT", "EMAIL", "CALL", "SOCIAL"}

# State machine: action -> set of statuses it may apply from
_TRANSITIONS = {
    "publish":   {"DRAFT", "DEPRECATED"},
    "unpublish": {"PUBLISHED"},
    "deprecate": {"PUBLISHED"},
    "archive":   {"DRAFT", "DEPRECATED"},
    "restore":   {"ARCHIVED"},
}


def _tenant_db_has_department_id(db: Session) -> bool:
    """Older tenant DBs lack the ``departmentId`` column on form_definitions."""
    return bool(
        db.execute(
            text(
                "SELECT EXISTS ("
                "  SELECT 1 FROM information_schema.columns"
                "  WHERE table_name = 'form_definitions' AND column_name = 'departmentId'"
                ")"
            )
        ).scalar_one()
    )


def _enforce_plan_limit(db: Session, plan: str, custom_forms_limit: int | None = None) -> None:
    try:
        limits = PLAN_LIMITS[PlanType(plan)]
    except (ValueError, KeyError):
        limits = PLAN_LIMITS[PlanType.BASIC]
    max_forms = custom_forms_limit if custom_forms_limit is not None else limits.get("forms", PLAN_LIMITS[PlanType.BASIC]["forms"])
    total = db.execute(
        select(func.count()).select_from(FormDefinition).where(
            FormDefinition.status != "ARCHIVED"
        )
    ).scalar_one()
    if total >= max_forms:
        raise conflict(
            "FORM_LIMIT_REACHED",
            f"Your plan allows up to {max_forms} forms. Archive an existing form to add a new one.",
            {"limit": max_forms, "current": total},
        )


def _validate_channels(channels: Any) -> list[str]:
    if not isinstance(channels, list) or not channels:
        raise bad_request("INVALID_CHANNELS", "channels must be a non-empty list")
    out: list[str] = []
    for ch in channels:
        if not isinstance(ch, str) or ch.upper() not in VALID_CHANNELS:
            raise bad_request("INVALID_CHANNELS", f"Unknown channel: {ch}")
        out.append(ch.upper())
    return out


def _ensure_no_published_clash(
    db: Session,
    department_id: str | None,
    channels: list[str],
    *,
    has_dept_col: bool,
    exclude_form_id: str | None = None,
) -> None:
    q = select(FormDefinition).where(FormDefinition.status == "PUBLISHED")
    if exclude_form_id:
        q = q.where(FormDefinition.id != exclude_form_id)
    if has_dept_col:
        if department_id is None:
            q = q.where(FormDefinition.departmentId.is_(None))
        else:
            q = q.where(
                or_(
                    FormDefinition.departmentId == department_id,
                    FormDefinition.departmentId.is_(None),
                )
            )
    for other in db.execute(q).scalars():
        other_channels = other.channels if isinstance(other.channels, list) else []
        overlap = set(channels) & set(other_channels)
        if overlap:
            raise conflict(
                "FORM_CHANNEL_OCCUPIED",
                f"Channel(s) {sorted(overlap)} already used by published form '{other.name}'.",
                {"formId": other.id, "channels": sorted(overlap)},
            )


# ─── public API ───────────────────────────────────────────────────────────────

def list_forms(
    db: Session,
    *,
    status: str | None = None,
    department_id: str | None = None,
    search: str | None = None,
) -> list[FormDefinition]:
    q = select(FormDefinition)
    has_dept_col = _tenant_db_has_department_id(db)
    if status:
        if status not in VALID_STATUSES:
            raise bad_request("INVALID_STATUS", f"Unknown status: {status}")
        q = q.where(FormDefinition.status == status)
    if department_id and has_dept_col:
        q = q.where(FormDefinition.departmentId == department_id)
    if search:
        like = f"%{search}%"
        q = q.where(or_(FormDefinition.name.ilike(like), FormDefinition.formKey.ilike(like)))
    q = q.order_by(FormDefinition.createdAt.desc())
    return list(db.execute(q).scalars())


def get_form(db: Session, form_id: str) -> FormDefinition:
    row = db.execute(
        select(FormDefinition).where(FormDefinition.id == form_id)
    ).scalar_one_or_none()
    if row is None:
        raise not_found("FORM_NOT_FOUND", "Form not found")
    return row


def create_form(
    db: Session,
    *,
    plan: str,
    custom_forms_limit: int | None = None,
    created_by_id: str,
    form_key: str,
    name: str,
    description: str | None,
    channels: list[str],
    scoring_strategy: dict[str, Any],
    sections: list[Any],
    questions: list[Any],
    department_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> FormDefinition:
    if not form_key:
        raise bad_request("INVALID_FORM_KEY", "formKey is required")
    if not name:
        raise bad_request("INVALID_NAME", "name is required")

    _enforce_plan_limit(db, plan, custom_forms_limit=custom_forms_limit)
    norm_channels = _validate_channels(channels)
    has_dept_col = _tenant_db_has_department_id(db)

    # auto-increment version per formKey
    next_version = (
        db.execute(
            select(func.coalesce(func.max(FormDefinition.version), 0)).where(
                FormDefinition.formKey == form_key
            )
        ).scalar_one()
        + 1
    )

    form = FormDefinition(
        formKey=form_key,
        version=next_version,
        name=name,
        description=description,
        status="DRAFT",
        channels=norm_channels,
        scoringStrategy=scoring_strategy,
        sections=sections,
        questions=questions,
        fmetadata=metadata,
        createdById=created_by_id,
    )
    if has_dept_col:
        form.departmentId = department_id
    db.add(form)
    db.commit()
    db.refresh(form)
    return form


def update_form(db: Session, form_id: str, patch: dict[str, Any]) -> FormDefinition:
    form = get_form(db, form_id)
    if form.status not in {"DRAFT", "DEPRECATED"}:
        raise conflict(
            "FORM_NOT_EDITABLE",
            f"Forms in status {form.status} cannot be edited; clone to a new draft instead.",
        )
    if "name" in patch:
        form.name = patch["name"]
    if "description" in patch:
        form.description = patch["description"]
    if "channels" in patch:
        form.channels = _validate_channels(patch["channels"])
    if "scoringStrategy" in patch:
        form.scoringStrategy = patch["scoringStrategy"]
    if "sections" in patch:
        form.sections = patch["sections"]
    if "questions" in patch:
        form.questions = patch["questions"]
    if "metadata" in patch:
        form.fmetadata = patch["metadata"]
    db.commit()
    db.refresh(form)
    return form


def change_status(db: Session, form_id: str, action: str) -> FormDefinition:
    if action not in _TRANSITIONS:
        raise bad_request("INVALID_ACTION", f"Unknown action: {action}")
    form = get_form(db, form_id)
    if form.status not in _TRANSITIONS[action]:
        raise conflict(
            "INVALID_FORM_TRANSITION",
            f"Cannot {action} a form in status {form.status}",
        )

    now = datetime.now(timezone.utc)
    has_dept_col = _tenant_db_has_department_id(db)

    if action == "publish":
        channels = form.channels if isinstance(form.channels, list) else []
        dept_id = getattr(form, "departmentId", None) if has_dept_col else None
        _ensure_no_published_clash(
            db, dept_id, channels, has_dept_col=has_dept_col, exclude_form_id=form.id
        )
        form.status = "PUBLISHED"
        form.publishedAt = now
    elif action == "unpublish":
        form.status = "DRAFT"
        form.publishedAt = None
    elif action == "deprecate":
        form.status = "DEPRECATED"
        form.deprecatedAt = now
    elif action == "archive":
        form.status = "ARCHIVED"
        form.archivedAt = now
    elif action == "restore":
        form.status = "DRAFT"
        form.archivedAt = None

    db.commit()
    db.refresh(form)
    return form
