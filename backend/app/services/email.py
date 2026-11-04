"""Outgoing email: invitations and password-reset links.

Backends (EMAIL_BACKEND):
  disabled  nothing is sent; admins share the link shown on screen (the default)
  console   messages are written to the API log (local development)
  smtp      any SMTP provider. DigitalOcean blocks ports 25, 465 and 587 on droplets, so use a
            provider that also listens on 2525 (Brevo, Mailgun, Postmark, Resend, SendGrid…)
  memory    kept in OUTBOX (tests only)
"""

import html
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from app.config import Settings
from app.models import Role, User

log = logging.getLogger("wob.email")

ROLE_NAMES = {
    Role.ADMIN: "administrator",
    Role.SECRETARIAT: "awards coordinator",
    Role.REVIEWER: "evidence reviewer",
    Role.JUDGE: "judge",
    Role.COMMITTEE: "committee member",
}


class EmailError(Exception):
    pass


@dataclass
class SentEmail:
    to: str
    subject: str
    text: str
    html: str


OUTBOX: list[SentEmail] = []


def enabled(settings: Settings) -> bool:
    return settings.email_backend != "disabled"


def send(settings: Settings, to: str, subject: str, text: str, body_html: str) -> None:
    """Send one message, or raise EmailError. Never logs message bodies (they hold links)."""
    backend = settings.email_backend
    if backend == "disabled":
        raise EmailError("email is not configured (EMAIL_BACKEND=disabled)")
    if backend == "memory":
        if settings.environment == "production":
            raise EmailError("the memory email backend is for tests only")
        OUTBOX.append(SentEmail(to, subject, text, body_html))
        return
    if backend == "console":
        if settings.environment == "production":
            raise EmailError("the console email backend would log secrets in production")
        log.warning("EMAIL (console backend) to=%s subject=%s\n%s", to, subject, text)
        return

    if not settings.smtp_host or not settings.email_from:
        raise EmailError("SMTP_HOST and EMAIL_FROM must be set for EMAIL_BACKEND=smtp")
    message = EmailMessage()
    message["From"] = formataddr((settings.email_from_name, settings.email_from))
    message["To"] = to
    message["Subject"] = subject
    message["Message-ID"] = make_msgid(domain=settings.email_from.split("@")[-1])
    if settings.email_reply_to:
        message["Reply-To"] = settings.email_reply_to
    message.set_content(text)
    message.add_alternative(body_html, subtype="html")

    context = ssl.create_default_context()
    password = settings.smtp_password.get_secret_value() if settings.smtp_password else None
    try:
        if settings.smtp_security == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port, timeout=20, context=context
            )
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
        with server:
            if settings.smtp_security == "starttls":
                server.starttls(context=context)
            if settings.smtp_username:
                server.login(settings.smtp_username, password or "")
            server.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        log.warning("email to %s failed: %s", to, type(exc).__name__)
        raise EmailError(f"the email could not be sent ({type(exc).__name__}: {exc})") from exc


def _judge_invitation(settings: Settings, user: User, invited_by: User | None) -> dict:
    """A judge's first invitation: what judging involves, then the usual set-password notes."""
    who = f"{invited_by.display_name} has invited you" if invited_by else "You have been invited"
    return {
        "subject": "You're invited to the Women on Boards Awards 2026 judging system",
        "intro": f"{who} to the Women on Boards Awards 2026 evaluation system as a judge.",
        "about": (
            "In the system you will find each award category's finalists, with their reports and "
            "supporting documents. You will score each finalist and record your comments there. "
            "Your scores are seen only by the awards team, never by the other judges."
        ),
        "lead": "To get started, create your password:",
        "action": "Create your password",
        "notes": [
            f"This link is personal to you. It works once and expires in "
            f"{settings.invite_ttl_hours} hours. If it expires, contact the awards team and we "
            "will send you a new one.",
            f"Choose a password of at least {settings.password_min_length} characters that "
            "isn't based on your email address.",
            f"Your sign-in email is {user.email}.",
            "Please don't forward this email, because the link gives access to your account.",
        ],
        "next": (
            "Once your password is set, sign in and open Judges' Pack in the menu to begin. You "
            "will receive a separate email with full judging instructions."
        ),
        "closing": "If you weren't expecting this invitation, you can ignore this email.",
    }


def send_invitation(
    settings: Settings, user: User, link: str, invited_by: User | None, *, reset: bool
) -> None:
    """The set-password email for a new account (reset=False) or a new link (reset=True).
    Judges' first invitation also explains what judging involves."""
    hours = settings.invite_ttl_hours
    role = ROLE_NAMES.get(user.role, user.role.value)
    role = f"{'an' if role[0] in 'aeiou' else 'a'} {role}"
    site = settings.email_site_name
    if not reset and user.role == Role.JUDGE:
        m = _judge_invitation(settings, user, invited_by)
    else:
        if reset:
            subject = f"Set a new password for {site}"
            intro = (
                f"A new password link has been created for your {site} account. Your previous "
                "password no longer works, and you have been signed out everywhere."
            )
            action = "Set a new password"
        else:
            subject = f"You're invited to {site}"
            intro = (
                f"{invited_by.display_name} has invited you to {site} as {role}."
                if invited_by
                else f"You have been invited to {site} as {role}."
            )
            action = "Create your password"
        m = {
            "subject": subject,
            "intro": intro,
            "about": None,
            "lead": None,
            "action": action,
            "notes": [],
            "next": None,
            "closing": (
                f"The link works once and expires in {hours} hours. Don't forward this email: "
                "the link gives access to your account. If you weren't expecting it, you can "
                f"ignore it.\n\nYour sign-in email is {user.email}."
            ),
        }

    paragraphs = [m["intro"], m["about"]]
    notes = "".join(f"- {n}\n" for n in m["notes"])
    text = (
        f"Dear {user.display_name},\n\n"
        + "".join(f"{x}\n\n" for x in paragraphs if x)
        + f"{m['lead'] or m['action'] + ':'}\n{link}\n\n"
        + (f"Please note:\n{notes}\n" if notes else "")
        + (f"{m['next']}\n\n" if m["next"] else "")
        + f"{m['closing']}\n\nKind regards,\n{settings.email_from_name}\n"
    )
    e = html.escape
    para = '<p style="margin:0 0 18px;line-height:1.5">{}</p>'
    small = '<p style="margin:0 0 8px;font-size:13px;color:#7a6672;line-height:1.5">{}</p>'
    notes_html = (
        '<p style="margin:0 0 6px;font-weight:bold">Please note</p>'
        '<ul style="margin:0 0 18px;padding-left:20px;line-height:1.5">'
        + "".join(f'<li style="margin:0 0 4px">{e(n)}</li>' for n in m["notes"])
        + "</ul>"
        if m["notes"]
        else ""
    )
    body_html = f"""<!doctype html>
<html><body style="margin:0;background:#f7f2eb;font-family:Arial,Helvetica,sans-serif;color:#2d1b2e">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f7f2eb;padding:24px 12px">
<tr><td align="center">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;background:#fffdf9;border:1px solid #ebe0d1;border-radius:10px">
<tr><td style="background:#3a1340;border-radius:10px 10px 0 0;padding:20px 28px">
<div style="font-family:Georgia,serif;font-size:20px;color:#ffffff">Women on Boards Awards</div>
<div style="font-family:Georgia,serif;font-size:14px;color:#c9a45c">AI-Powered Evaluation and Ranking System</div>
</td></tr>
<tr><td style="padding:28px">
<p style="margin:0 0 14px">Dear {e(user.display_name)},</p>
{"".join(para.format(e(x)) for x in paragraphs if x)}
{para.format(e(m["lead"])) if m["lead"] else ""}
<p style="margin:0 0 22px"><a href="{e(link)}" style="display:inline-block;background:#9a6c22;color:#ffffff;text-decoration:none;padding:12px 22px;border-radius:8px;font-weight:bold">{e(m["action"])}</a></p>
<p style="margin:0 0 8px;font-size:13px;color:#7a6672">If the button doesn't work, paste this link into your browser:</p>
<p style="margin:0 0 22px;font-size:13px;word-break:break-all"><a href="{e(link)}" style="color:#5e2558">{e(link)}</a></p>
{notes_html}
{para.format(e(m["next"])) if m["next"] else ""}
{"".join(small.format(e(x)) for x in m["closing"].split(chr(10) * 2))}
<p style="margin:14px 0 0">Kind regards,<br>{e(settings.email_from_name)}</p>
</td></tr>
</table>
</td></tr>
</table>
</body></html>"""
    send(settings, user.email, m["subject"], text, body_html)
