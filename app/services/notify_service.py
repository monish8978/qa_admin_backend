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

TemplateKey = Literal[
    "tenant_ready",
    "user_invited",
    "password_reset",
    "mfa_otp",
    "signup_otp",
    "tenant_signup_admin_alert",
    "tenant_approved",
    "user_created",
    "plan_upgrade_request",
]


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

def _wrap_html(title: str, content_html: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
      background-color: #f8fafc;
      color: #334155;
      margin: 0;
      padding: 0;
      -webkit-font-smoothing: antialiased;
    }}
    .wrapper {{
      width: 100%;
      background-color: #f8fafc;
      padding: 40px 0;
    }}
    .container {{
      max-width: 570px;
      margin: 0 auto;
      background: #ffffff;
      border: 1px solid #e2e8f0;
      border-radius: 16px;
      box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.05), 0 2px 4px -2px rgb(0 0 0 / 0.05);
      overflow: hidden;
    }}
    .header {{
      background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%);
      padding: 32px;
      text-align: center;
    }}
    .header h1 {{
      color: #ffffff;
      font-size: 24px;
      font-weight: 700;
      margin: 0;
      letter-spacing: -0.025em;
    }}
    .content {{
      padding: 40px;
      line-height: 1.6;
    }}
    .content p {{
      margin: 0 0 20px 0;
      font-size: 16px;
      color: #475569;
    }}
    .btn {{
      display: inline-block;
      background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%);
      color: #ffffff !important;
      text-decoration: none;
      padding: 12px 30px;
      border-radius: 8px;
      font-weight: 600;
      font-size: 15px;
      margin: 10px 0 20px 0;
      text-align: center;
      box-shadow: 0 4px 10px rgba(79, 70, 229, 0.2);
    }}
    .otp-container {{
      background-color: #f1f5f9;
      border-radius: 12px;
      padding: 20px;
      text-align: center;
      margin: 20px 0;
      border: 1px dashed #cbd5e1;
    }}
    .otp-code {{
      font-size: 32px;
      font-weight: 800;
      letter-spacing: 0.15em;
      color: #4f46e5;
      margin: 0;
    }}
    .footer {{
      padding: 24px 40px;
      background-color: #f8fafc;
      border-top: 1px solid #e2e8f0;
      text-align: center;
      font-size: 12px;
      color: #64748b;
    }}
    .footer p {{
      margin: 0;
    }}
  </style>
</head>
<body>
  <div class="wrapper">
    <div class="container">
      <div class="header">
        <h1>{title}</h1>
      </div>
      <div class="content">
        {content_html}
      </div>
      <div class="footer">
        <p>© 2026 QA Platform. All rights reserved.</p>
      </div>
    </div>
  </div>
</body>
</html>"""


def _render(template: TemplateKey, ctx: dict[str, Any]) -> tuple[str, str, str]:
    """Return (subject, html, text)."""
    if template == "tenant_ready":
        name = ctx.get("name", "there")
        login = ctx.get("loginUrl", "")
        subject = "Your QA Platform Workspace is Ready"
        text = (
            f"Hi {name},\n\nYour QA Platform workspace has been provisioned and is ready to use.\n"
            f"Sign in: {login}\n\nThe QA Platform team"
        )
        html = _wrap_html(
            title="Workspace Ready",
            content_html=(
                f"<p>Hi {name},</p>"
                f"<p>Great news! Your QA Platform workspace has been provisioned and is ready for use.</p>"
                f"<p>Click the button below to sign in and begin your quality operations:</p>"
                f'<div style="text-align: center;"><a href="{login}" class="btn">Open Dashboard</a></div>'
                f"<p>If you have any questions, feel free to contact our support team.</p>"
                
            )
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
        html = _wrap_html(
            title="Team Invitation",
            content_html=(
                f"<p>Hi {name},</p>"
                f"<p><strong>{inviter}</strong> has invited you to join the <strong>QA Platform</strong> workspace.</p>"
                f"<p>Click the button below to accept the invitation and set up your account password:</p>"
                f'<div style="text-align: center;"><a href="{accept}" class="btn">Accept Invitation</a></div>'
                f"<p><em>Note: This invitation link is secure and will expire in 7 days.</em></p>"
                
            )
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
        html = _wrap_html(
            title="Password Reset Request",
            content_html=(
                f"<p>Hi {name},</p>"
                f"<p>We received a request to reset the password for your QA Platform account.</p>"
                f"<p>To complete your password reset, click the button below:</p>"
                f'<div style="text-align: center; margin: 24px 0;"><a href="{reset}" style="display: inline-block; background-color: #111827; color: #ffffff !important; text-decoration: none; padding: 12px 32px; border-radius: 8px; font-weight: 600; font-size: 15px; text-align: center; box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05); font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, sans-serif;">Reset Password</a></div>'
                f"<p><em>Note: This link will expire in 15 minutes. If you did not make this request, you can safely ignore this email.</em></p>"
                f"<p>Best regards,<br><strong>The QA Platform Team</strong></p>"
            )
        )
        return subject, html, text

    if template == "mfa_otp":
        otp = ctx.get("otp", "")
        subject = f"Your QA Platform Verification Code: {otp}"
        text = (
            f"Hi,\n\nYour verification code is: {otp}\n\n"
            f"This code will expire in 5 minutes.\n"
            f"If you did not request this, please ignore this email."
        )
        html = _wrap_html(
            title="Two-Factor Authentication",
            content_html=(
                f"<p>Hi,</p>"
                f"<p>To complete your sign-in, please enter the following verification code (OTP):</p>"
                f'<div class="otp-container"><p class="otp-code">{otp}</p></div>'
                f"<p><em>Note: This code is valid for 5 minutes. If you did not attempt to sign in to your account, please ignore this email.</em></p>"
                
            )
        )
        return subject, html, text

    if template == "signup_otp":
        otp = ctx.get("otp", "")
        subject = f"Verify your email for QA Platform: {otp}"
        text = (
            f"Hi,\n\nYour email verification code is: {otp}\n\n"
            f"This code will expire in 5 minutes.\n"
            f"If you did not request this, please ignore this email."
        )
        html = _wrap_html(
            title="Email Verification",
            content_html=(
                f"<p>Hi,</p>"
                f"<p>Thank you for starting your registration with QA Platform. Please verify your email address by entering the following code (OTP):</p>"
                f'<div class="otp-container"><p class="otp-code">{otp}</p></div>'
                f"<p><em>Note: This code is valid for 5 minutes. If you did not request this, please ignore this email.</em></p>"
                
            )
        )
        return subject, html, text

    if template == "tenant_signup_admin_alert":
        tenant_name = ctx.get("tenantName", "")
        tenant_slug = ctx.get("tenantSlug", "")
        admin_name = ctx.get("adminName", "")
        admin_email = ctx.get("adminEmail", "")
        plan = ctx.get("plan", "")
        subject = f"New Tenant Registration: {tenant_name}"
        text = (
            f"New Tenant Registered:\n\n"
            f"Tenant Name: {tenant_name}\n"
            f"Workspace Slug: {tenant_slug}\n"
            f"Admin Name: {admin_name}\n"
            f"Admin Email: {admin_email}\n"
            f"Selected Plan: {plan}\n\n"
            f"This is an automated notification. The workspace has been automatically activated."
        )
        html = _wrap_html(
            title="New Tenant Registration",
            content_html=(
                f"<p>A new tenant has registered and their workspace has been automatically activated.</p>"
                f'<table style="width: 100%; border-collapse: collapse; margin: 20px 0; border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden;">'
                f'<tr style="background-color: #f8fafc;"><td style="padding: 10px; border-bottom: 1px solid #e2e8f0; font-weight: bold;">Tenant Name</td><td style="padding: 10px; border-bottom: 1px solid #e2e8f0;">{tenant_name}</td></tr>'
                f'<tr><td style="padding: 10px; border-bottom: 1px solid #e2e8f0; font-weight: bold;">Workspace Slug</td><td style="padding: 10px; border-bottom: 1px solid #e2e8f0;">{tenant_slug}</td></tr>'
                f'<tr style="background-color: #f8fafc;"><td style="padding: 10px; border-bottom: 1px solid #e2e8f0; font-weight: bold;">Admin Name</td><td style="padding: 10px; border-bottom: 1px solid #e2e8f0;">{admin_name}</td></tr>'
                f'<tr><td style="padding: 10px; border-bottom: 1px solid #e2e8f0; font-weight: bold;">Admin Email</td><td style="padding: 10px; border-bottom: 1px solid #e2e8f0;">{admin_email}</td></tr>'
                f'<tr style="background-color: #f8fafc;"><td style="padding: 10px; font-weight: bold;">Selected Plan</td><td style="padding: 10px;">{plan}</td></tr>'
                f"</table>"
                f"<p>No action is required from your side.</p>"
            )
        )
        return subject, html, text

    if template == "tenant_approved":
        tenant_name = ctx.get("tenantName", "")
        name = ctx.get("name", "there")
        subject = f"Your QA Platform Workspace is Approved"
        text = (
            f"Hi {name},\n\n"
            f"Your QA Platform workspace '{tenant_name}' has been approved by the administrator.\n"
            f"You can now login to your account.\n\n"
            f"Best regards,\nThe QA Platform Team"
        )
        html = _wrap_html(
            title="Workspace Approved",
            content_html=(
                f"<p>Hi {name},</p>"
                f"<p>Congratulations! Your QA Platform workspace <strong>{tenant_name}</strong> has been approved by the administrator.</p>"
                f"<p>Your account is now active and you can log in to your dashboard.</p>"
                f'<div style="text-align: center; margin: 24px 0;"><a href="{ctx.get("loginUrl", "")}" style="display: inline-block; background-color: #111827; color: #ffffff !important; text-decoration: none; padding: 12px 32px; border-radius: 8px; font-weight: 600; font-size: 15px; text-align: center; box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05); font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, sans-serif;">Go to Dashboard</a></div>'
                f"<p>Best regards,<br><strong>The QA Platform Team</strong></p>"
            )
        )
        return subject, html, text

    if template == "user_created":
        name = ctx.get("name", "there")
        email = ctx.get("email", "")
        password = ctx.get("password", "")
        dashboard_url = ctx.get("dashboardUrl", "")
        subject = "Welcome to QA Platform - Your Account Details"
        text = (
            f"Hi {name},\n\n"
            f"An account has been created for you on QA Platform.\n\n"
            f"Here are your login credentials:\n"
            f"Email (Username): {email}\n"
            f"Password: {password}\n\n"
            f"You can sign in to your dashboard here: {dashboard_url}\n\n"
            f"For security, please use the 'Forgot Password' option on the sign-in page to set a new password as soon as possible.\n\n"
            f"Best regards,\nThe QA Platform Team"
        )
        html = _wrap_html(
            title="Account Created",
            content_html=(
                f"<p>Hi {name},</p>"
                f"<p>An account has been created for you on the <strong>QA Platform</strong>.</p>"
                f"<p>Here are your access details:</p>"
                f'<table style="width: 100%; border-collapse: collapse; margin: 20px 0; border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden;">'
                f'<tr style="background-color: #f8fafc;"><td style="padding: 12px; border-bottom: 1px solid #e2e8f0; font-weight: bold; font-size: 14px;">Email (Username)</td><td style="padding: 12px; border-bottom: 1px solid #e2e8f0; font-family: monospace; font-size: 14px; color: #334155;">{email}</td></tr>'
                f'<tr><td style="padding: 12px; font-weight: bold; font-size: 14px;">Temporary Password</td><td style="padding: 12px; font-family: monospace; font-size: 14px; color: #334155;">{password}</td></tr>'
                f"</table>"
                f"<p>Click the button below to sign in and open your dashboard:</p>"
                f'<div style="text-align: center; margin: 24px 0;"><a href="{dashboard_url}" style="display: inline-block; background-color: #111827; color: #ffffff !important; text-decoration: none; padding: 12px 32px; border-radius: 8px; font-weight: 600; font-size: 15px; text-align: center; box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05); font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, sans-serif;">Open Dashboard</a></div>'
                f"<p><em>Note: For security, please use the <strong>Forgot Password</strong> option on the sign-in page to set a new password.</em></p>"
                f"<p>Best regards,<br><strong>The QA Platform Team</strong></p>"
            )
        )
        return subject, html, text

    if template == "plan_upgrade_request":
        user_email = ctx.get("user_email", "User")
        tenant_id = ctx.get("tenant_id", "")
        requested_plan = ctx.get("requested_plan", "")
        subject = f"Plan Upgrade Request: Tenant {tenant_id} -> {requested_plan}"
        text = (
            f"Hello Admin,\n\n"
            f"User {user_email} from Tenant Workspace ({tenant_id}) has requested a plan upgrade to {requested_plan}.\n\n"
            f"Please review this request in Super Admin portal.\n\n"
            f"Best regards,\nQA Platform System"
        )
        html = _wrap_html(
            title="Plan Upgrade Request",
            content_html=(
                f"<p>Hello Admin,</p>"
                f"<p>User <strong>{user_email}</strong> from Tenant Workspace <strong>{tenant_id}</strong> has requested a plan upgrade to <strong>{requested_plan}</strong>.</p>"
                f"<p>Please review this request in the Super Admin portal.</p>"
            )
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
def send_email(
    to: str,
    subject: str = "",
    body: str = "",
    *,
    template: str | None = None,
    data: dict | None = None,
    master: Session | None = None,
    tenant_id: str | None = None,
) -> None:
    if master is None:
        log.info("notify(send_email): to=%s subject=%s template=%s", to, subject, template)
        return
    if template:
        send_notification(
            master,
            template=template,  # type: ignore[arg-type]
            to=to,
            context=data or {},
            tenant_id=tenant_id,
        )
    else:
        mailer = _resolve_mailer(master, tenant_id)
        if mailer is None:
            log.info("notify: no mailer configured for direct email to=%s", to)
            return
        try:
            _send(mailer, to, subject, _wrap_html(subject, f"<p>{body}</p>"), body)
        except Exception:
            log.warning("notify: direct send failed for to=%s", to, exc_info=True)
