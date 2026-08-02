"""Celery task: tenant.provision — port of tenant-provision.worker.ts.

Steps (must remain idempotent at the unit-test level):
  1. Generate random db user/name/password.
  2. Connect as TENANT_DB_SUPERUSER and CREATE USER + CREATE DATABASE + GRANT.
  3. Persist encrypted password + db credentials on the master tenant row.
  4. Run `pnpm prisma migrate deploy` against the new tenant DB.
  5. Seed starter form template, escalation rule, blind-review settings.
  6. Mark admin user + tenant ACTIVE.
  7. Best-effort notify the admin via send_notification(template="tenant_ready").
"""
from __future__ import annotations

import base64
import logging
import os
import secrets
import subprocess
from pathlib import Path
from typing import Any

import psycopg
from sqlalchemy import select

from ..celery_app import celery_app
from ..common.encryption import encrypt
from ..config import get_settings
from ..db import SessionLocal
from ..models.master import BlindReviewSettings, EscalationRule, Tenant, User
from .notify_service import send_notification
from .tenant_pool import get_tenant_pool

log = logging.getLogger("qa.worker.provision")





def _generate_credentials(tenant_id: str) -> tuple[str, str, str]:
    safe = tenant_id.replace("-", "_")[:24]
    db_name = f"qa_tenant_{safe}"
    db_user = f"qa_user_{safe}"
    raw = secrets.token_bytes(24)
    db_password = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return db_name, db_user, db_password


def _create_database(db_name: str, db_user: str, db_password: str) -> None:
    settings = get_settings()
    conn = psycopg.connect(
        host=settings.TENANT_DB_HOST,
        port=settings.TENANT_DB_PORT,
        user=settings.TENANT_DB_SUPERUSER,
        password=settings.TENANT_DB_SUPERUSER_PASSWORD,
        dbname="postgres",
        autocommit=True,
        sslmode="require" if settings.NODE_ENV == "production" else "prefer",
    )
    try:
        with conn.cursor() as cur:
            escaped_password = db_password.replace("'", "''")
            # Idempotent: Postgres has no CREATE USER/DATABASE IF NOT EXISTS, so
            # guard each step. This lets the task be safely retried after a
            # partial failure without erroring on already-created objects.
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (db_user,))
            if cur.fetchone() is None:
                cur.execute(f'CREATE USER "{db_user}" WITH PASSWORD \'{escaped_password}\'')
            else:
                cur.execute(f'ALTER USER "{db_user}" WITH PASSWORD \'{escaped_password}\'')
            cur.execute(f'GRANT "{db_user}" TO CURRENT_USER')
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            if cur.fetchone() is None:
                cur.execute(f'CREATE DATABASE "{db_name}" OWNER "{db_user}"')
            cur.execute(f'GRANT ALL PRIVILEGES ON DATABASE "{db_name}" TO "{db_user}"')
    finally:
        conn.close()


def _create_tenant_schema(tenant_db_url: str) -> None:
    from sqlalchemy import create_engine
    from ..models.tenant import TenantBase
    
    # Ensure it uses psycopg
    if tenant_db_url.startswith("postgresql://"):
        tenant_db_url = tenant_db_url.replace("postgresql://", "postgresql+psycopg://", 1)
        
    engine = create_engine(tenant_db_url)
    TenantBase.metadata.create_all(engine)
    engine.dispose()


def _seed_starter_form(
    tenant_id: str, admin_user_id: str
) -> None:
    from ..models.tenant import FormDefinition

    pool = get_tenant_pool()
    with pool.session(tenant_id) as ts:
        existing = ts.execute(
            select(FormDefinition).where(FormDefinition.formKey == "starter_template")
        ).scalar_one_or_none()
        if existing is not None:
            return
        ts.add(
            FormDefinition(
                formKey="starter_template",
                version=1,
                name="Starter QA Template",
                description="Default template — customize before publishing",
                status="DRAFT",
                channels=["CHAT", "EMAIL"],
                scoringStrategy={
                    "type": "weighted_sections",
                    "passMark": 70,
                    "scale": 100,
                    "roundingPolicy": "round",
                },
                sections=[
                    {"id": "sec_1", "title": "Communication", "weight": 50, "order": 1},
                    {"id": "sec_2", "title": "Resolution", "weight": 50, "order": 2},
                ],
                questions=[
                    {
                        "id": "q_1",
                        "sectionId": "sec_1",
                        "key": "greeting",
                        "label": "Did the agent greet the customer?",
                        "type": "boolean",
                        "required": True,
                        "weight": 50,
                        "order": 1,
                        "rubric": {"goal": "Professional greeting", "anchors": []},
                    },
                    {
                        "id": "q_2",
                        "sectionId": "sec_1",
                        "key": "tone",
                        "label": "Rate the agent's tone",
                        "type": "rating",
                        "required": True,
                        "weight": 50,
                        "order": 2,
                        "validation": {"min": 1, "max": 5},
                        "rubric": {
                            "goal": "Empathetic and professional",
                            "anchors": [
                                {"value": 1, "label": "Very poor"},
                                {"value": 5, "label": "Excellent"},
                            ],
                        },
                    },
                    {
                        "id": "q_3",
                        "sectionId": "sec_2",
                        "key": "issue_resolved",
                        "label": "Was the issue resolved?",
                        "type": "boolean",
                        "required": True,
                        "weight": 60,
                        "order": 1,
                    },
                    {
                        "id": "q_4",
                        "sectionId": "sec_2",
                        "key": "resolution_time",
                        "label": "How would you rate the resolution speed?",
                        "type": "rating",
                        "required": True,
                        "weight": 40,
                        "order": 2,
                        "validation": {"min": 1, "max": 5},
                    },
                ],
                createdById=admin_user_id,
            )
        )
        ts.commit()


@celery_app.task(
    name="tenant.provision",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    acks_late=True,
)
def tenant_provision(self, *, tenantId: str, adminUserId: str) -> dict[str, Any]:
    log.info("[provision] Starting tenant %s", tenantId)
    settings = get_settings()
    db_name, db_user, db_password = _generate_credentials(tenantId)

    try:
        _create_database(db_name, db_user, db_password)
    except Exception as err:  # noqa: BLE001
        raise RuntimeError(f"Failed to create tenant DB: {err}") from err

    log.info("[provision] DB created: %s", db_name)
    encrypted_password = encrypt(db_password)

    with SessionLocal() as master:
        tenant = master.get(Tenant, tenantId)
        if not tenant:
            raise RuntimeError(f"Tenant {tenantId} missing")
        tenant.dbHost = settings.TENANT_DB_HOST
        tenant.dbPort = settings.TENANT_DB_PORT
        tenant.dbName = db_name
        tenant.dbUser = db_user
        tenant.dbPasswordEnc = encrypted_password
        master.commit()

    from urllib.parse import quote
    tenant_db_url = (
        f"postgresql://{db_user}:{quote(db_password, safe='')}"
        f"@{settings.TENANT_DB_HOST}:{settings.TENANT_DB_PORT}/{db_name}"
    )
    _create_tenant_schema(tenant_db_url)
    log.info("[provision] Schema created for tenant %s", tenantId)

    _seed_starter_form(tenantId, adminUserId)

    with SessionLocal() as master:
        admin = master.get(User, adminUserId)
        if admin:
            admin.status = "ACTIVE"
        # Guard against duplicate inserts when the task is retried after a
        # partial failure (these tables are keyed by tenantId).
        existing_rule = master.execute(
            select(EscalationRule).where(EscalationRule.tenantId == tenantId)
        ).scalar_one_or_none()
        if existing_rule is None:
            master.add(
                EscalationRule(
                    tenantId=tenantId,
                    qaDeviationThreshold=15,
                    verifierDeviationThreshold=10,
                    verifierMinRangeStart=0,
                    verifierMinRangeEnd=40,
                    verifierMaxRangeStart=90,
                    verifierMaxRangeEnd=100,
                    staleQueueHours=24,
                )
            )
        existing_blind = master.execute(
            select(BlindReviewSettings).where(BlindReviewSettings.tenantId == tenantId)
        ).scalar_one_or_none()
        if existing_blind is None:
            master.add(
                BlindReviewSettings(
                    tenantId=tenantId,
                    hideAgentFromQA=False,
                    hideQAFromVerifier=False,
                )
            )
        tenant = master.get(Tenant, tenantId)
        if tenant:
            tenant.status = "ACTIVE"  # Automatically activate upon signup
        master.commit()

        log.info("[provision] Tenant %s is now ACTIVE", tenantId)

        # Send alert email and in-app notification to Super Admin
        from ..models.master import PlatformNotification, PlatformAuditLog

        try:
            # 1. Create In-App Notification for Super Admin
            notif = PlatformNotification(
                title="New Workspace Registered",
                message=f"Workspace '{tenant.name}' ({tenant.slug}) has registered under the {tenant.plan} plan.",
                type="info",
                target_audience="super_admin",
                sent_by="System"
            )
            master.add(notif)

            # 2. Create Audit Log
            audit = PlatformAuditLog(
                user_id=admin.id if admin else None,
                user_email=admin.email if admin else "System",
                action="tenant.registered",
                resource_type="tenant",
                resource_id=tenantId,
                details={
                    "tenantName": tenant.name,
                    "tenantSlug": tenant.slug,
                    "adminName": admin.name if admin else None,
                    "adminEmail": admin.email if admin else None,
                    "plan": tenant.plan if isinstance(tenant.plan, str) else tenant.plan.value,
                },
            )
            master.add(audit)
            master.commit()

            # 2. Send Email Alert
            send_notification(
                master,
                template="tenant_signup_admin_alert",
                to=settings.EMAIL_FROM,
                context={
                    "tenantName": tenant.name,
                    "tenantSlug": tenant.slug,
                    "adminName": admin.name,
                    "adminEmail": admin.email,
                    "plan": tenant.plan,
                },
            )
            log.info("[provision] Sent informational signup alert to %s for tenant: %s", settings.EMAIL_FROM, tenant.slug)
        except Exception as e:
            log.error("[provision] Failed to send signup informational alert to admin: %s", e)

    return {"tenantId": tenantId, "dbName": db_name, "status": "ACTIVE"}

