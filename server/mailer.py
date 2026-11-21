"""Password-reset email delivery. Standard library only (smtplib).

The server has no mail queue and no background worker, so delivery is a plain
blocking SMTP conversation with a short timeout: a school clicking "forgot
password" waits for the send, and a dead mail host must not hold the request
open for minutes. Failures are raised as :class:`MailError` and logged by the
caller; the HTTP response stays deliberately generic either way.
"""
import html
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid


class MailError(Exception):
    """Delivery failed or SMTP is not configured. The message is operator-facing."""


def send_password_reset(cfg, to_email, reset_url, school=""):
    """Email one password-reset link.

    Returns normally when the SMTP server accepted the message; raises
    :class:`MailError` otherwise, never leaking the underlying credentials.
    """
    if not cfg.smtp_host:
        raise MailError("SMTP_HOST is not configured")

    minutes = max(1, int(round(cfg.password_reset_ttl / 60.0)))
    subject = "Reset your Invigismart Pro password"
    greeting = " for %s" % school.strip() if school and school.strip() else ""

    text = (
        "Hello,\n\n"
        "Someone asked to reset the administrator password%s for Invigismart Pro.\n"
        "If that was you, open the link below to choose a new password. It can be\n"
        "used once and expires in %d minutes:\n\n"
        "%s\n\n"
        "If you did not ask for this, you can ignore this email - your current\n"
        "password still works and nothing has changed.\n\n"
        "Invigismart Pro\n"
        % (greeting, minutes, reset_url)
    )
    # The HTML part repeats the same facts; every value interpolated here is
    # escaped, because the school name is user-supplied at signup.
    html_body = (
        "<!doctype html><html><body style=\"margin:0;padding:24px;"
        "font-family:Inter,system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;"
        "background:#f8fafc;color:#0f172a\">"
        "<div style=\"max-width:520px;margin:0 auto;background:#fff;border:1px solid "
        "#e2e8f0;border-radius:16px;padding:28px\">"
        "<h1 style=\"font-size:18px;margin:0 0 12px\">Reset your password</h1>"
        "<p style=\"font-size:14px;line-height:1.6;color:#475569\">"
        "Someone asked to reset the administrator password%s for Invigismart Pro. "
        "If that was you, choose a new password using the button below.</p>"
        "<p style=\"margin:24px 0\"><a href=\"%s\" style=\"display:inline-block;"
        "background:#4f46e5;color:#fff;text-decoration:none;font-weight:600;"
        "padding:12px 20px;border-radius:10px\">Choose a new password</a></p>"
        "<p style=\"font-size:12px;line-height:1.6;color:#64748b\">"
        "This link can be used once and expires in %d minutes. If you did not ask "
        "for this, ignore this email - your current password still works and "
        "nothing has changed.</p>"
        "<p style=\"font-size:12px;line-height:1.6;color:#94a3b8;word-break:break-all\">"
        "If the button does not work, copy this link into your browser:<br>%s</p>"
        "</div></body></html>"
        % (
            html.escape(greeting),
            html.escape(reset_url, quote=True),
            minutes,
            html.escape(reset_url),
        )
    )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr(("Invigismart Pro", cfg.smtp_from))
    message["To"] = to_email
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(
        domain=(cfg.smtp_from.rsplit("@", 1)[-1] or None)
    )
    message.set_content(text)
    message.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    try:
        if cfg.smtp_ssl:
            smtp = smtplib.SMTP_SSL(
                cfg.smtp_host, cfg.smtp_port, timeout=cfg.smtp_timeout, context=context
            )
        else:
            smtp = smtplib.SMTP(
                cfg.smtp_host, cfg.smtp_port, timeout=cfg.smtp_timeout
            )
        with smtp:
            if not cfg.smtp_ssl and cfg.smtp_starttls:
                smtp.starttls(context=context)
            if cfg.smtp_username:
                smtp.login(cfg.smtp_username, cfg.smtp_password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        detail = str(exc) or exc.__class__.__name__
        raise MailError(detail)
