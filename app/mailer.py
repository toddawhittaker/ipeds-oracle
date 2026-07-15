"""Transactional email via Resend. If no API key is configured (dev), the email
is logged to the console instead of sent, so local flows still work end-to-end.
"""
from __future__ import annotations

import logging

from app.config import get_settings

log = logging.getLogger("ipeds.mail")


def send_email(to: str, subject: str, html: str, text: str | None = None) -> bool:
    s = get_settings()
    if not s.resend_api_key:
        log.warning("[DEV] No RESEND_API_KEY — email NOT sent.\n"
                    "  to=%s\n  subject=%s\n  %s", to, subject, text or html)
        return False
    import resend
    resend.api_key = s.resend_api_key
    resend.Emails.send({
        "from": s.mail_from,
        "to": [to],
        "subject": subject,
        "html": html,
        **({"text": text} if text else {}),
    })
    log.info("sent email to %s: %s", to, subject)
    return True


def send_magic_link(to: str, link: str) -> bool:
    ttl = get_settings().magic_link_ttl_minutes
    subject = "Your IPEDS Query sign-in link"
    html = f"""\
<div style="font-family:system-ui,sans-serif;max-width:480px">
  <h2>Sign in to IPEDS Query</h2>
  <p>Click below to sign in. This link works once and expires in {ttl} minutes.</p>
  <p><a href="{link}" style="display:inline-block;padding:10px 18px;
     background:#2b6cb0;color:#fff;border-radius:6px;text-decoration:none">
     Sign in</a></p>
  <p style="color:#666;font-size:13px">If you didn't request this, ignore it.</p>
</div>"""
    text = f"Sign in to IPEDS Query (expires in {ttl} min):\n{link}"
    return send_email(to, subject, html, text)


def send_access_request(admin_to: str, requester: str, reason: str = "") -> bool:
    subject = f"IPEDS Query — access request from {requester}"
    body = (f"{requester} requested access to IPEDS Query.\n\n"
            f"Reason: {reason or '(none given)'}\n\n"
            "Add them in the admin console's Allowlist tab to approve.")
    html = f"<pre style='font-family:system-ui,sans-serif'>{body}</pre>"
    return send_email(admin_to, subject, html, body)


def send_access_approved(to: str, link: str) -> bool:
    """Sent when an admin approves/adds someone: welcomes them with a ready
    sign-in link, plus the app URL as a fallback if that link expires."""
    s = get_settings()
    ttl = s.magic_link_ttl_minutes
    app_url = s.app_public_url
    subject = "You're approved for IPEDS Query"
    html = f"""\
<div style="font-family:system-ui,sans-serif;max-width:480px">
  <h2>Welcome to IPEDS Query</h2>
  <p>Your access has been approved. Click below to sign in — this link works
     once and expires in {ttl} minutes.</p>
  <p><a href="{link}" style="display:inline-block;padding:10px 18px;
     background:#2b6cb0;color:#fff;border-radius:6px;text-decoration:none">
     Sign in</a></p>
  <p style="color:#666;font-size:13px">If the link has expired, go to
     <a href="{app_url}">{app_url}</a> and enter your email for a fresh one.</p>
</div>"""
    text = (f"Your access to IPEDS Query is approved. Sign in (expires in "
            f"{ttl} min):\n{link}\n\nIf that link has expired, go to {app_url} "
            "and enter your email.")
    return send_email(to, subject, html, text)
