"""``email`` action: send one message over SMTP using process-level settings.

SMTP credentials come from the environment (``SMTP_HOST``, ``SMTP_PORT``,
``SMTP_USER``, ``SMTP_PASS``, ``SMTP_FROM``) rather than from the workflow
file, so workflow JSON stays shareable. When no SMTP server is configured the
action returns ``{"skipped": true, ...}`` instead of failing, which makes it
safe to include in workflows you test locally.

Config
------
``to``        Recipient string or list (required)
``cc`` / ``bcc``
``subject``   Default "(no subject)"
``from``      Overrides ``SMTP_FROM``
``body``      Plain-text body
``html``      Optional HTML alternative part
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

from openzap.errors import ActionError


def _recipients(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


def run(config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Send the message, or skip cleanly when SMTP is not configured."""
    settings = context.get("_settings")
    smtp = settings.smtp if settings is not None else None
    if smtp is None or not smtp.configured:
        return {
            "skipped": True,
            "reason": "SMTP is not configured (set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS)",
        }

    to = _recipients(config.get("to"))
    if not to:
        raise ActionError("'email' requires a non-empty 'to'")

    message = EmailMessage()
    message["From"] = config.get("from") or smtp.sender
    message["To"] = ", ".join(to)
    cc = _recipients(config.get("cc"))
    bcc = _recipients(config.get("bcc"))
    if cc:
        message["Cc"] = ", ".join(cc)
    if bcc:
        message["Bcc"] = ", ".join(bcc)
    message["Subject"] = config.get("subject") or "(no subject)"
    message.set_content(str(config.get("body") or ""))
    if config.get("html"):
        message.add_alternative(str(config["html"]), subtype="html")

    recipients = to + cc + bcc
    try:
        if smtp.ssl:
            server = smtplib.SMTP_SSL(smtp.host, smtp.port, timeout=smtp.timeout)
        else:
            server = smtplib.SMTP(smtp.host, smtp.port, timeout=smtp.timeout)
            if smtp.starttls:
                try:
                    server.starttls(context=ssl.create_default_context())
                except smtplib.SMTPNotSupportedError:
                    pass  # Server advertises no STARTTLS; continue in clear text.
        with server:
            if smtp.user:
                server.login(smtp.user, smtp.password)
            sent = server.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise ActionError(f"SMTP delivery to {smtp.host}:{smtp.port} failed: {exc}") from exc

    return {
        "skipped": False,
        "sent": True,
        "message_id": message["Message-ID"],
        "recipients": sent,
        "server": f"{smtp.host}:{smtp.port}",
    }
