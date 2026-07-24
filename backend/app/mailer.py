"""Transactional email. `send_email` dispatches to a backend chosen by
`mail_backend` (auto | resend | smtp | console): Resend (a hosted email API — the
easiest path for a pilot), SMTP (an institution's own Google Workspace / Microsoft
365 / relay, via stdlib `smtplib`), or console (log-only, for dev — so local flows
still work end-to-end without any provider). A backend failure never propagates —
a login or approval must not 500 because email is down — it is logged and
`send_email` returns False.

All three emails share one Outlook-safe HTML shell (`_email_document`) and a
bulletproof MSO button (`_button`): a real `<!DOCTYPE>` + `<head>`, a full-bleed
`role="presentation"` table layout (a teal header band edge-to-edge, no floating
card — the message reads as part of the inbox), web-safe fonts (Arial for body /
Georgia for the bookish headings — never `system-ui`, which Outlook's Word engine
renders as Times), and a VML button so the call-to-action keeps its fill + rounded
corners in Outlook desktop. Emails render outside the app's CSP, so inline styles
are fine.

The header carries the real Column mark, sent as an **inline (CID) attachment** —
`_LOGO_PNG`, a cream-shaft/gold-caps rendering of `brand/icon.svg` (a teal shaft
would vanish against the teal band). It ships base64-embedded in this module so
there's no runtime file read, and both transports attach it: Resend via
`attachments=[{content, content_id, …}]`, SMTP via `add_related(..., cid=…)`.
Gmail and Outlook both refuse `data:` URI images, so CID is the only option that
renders everywhere.
"""
from __future__ import annotations

import base64
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
_PAGE_BG = "#fafbf8"   # --panel (the page — full-bleed, no outer canvas/card)
_GOLD = "#d89a4b"      # --accent-2 (the wordmark's rule)
_MONO = "Consolas,'Courier New',monospace"
_SERIF = "Georgia,'Times New Roman',serif"
_SANS = "Arial,Helvetica,sans-serif"

# The Column mark, inline-attached by Content-ID (see the module docstring). 256px
# PNG in cream + gold, regenerated from `brand/icon.svg` with the ImageMagick recipe
# in `brand/`; base64-embedded so sending never touches the filesystem.
_LOGO_CID = "column-mark"
_LOGO_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAQAAAAEACAYAAABccqhmAAADeklEQVR4nO3csVHrQBRAUfEjmKEQeqEPiqIPeqEQ"
    "ZiDjpyR4JK1kM3vPiS2v7H17I1vLAgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABz"
    "ubv1DVzb++vz963vgb/t6eUtcy7+3foGrsnhZ43SnGQCUNpUxlXmJRGAymZyrMLcTB+AwiZyntnnZ/oAAL8TAAgT"
    "AAgTAAgTAAgTAAgTAAgTAAgTAAgTAAgTAAgTAAgTAAgTALhg9qcDTR+A2TcQRkwfgGURAfYpzE0iAMvS2EyOU5mX"
    "xIf8afYnvDCmcvABAAAAAAAAAICJ+NXTQT6/Plb/wvDh/nHX9z7DGlvef+8arJf5L8CZtg711tfPssY17oltBGDQ"
    "3gHdct0Ma4wcZBE4jwAMGB3MNdfPsMYRB1gEziEAOx01kJfeZ4Y1jjy4InA8AYAwAYAwAYAwAYAwAYAwAYAwAYAw"
    "AYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAw"
    "AYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAw"
    "AYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAw"
    "AYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAYAw"
    "Abixh/vHO2v8nTVqBGCnWQb+7DUc2r9NAAaMDvea62dY44gICMk5BGDQ3sHcct0Ma4wcYIf/PL7Yg3x+fXyved3I"
    "MM+wxtr3H1kDAAAAAAAAAAAgJfdLq/fX59W/RKPp6eUtcy5S/wVw+FmjNCeZAJQ2lXGVeUkEoLKZHKswN9MHoLCJ"
    "nGf2+Zk+AMDvBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBADCBAAumP3pQNMHYPYNhBHTB2BZRIB9"
    "CnOTCMCyNDaT41TmJfEhf5r9CS+MqRx8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "mNx/SE7rKSq69L8AAAAASUVORK5CYII="
)
_LOGO_PNG = base64.b64decode(_LOGO_PNG_B64)


def _resolve_backend(s) -> str:
    """Pick the transport. An explicit `mail_backend` wins; `auto` prefers Resend
    (if a key is set), then SMTP (if a host is set), else console (log-only)."""
    backend = (s.mail_backend or "auto").strip().lower()
    if backend == "auto":
        if s.resend_api_key:
            return "resend"
        if s.smtp_host:
            return "smtp"
        return "console"
    if backend not in ("resend", "smtp", "console"):
        log.warning("Unknown MAIL_BACKEND=%r — falling back to console (log-only).", backend)
        return "console"
    return backend


def send_email(to: str, subject: str, html: str, text: str | None = None) -> bool:
    s = get_settings()
    backend = _resolve_backend(s)
    if backend == "console":
        log.warning("[DEV] MAIL_BACKEND=console (no provider configured) — email NOT sent.\n"
                    "  to=%s\n  subject=%s\n  %s", to, subject, text or html)
        return False
    try:
        if backend == "resend":
            _send_resend(s, to, subject, html, text)
        else:  # smtp
            _send_smtp(s, to, subject, html, text)
    except Exception:
        # A mail-provider failure (unverified sending domain, outage, bad/revoked
        # key, SMTP auth/TLS error) must never break the calling flow — a login or
        # an admin approval should not 500 because email is down. Log it (the admin
        # Logs view surfaces this) and report failure so callers can react.
        log.exception("Failed to send email to %s (subject=%r) via %s", to, subject, backend)
        return False
    log.info("sent email to %s via %s: %s", to, backend, subject)
    return True


def _send_resend(s, to: str, subject: str, html: str, text: str | None) -> None:
    import resend
    resend.api_key = s.resend_api_key
    resend.Emails.send({
        "from": s.mail_from,
        "to": [to],
        "subject": subject,
        "html": html,
        **({"text": text} if text else {}),
        # Inline (not downloadable) — a `content_id` is what makes Resend emit it as
        # a related part the header's `cid:` <img> can resolve.
        "attachments": [{
            "content": _LOGO_PNG_B64,
            "filename": "column.png",
            "content_id": _LOGO_CID,
            "content_type": "image/png",
        }],
    })


def _send_smtp(s, to: str, subject: str, html: str, text: str | None) -> None:
    """Send via the institution's own SMTP (Google/Microsoft/relay) using stdlib.
    A multipart/alternative message (plain text + the HTML the reader sees, the HTML
    part itself a multipart/related carrying the CID logo); TLS via STARTTLS (587) or
    implicit SSL (465); auth only when a username is set — some relays authenticate
    by IP and take no login."""
    import smtplib
    import ssl
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = s.mail_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text or "This message requires an HTML-capable email client.")
    msg.add_alternative(html, subtype="html")
    # Wrap the HTML part in a multipart/related so the header's `cid:` <img> resolves.
    msg.get_payload()[-1].add_related(
        _LOGO_PNG, maintype="image", subtype="png", cid=f"<{_LOGO_CID}>")

    if s.smtp_ssl:
        with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout,
                              context=ssl.create_default_context()) as srv:
            if s.smtp_username:
                srv.login(s.smtp_username, s.smtp_password)
            srv.send_message(msg)
        return
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout) as srv:
        if s.smtp_starttls:
            srv.starttls(context=ssl.create_default_context())
        if s.smtp_username:
            srv.login(s.smtp_username, s.smtp_password)
        srv.send_message(msg)


def _button(href: str, label: str) -> str:
    """A bulletproof CTA button. The `<!--[if mso]>` VML `roundrect` gives Outlook
    desktop a real filled, rounded button (Word ignores background/border-radius on
    an `<a>`); every other client uses the styled anchor. Width is fixed so the VML
    and the fallback line up; it grows with the label so long text isn't clipped."""
    w = max(200, 24 + 9 * len(label))
    safe = _esc(label)
    # Escape the URL for the HTML attribute context too (not just the visible
    # label): a stray quote in an href would otherwise break out of the attribute.
    # The link is server-built (app_public_url + a URL-safe token), so this is
    # defense-in-depth, but it's free.
    href = _esc(href)
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


def _wordmark_html() -> str:
    """The app's wordmark, rebuilt in table-and-inline-style HTML: the CID Column
    mark · mono "IPEDS" · gold rule · serif "Oracle" — the same device as
    `frontend/src/Wordmark.jsx`, in cream on the teal band. A table (not flexbox,
    which Outlook's Word engine ignores) keeps the three cells baseline-aligned."""
    return f"""\
<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
<td style="vertical-align:middle;padding-right:12px;"><img src="cid:{_LOGO_CID}" width="30" height="30" alt="" style="display:block;width:30px;height:30px;border:0;" /></td>
<td style="font-family:{_MONO};font-size:15px;font-weight:bold;letter-spacing:2px;color:{_ON_TEAL};vertical-align:middle;padding-right:11px;">IPEDS</td>
<td style="font-family:{_SERIF};font-size:23px;font-weight:bold;color:{_ON_TEAL};letter-spacing:-.01em;vertical-align:middle;border-left:2px solid {_GOLD};padding-left:11px;">Oracle</td>
</tr></table>"""


def _email_document(preheader: str, inner_html: str) -> str:
    """Wrap a body fragment in the Outlook-safe shell: doctype + head (charset,
    viewport, MSO font override), a hidden preheader (inbox preview text), the
    full-bleed teal header band carrying the wordmark, the body, and a footer.
    Full-bleed by design — no centered card, no outer canvas colour: the message
    sits flush in the reading pane instead of floating on a fake page."""
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
<style type="text/css">
body{{margin:0;padding:0;width:100%;background-color:{_PAGE_BG};-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;}}
table{{border-collapse:collapse;}} a{{color:{_TEAL};}}
.pad{{padding:34px 40px;}}
@media only screen and (max-width:600px){{ .pad{{padding:26px 22px !important;}} }}
</style>
</head>
<body style="margin:0;padding:0;background-color:{_PAGE_BG};">
<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;font-size:1px;line-height:1px;color:{_PAGE_BG};">{_esc(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:{_PAGE_BG};">
<tr><td style="background-color:{_TEAL};padding:20px 40px;">{_wordmark_html()}</td></tr>
<tr><td class="pad" style="font-family:{_SANS};font-size:15px;line-height:1.6;color:{_INK};">
{inner_html}
</td></tr>
<tr><td style="padding:18px 40px 26px;border-top:1px solid {_LINE};font-family:{_SANS};font-size:12px;line-height:1.5;color:{_MUTED};">
{PRODUCT_NAME} · U.S. postsecondary data (IPEDS), explored in plain English.
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
<p style="margin:0;font-size:13px;"><a href="{_esc(link)}" style="color:{_TEAL};word-break:break-all;">{_esc(link)}</a></p>
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
<p style="margin:0;font-size:13px;color:{_MUTED};">Or open <a href="{_esc(review_url)}" style="color:{_TEAL};word-break:break-all;">{_esc(review_url)}</a></p>"""
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
Bookmark <a href="{_esc(app_url)}" style="color:{_TEAL};">{_esc(app_url)}</a> — you can request a fresh
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
