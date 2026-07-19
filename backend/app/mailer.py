"""Transactional email via Resend. If no API key is configured (dev), the email
is logged to the console instead of sent, so local flows still work end-to-end.

All three emails share one Outlook-safe HTML shell (`_email_document`) and a
bulletproof MSO button (`_button`): a real `<!DOCTYPE>` + `<head>`, a 600px
`role="presentation"` table layout, web-safe fonts (Arial for body / Georgia for
the bookish headings — never `system-ui`, which Outlook's Word engine renders as
Times), and a VML button so the call-to-action keeps its fill + rounded corners in
Outlook desktop. Emails render outside the app's CSP, so inline styles are fine.
"""
from __future__ import annotations

import logging
from html import escape as _esc

from app.config import PRODUCT_NAME, get_settings

log = logging.getLogger("ipeds.mail")

# Brand palette — the app's LIGHT-theme "Reading Room" tokens (styles.css). Emails
# render on a light ground regardless of the reader's app theme, so these are fixed.
_TEAL = "#12514c"      # --accent (button / links / header bar)
_ON_TEAL = "#f7f8f4"   # --on-fg (text on a teal fill)
_INK = "#1b2624"       # --text
_MUTED = "#5c6a65"     # --muted
_LINE = "#d8ddd5"      # --line
_PAGE_BG = "#eceee9"   # --bg (outer canvas)
_PANEL = "#fafbf8"     # --panel (the card)
_SERIF = "Georgia,'Times New Roman',serif"
_SANS = "Arial,Helvetica,sans-serif"


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


def _button(href: str, label: str) -> str:
    """A bulletproof CTA button. The `<!--[if mso]>` VML `roundrect` gives Outlook
    desktop a real filled, rounded button (Word ignores background/border-radius on
    an `<a>`); every other client uses the styled anchor. Width is fixed so the VML
    and the fallback line up; it grows with the label so long text isn't clipped."""
    w = max(200, 24 + 9 * len(label))
    safe = _esc(label)
    return f"""\
<!--[if mso]>
<v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" href="{href}" style="height:44px;v-text-anchor:middle;width:{w}px;" arcsize="14%" strokecolor="{_TEAL}" fillcolor="{_TEAL}">
  <w:anchorlock/>
  <center style="color:{_ON_TEAL};font-family:{_SANS};font-size:16px;font-weight:bold;">{safe}</center>
</v:roundrect>
<![endif]-->
<!--[if !mso]><!-- -->
<a href="{href}" style="background-color:{_TEAL};border-radius:6px;color:{_ON_TEAL};display:inline-block;font-family:{_SANS};font-size:16px;font-weight:bold;line-height:44px;text-align:center;text-decoration:none;width:{w}px;-webkit-text-size-adjust:none;mso-hide:all;">{safe}</a>
<!--<![endif]-->"""


def _email_document(preheader: str, inner_html: str) -> str:
    """Wrap a body fragment in the Outlook-safe shell: doctype + head (charset,
    viewport, MSO font override), a hidden preheader (inbox preview text), a teal
    header bar with the product wordmark, the body card, and a footer."""
    return f"""\
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<meta name="x-apple-disable-message-reformatting" />
<title>{PRODUCT_NAME}</title>
<!--[if mso]>
<style type="text/css">body,table,td,a,h1,h2,p,li{{font-family:{_SANS} !important;}}</style>
<![endif]-->
<style type="text/css">body{{margin:0;padding:0;}} a{{color:{_TEAL};}}</style>
</head>
<body style="margin:0;padding:0;background-color:{_PAGE_BG};">
<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;font-size:1px;line-height:1px;color:{_PAGE_BG};">{_esc(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:{_PAGE_BG};">
<tr><td align="center" style="padding:28px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;background-color:{_PANEL};border:1px solid {_LINE};border-radius:10px;">
<tr><td style="background-color:{_TEAL};border-radius:10px 10px 0 0;padding:18px 28px;">
<span style="font-family:{_SERIF};font-size:20px;font-weight:bold;color:{_ON_TEAL};letter-spacing:.2px;">{PRODUCT_NAME}</span>
</td></tr>
<tr><td style="padding:28px;font-family:{_SANS};font-size:15px;line-height:1.55;color:{_INK};">
{inner_html}
</td></tr>
<tr><td style="padding:16px 28px 22px;border-top:1px solid {_LINE};font-family:{_SANS};font-size:12px;line-height:1.5;color:{_MUTED};">
{PRODUCT_NAME} · U.S. postsecondary data (IPEDS), explored in plain English.
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


def _h1(text: str) -> str:
    return (f'<h1 style="margin:0 0 14px;font-family:{_SERIF};font-size:23px;'
            f'font-weight:bold;color:{_INK};">{text}</h1>')


def send_magic_link(to: str, link: str) -> bool:
    ttl = get_settings().magic_link_ttl_minutes
    subject = f"Your {PRODUCT_NAME} sign-in link"
    inner = f"""\
{_h1("Your sign-in link")}
<p style="margin:0 0 22px;">Click the button below to sign in to {PRODUCT_NAME}. This link
works once and expires in {ttl} minutes.</p>
<p style="margin:0 0 22px;">{_button(link, "Sign in")}</p>
<p style="margin:0 0 4px;font-size:13px;color:{_MUTED};">If the button doesn't work, paste this link into your browser:</p>
<p style="margin:0;font-size:13px;"><a href="{link}" style="color:{_TEAL};word-break:break-all;">{link}</a></p>
<p style="margin:18px 0 0;font-size:13px;color:{_MUTED};">If you didn't request this, you can safely ignore this email.</p>"""
    text = (f"Sign in to {PRODUCT_NAME} (works once, expires in {ttl} min):\n{link}\n\n"
            "If you didn't request this, ignore it.")
    return send_email(to, subject, _email_document("Your one-time sign-in link.", inner), text)


def send_access_request(admins: list[str], requester: str) -> bool:
    """Notify every admin that someone requested access, so they can approve (or
    decline) it in the console. `admins` is the deduped recipient list. Returns True
    only if a notification reached every admin. The CTA deep-links straight to the
    Pending requests tab so there's no hunting for it."""
    if not admins:
        return False
    review_url = get_settings().app_public_url.rstrip("/") + "/admin/users/pending"
    who = _esc(requester)
    subject = f"{PRODUCT_NAME} — access request from {requester}"
    inner = f"""\
{_h1("New access request")}
<p style="margin:0 0 16px;"><strong style="color:{_INK};">{who}</strong> just requested access
to {PRODUCT_NAME}.</p>
<p style="margin:0 0 22px;">Review it in the admin console — <strong style="color:{_INK};">approve</strong>
to let them in (they'll get an approval email and can request their own one-time sign-in link),
or <strong style="color:{_INK};">decline</strong> to block further requests from that address.</p>
<p style="margin:0 0 22px;">{_button(review_url, "Review this request")}</p>
<p style="margin:0;font-size:13px;color:{_MUTED};">Or open <a href="{review_url}" style="color:{_TEAL};word-break:break-all;">{review_url}</a></p>"""
    text = (f"{requester} just requested access to {PRODUCT_NAME}.\n\n"
            f"Review it — approve or decline — in the admin console:\n{review_url}\n\n"
            "Approving lets them sign in: they'll get an approval email and can request a "
            "one-time sign-in link themselves.")
    html = _email_document(f"{requester} requested access to {PRODUCT_NAME}.", inner)
    # Send to each admin independently so one bad address doesn't drop the rest.
    return all([send_email(a, subject, html, text) for a in admins])


def send_access_approved(to: str) -> bool:
    """Sent when an admin approves/adds someone (single approve, manual add, or CSV
    import): a warm welcome that tells them they're in and how to sign in. It carries
    NO magic link — approval no longer mints a token. Instead it points at the login
    page, where the person enters their email and gets a fresh one-time link on
    demand, whenever they're ready."""
    app_url = get_settings().app_public_url
    subject = f"You're approved for {PRODUCT_NAME} \U0001F393"
    inner = f"""\
{_h1("You're approved \U0001F393")}
<p style="margin:0 0 16px;">Your access to {PRODUCT_NAME} has been approved. It lets you explore
U.S. college and university data (IPEDS) just by asking questions in plain English — no SQL and
no spreadsheets.</p>
<p style="margin:0 0 22px;">To sign in, head to {PRODUCT_NAME} and enter your email — a one-time
sign-in link arrives right away. There's no password to remember.</p>
<p style="margin:0 0 26px;">{_button(app_url, "Request your sign-in link")}</p>
<p style="margin:0 0 8px;font-family:{_SERIF};font-size:16px;font-weight:bold;color:{_INK};">Try asking things like:</p>
<ul style="margin:0 0 18px;padding-left:22px;color:{_INK};">
<li style="margin:0 0 6px;">Top 20 institutions awarding Associate's degrees in Registered Nursing over the last 3 years.</li>
<li style="margin:0 0 6px;">How many Computer Science bachelor's degrees did California public universities award last year?</li>
<li style="margin:0;">Which states awarded the most Master's degrees in Education?</li>
</ul>
<p style="margin:0 0 18px;">Every answer comes with the data behind it — sortable tables, inline
charts, and one-click CSV export.</p>
<p style="margin:0;font-size:13px;color:{_MUTED};border-top:1px solid {_LINE};padding-top:14px;">
Bookmark <a href="{app_url}" style="color:{_TEAL};">{app_url}</a> — you can request a fresh
sign-in link there any time you need to sign in.</p>"""
    text = (
        f"You're approved for {PRODUCT_NAME}!\n\n"
        f"{PRODUCT_NAME} lets you explore U.S. college and university data (IPEDS) just by "
        "asking questions in plain English.\n\n"
        f"To sign in, go to {app_url}, enter your email, and a one-time sign-in link arrives "
        "right away — no password to remember.\n\n"
        "Try asking things like:\n"
        "  - Top 20 institutions awarding Associate's degrees in Registered Nursing over the "
        "last 3 years.\n"
        "  - How many Computer Science bachelor's degrees did California public universities "
        "award last year?\n"
        "  - Which states awarded the most Master's degrees in Education?\n\n"
        "Every answer comes with sortable tables, inline charts, and CSV export.\n\n"
        f"Bookmark {app_url} — request a fresh sign-in link there any time you need to sign in.")
    return send_email(to, subject, _email_document(f"You're approved for {PRODUCT_NAME}.", inner), text)
