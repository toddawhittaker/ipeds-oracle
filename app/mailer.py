"""Transactional email via Resend. If no API key is configured (dev), the email
is logged to the console instead of sent, so local flows still work end-to-end.
"""
from __future__ import annotations

import logging

from app.config import PRODUCT_NAME, get_settings

log = logging.getLogger("ipeds.mail")


def send_email(to: str, subject: str, html: str, text: str | None = None) -> bool:
    s = get_settings()
    if not s.resend_api_key:
        log.warning("[DEV] No RESEND_API_KEY — email NOT sent.\n"
                    "  to=%s\n  subject=%s\n  %s", to, subject, text or html)
        return False
    try:
        import resend
        resend.api_key = s.resend_api_key
        resend.Emails.send({
            "from": s.mail_from,
            "to": [to],
            "subject": subject,
            "html": html,
            **({"text": text} if text else {}),
        })
    except Exception:
        # A mail-provider failure (unverified sending domain, outage, bad/revoked
        # key) must never break the calling flow — a login or an admin approval
        # should not 500 because email is down. Log it (the admin Logs view
        # surfaces this) and report failure so callers can react if they choose.
        log.exception("Failed to send email to %s (subject=%r)", to, subject)
        return False
    log.info("sent email to %s: %s", to, subject)
    return True


def send_magic_link(to: str, link: str) -> bool:
    ttl = get_settings().magic_link_ttl_minutes
    subject = f"Your {PRODUCT_NAME} sign-in link"
    html = f"""\
<div style="font-family:system-ui,sans-serif;max-width:480px">
  <h2>Sign in to {PRODUCT_NAME}</h2>
  <p>Click below to sign in. This link works once and expires in {ttl} minutes.</p>
  <p><a href="{link}" style="display:inline-block;padding:10px 18px;
     background:#2b6cb0;color:#fff;border-radius:6px;text-decoration:none">
     Sign in</a></p>
  <p style="color:#666;font-size:13px">If you didn't request this, ignore it.</p>
</div>"""
    text = f"Sign in to {PRODUCT_NAME} (expires in {ttl} min):\n{link}"
    return send_email(to, subject, html, text)


def send_access_request(admins: list[str], requester: str, reason: str = "") -> bool:
    """Notify every admin that someone requested access, so they can go approve
    (or decline) it in the console. `admins` is the deduped recipient list.
    Returns True only if a notification was sent to every admin."""
    if not admins:
        return False
    subject = f"{PRODUCT_NAME} — access request from {requester}"
    body = (f"{requester} just requested access to {PRODUCT_NAME}.\n\n"
            f"Reason: {reason or '(none given)'}\n\n"
            "Head to the admin console's Allowlist tab to approve or decline "
            "them — approving emails them a sign-in link automatically.")
    html = f"<pre style='font-family:system-ui,sans-serif'>{body}</pre>"
    # Send to each admin independently so one bad address doesn't drop the rest.
    return all([send_email(a, subject, html, body) for a in admins])


def send_access_approved(to: str, link: str) -> bool:
    """Sent when an admin approves/adds someone: a warm onboarding welcome that
    explains what the app does and how to use it, with a ready sign-in link and
    the app URL as a fallback if that link expires."""
    s = get_settings()
    ttl = s.magic_link_ttl_minutes
    app_url = s.app_public_url
    subject = f"Welcome to {PRODUCT_NAME} — you're approved 🎓"
    html = f"""\
<div style="font-family:system-ui,-apple-system,sans-serif;max-width:520px;
     color:#1a202c;line-height:1.5">
  <h2 style="margin:0 0 12px">Welcome to {PRODUCT_NAME} 🎓</h2>
  <p>Your access has been approved. {PRODUCT_NAME} lets you explore U.S. college and
     university data (IPEDS) just by asking questions in plain English — no SQL
     and no spreadsheets required.</p>
  <p style="margin:22px 0">
    <a href="{link}" style="display:inline-block;padding:11px 20px;
       background:#2b6cb0;color:#fff;border-radius:6px;text-decoration:none;
       font-weight:600">Sign in to get started</a></p>
  <p style="color:#666;font-size:13px;margin-top:-8px">This link signs you in
     once and expires in {ttl} minutes.</p>

  <h3 style="margin:26px 0 8px;font-size:15px">Try asking things like:</h3>
  <ul style="padding-left:20px;margin:0 0 16px">
    <li>Top 20 institutions awarding Associate's degrees in Registered Nursing
        over the last 3 years.</li>
    <li>How many Computer Science bachelor's degrees did California public
        universities award last year?</li>
    <li>Which states awarded the most Master's degrees in Education?</li>
  </ul>
  <p style="font-size:14px">Every answer comes with the data behind it — sortable
     tables, inline charts, and one-click CSV export. A 👍 or 👎 on any answer
     helps the assistant get better over time.</p>

  <p style="color:#666;font-size:13px;border-top:1px solid #e2e8f0;
     padding-top:12px;margin-top:22px">The link above expires in {ttl} minutes,
     but don't worry — you're already approved, so if it expires you can just go
     to <a href="{app_url}" style="color:#2b6cb0">{app_url}</a>, enter your email,
     and a fresh sign-in link arrives right away. You can do that any time you
     need to sign in.</p>
</div>"""
    text = (
        f"Welcome to {PRODUCT_NAME} — you're approved!\n\n"
        f"{PRODUCT_NAME} lets you explore U.S. college and university data (IPEDS) "
        "just by asking questions in plain English.\n\n"
        f"Sign in (this link works once and expires in {ttl} min):\n{link}\n\n"
        "Try asking things like:\n"
        "  - Top 20 institutions awarding Associate's degrees in Registered "
        "Nursing over the last 3 years.\n"
        "  - How many Computer Science bachelor's degrees did California public "
        "universities award last year?\n"
        "  - Which states awarded the most Master's degrees in Education?\n\n"
        "Every answer comes with sortable tables, inline charts, and CSV export.\n\n"
        f"The link above expires in {ttl} minutes, but you're already approved — "
        f"if it expires, just go to {app_url}, enter your email, and a fresh "
        "sign-in link arrives right away. Do that any time you need to sign in.")
    return send_email(to, subject, html, text)
