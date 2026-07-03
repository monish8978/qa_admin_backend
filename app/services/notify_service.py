"""Email notifications — per-tenant SMTP with platform fallback.

Mirrors apps/api/src/notify/notify.service.ts + tenant-email-delivery.service.ts.

Supported templates (rendered inline below):
    tenant_ready, user_invited, password_reset
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..common.encryption import decrypt
from ..config import get_settings
from ..models.master import TenantEmailSettings

log = logging.getLogger("qa.notify")

TemplateKey = Literal["tenant_ready", "user_invited", "password_reset"]


@dataclass
class _Mailer:
    host: str
    port: int
    encryption: str  # NONE | TLS | SSL
    user: str | None
    password: str | None
    from_email: str
    from_name: str | None
    source: Literal["tenant", "platform"]


# ─── template rendering ───────────────────────────────────────────────────────

def _render(template: TemplateKey, ctx: dict[str, Any]) -> tuple[str, str, str]:
    """Return (subject, html, text)."""
    if template == "tenant_ready":
        name = ctx.get("name", "there")
        login = ctx.get("loginUrl", "")
        subject = "Your QA Platform workspace is ready"
        text = (
            f"Hi {name},\n\nYour QA Platform workspace has been provisioned and is ready to use.\n"
            f"Sign in: {login}\n\nThe QA Platform team"
        )
        html = (
            f"<p>Hi {name},</p>"
            f"<p>Your QA Platform workspace has been provisioned and is ready to use.</p>"
            f"<p><a href=\"{login}\">Open the dashboard</a></p>"
            f"<p>— The QA Platform team</p>"
        )
        return subject, html, text

    if template == "user_invited":
        name = ctx.get("name", "")
        inviter = ctx.get("invitedBy", "An administrator")
        accept = ctx.get("acceptUrl", "")
        subject = "You've been invited to QA Platform"
        text = (
            f"Hi {name},\n\n{inviter} invited you to QA Platform.\n"
            f"Accept your invitation: {accept}\n\nThis link expires in 7 days."
        )
        html = (
            f"<p>Hi {name},</p>"
            f"<p><strong>{inviter}</strong> invited you to QA Platform.</p>"
            f"<p><a href=\"{accept}\">Accept invitation</a></p>"
            f"<p><em>This link expires in 7 days.</em></p>"
        )
        return subject, html, text

    if template == "password_reset":
        name = ctx.get("name", "there")
        reset = ctx.get("resetUrl", "")
        subject = "Reset your QA Platform password"
        text = (
            f"Hi {name},\n\nWe received a request to reset your password.\n"
            f"Reset link (valid for 15 minutes): {reset}\n\n"
            f"If you didn't request this, you can ignore this email."
        )
        html = (
            f"<p>Hi {name},</p>"
            f"<p>We received a request to reset your password.</p>"
            f"<p><a href=\"{reset}\">Reset password</a> (valid for 15 minutes)</p>"
            f"<p><em>If you didn't request this, you can ignore this email.</em></p>"
        )
        return subject, html, text

    raise ValueError(f"Unknown template: {template}")


def _is_template_allowed(row: TenantEmailSettings | None, template: TemplateKey) -> bool:
    if row is None:
        return True
    if not row.notificationsEnabled:
        return False
    if template == "password_reset" and not row.forgotPasswordEnabled:
        return False
    return True


def _resolve_mailer(master: Session, tenant_id: str | None) -> _Mailer | None:
    if tenant_id is not None:
        row = master.execute(
            select(TenantEmailSettings).where(TenantEmailSettings.tenantId == tenant_id)
        ).scalar_one_or_none()
        if row and row.smtpHost and row.fromEmail and row.notificationsEnabled:
            password = None
            if row.smtpPassEnc:
                try:
                    password = decrypt(row.smtpPassEnc)
                except Exception:  # noqa: BLE001
                    log.warning("notify: failed to decrypt tenant SMTP password", exc_info=True)
            return _Mailer(
                host=row.smtpHost,
                port=row.smtpPort or 587,
                encryption=(row.encryption or "TLS").upper(),
                user=row.smtpUser,
                password=password,
                from_email=row.fromEmail,
                from_name=row.fromName,
                source="tenant",
            )

    cfg = get_settings()
    if cfg.SMTP_HOST and cfg.SMTP_PORT:
        return _Mailer(
            host=cfg.SMTP_HOST,
            port=cfg.SMTP_PORT,
            encryption="TLS",
            user=cfg.SMTP_USER,
            password=cfg.SMTP_PASS,
            from_email=cfg.EMAIL_FROM,
            from_name="QA Platform",
            source="platform",
        )
    return None


def _send(mailer: _Mailer, to: str, subject: str, html: str, text: str) -> None:
    msg = EmailMessage()
    msg["From"] = (
        f"{mailer.from_name} <{mailer.from_email}>" if mailer.from_name else mailer.from_email
    )
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    if mailer.encryption == "SSL":
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(mailer.host, mailer.port, context=ctx, timeout=20) as smtp:
            if mailer.user and mailer.password:
                smtp.login(mailer.user, mailer.password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(mailer.host, mailer.port, timeout=20) as smtp:
            smtp.ehlo()
            if mailer.encryption == "TLS":
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            if mailer.user and mailer.password:
                smtp.login(mailer.user, mailer.password)
            smtp.send_message(msg)


def send_notification(
    master: Session,
    *,
    template: TemplateKey,
    to: str,
    context: dict[str, Any],
    tenant_id: str | None = None,
) -> bool:
    """Render and deliver. Returns False (and logs) when no mailer is configured.

    Errors are swallowed and logged — the calling code should treat email as
    best-effort so a failing SMTP server never blocks a user-facing action.
    """
    settings_row = None
    if tenant_id is not None:
        settings_row = master.execute(
            select(TenantEmailSettings).where(TenantEmailSettings.tenantId == tenant_id)
        ).scalar_one_or_none()
    if not _is_template_allowed(settings_row, template):
        log.info("notify: template %s disabled for tenant=%s", template, tenant_id)
        return False

    mailer = _resolve_mailer(master, tenant_id)
    if mailer is None:
        log.info(
            "notify: no mailer configured (tenant=%s template=%s to=%s) — logging only",
            tenant_id, template, to,
        )
        return False

    subject, html, text = _render(template, context)
    try:
        _send(mailer, to, subject, html, text)
        log.info("notify: sent template=%s to=%s via=%s", template, to, mailer.source)
        return True
    except Exception:  # noqa: BLE001
        log.warning("notify: send failed (template=%s to=%s)", template, to, exc_info=True)
        return False


# Backwards-compatible shim for the original auth_service stub call site.
def send_email(*, to: str, template: str, data: dict, master: Session | None = None,
               tenant_id: str | None = None) -> None:
    if master is None:
        log.info("notify(stub-call): template=%s to=%s data_keys=%s",
                 template, to, list(data.keys()))
        return
    send_notification(
        master,
        template=template,  # type: ignore[arg-type]
        to=to,
        context=data,
        tenant_id=tenant_id,
    )
