"""Mailer contract: provider failures never propagate, and access-request
notifications fan out to every admin.

- send_email must catch any exception from the Resend SDK (unverified domain,
  outage, bad key) and return False rather than letting it break the calling
  login/approval flow.
- admin_recipients must gather every admin (users.is_admin=1, bootstrap or
  runtime-promoted) plus the access_request_to override, deduped.
- send_access_request must attempt a send to every admin in the list.
"""
import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

tmp = tempfile.mkdtemp()
os.environ["APP_DB_PATH"] = str(Path(tmp) / "app.db")
os.environ["ADMIN_EMAILS"] = "boss@example.edu"
os.environ["ACCESS_REQUEST_TO"] = "requests@example.edu"

from app import auth, mailer  # noqa: E402
from app.config import PRODUCT_NAME  # noqa: E402
from app.db import connect, init_db  # noqa: E402

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")
    except Exception as e:  # noqa: BLE001 -- a surviving `s.app_title` read
        # raises AttributeError here (the fake settings deliberately has no
        # such attribute) — report it as a named failure rather than crashing
        # the whole run, but it's still a hard, loud failure either way.
        FAILURES.append(name)
        print(f"  ✗ {name}: {type(e).__name__}: {e}")


def test_send_email_swallows_provider_errors():
    """A raised exception from resend.Emails.send -> False, not a propagation."""
    orig_get = mailer.get_settings
    import resend
    orig_send = resend.Emails.send
    try:
        mailer.get_settings = lambda: types.SimpleNamespace(
            resend_api_key="re_test_key", mail_from="IPEDS <x@example.com>")

        def boom(*a, **k):
            raise RuntimeError("domain is not verified")

        resend.Emails.send = boom
        result = mailer.send_email("u@example.com", "s", "<p>h</p>", "t")
        assert result is False, f"expected False on provider error, got {result!r}"
    finally:
        mailer.get_settings = orig_get
        resend.Emails.send = orig_send


def test_send_email_no_key_returns_false():
    orig_get = mailer.get_settings
    try:
        mailer.get_settings = lambda: types.SimpleNamespace(
            resend_api_key="", mail_from="x@example.com")
        assert mailer.send_email("u@example.com", "s", "<p>h</p>") is False
    finally:
        mailer.get_settings = orig_get


def test_admin_recipients_includes_all_admins_deduped():
    init_db()
    con = connect()
    try:
        # A second admin promoted at runtime, and a non-admin who must be excluded.
        con.execute("INSERT INTO users(email, is_admin, created_at) "
                    "VALUES ('dean@example.edu', 1, 0) "
                    "ON CONFLICT(email) DO UPDATE SET is_admin=1")
        con.execute("INSERT INTO users(email, is_admin, created_at) "
                    "VALUES ('student@example.edu', 0, 0) "
                    "ON CONFLICT(email) DO NOTHING")
        con.commit()
        recips = auth.admin_recipients(con)
        assert "boss@example.edu" in recips, recips        # bootstrap admin
        assert "dean@example.edu" in recips, recips         # runtime admin
        assert "requests@example.edu" in recips, recips     # access_request_to
        assert "student@example.edu" not in recips, recips  # non-admin excluded
        assert len(recips) == len(set(recips)), f"duplicates: {recips}"
    finally:
        con.close()


def test_send_access_request_fans_out_to_every_admin():
    sent = []
    orig = mailer.send_email
    try:
        mailer.send_email = lambda to, *a, **k: sent.append(to) or True
        ok = mailer.send_access_request(
            ["a@example.edu", "b@example.edu"], "new@person.com")
        assert ok is True, "expected True when all sends succeed"
        assert sent == ["a@example.edu", "b@example.edu"], sent
    finally:
        mailer.send_email = orig


def test_send_access_request_empty_is_falsey_noop():
    sent = []
    orig = mailer.send_email
    try:
        mailer.send_email = lambda to, *a, **k: sent.append(to) or True
        assert mailer.send_access_request([], "new@person.com") is False
        assert sent == [], "no admins -> no sends"
    finally:
        mailer.send_email = orig


def _settings_with_no_app_title(**overrides):
    """A settings stand-in carrying everything mailer.py legitimately needs
    (ttl, public URL) but DELIBERATELY missing `app_title` — PRODUCT_NAME is a
    fixed constant now, not institution-configurable, so mailer.py must never
    read `s.app_title` again. If it still does, this SimpleNamespace raises
    AttributeError instead of silently supplying a stale/wrong value."""
    base = dict(magic_link_ttl_minutes=15, app_public_url="https://ipeds.example.edu")
    base.update(overrides)
    return types.SimpleNamespace(**base)


def test_all_three_emails_use_the_product_name_constant():
    """send_magic_link, send_access_request, and send_access_approved must all
    reference the fixed PRODUCT_NAME constant — not a per-install app_title
    setting, which no longer exists on Settings after this change."""
    orig_get_settings, orig_send_email = mailer.get_settings, mailer.send_email
    sent = {}

    def fake_send_email(to, subject, html, text=None):
        sent[to] = {"subject": subject, "html": html, "text": text}
        return True

    mailer.get_settings = _settings_with_no_app_title
    mailer.send_email = fake_send_email
    try:
        mailer.send_magic_link("signin@example.com", "https://x/verify?token=abc")
        magic = sent["signin@example.com"]
        assert PRODUCT_NAME in magic["subject"], magic["subject"]

        mailer.send_access_request(["admin@example.edu"], "new@person.com")
        req = sent["admin@example.edu"]
        assert PRODUCT_NAME in req["subject"], req["subject"]

        mailer.send_access_approved("approved@example.com", "https://x/verify?token=abc")
        appr = sent["approved@example.com"]
        assert PRODUCT_NAME in appr["subject"], appr["subject"]
        assert PRODUCT_NAME in appr["html"], appr["html"]
        assert PRODUCT_NAME in appr["text"], appr["text"]
    finally:
        mailer.get_settings = orig_get_settings
        mailer.send_email = orig_send_email


def test_send_access_approved_html_has_no_stale_thumbs_copy():
    """Regression guard for the removed-thumbs-feature class of bug: the
    approval email used to promise "A 👍 or 👎 on any answer helps the
    assistant get better over time", but thumbs feedback was ripped out when
    lessons landed (self-learning now comes from the critic, not a user
    thumbs vote). Check for the whole family of thumbs-related phrasing --
    the glyphs themselves, the words describing the feature, and the specific
    stale sentence -- not just the two emoji characters, so a differently-
    worded reintroduction of the same stale claim still trips this."""
    orig_get_settings, orig_send_email = mailer.get_settings, mailer.send_email
    captured_html = {}

    def fake_send_email(to, subject, html_body, text=None):
        captured_html["html"] = html_body
        return True

    mailer.get_settings = lambda: types.SimpleNamespace(
        magic_link_ttl_minutes=15, app_public_url="https://ipeds.example.edu")
    mailer.send_email = fake_send_email
    try:
        mailer.send_access_approved("new@example.edu", "https://x/verify?token=abc")
    finally:
        mailer.get_settings = orig_get_settings
        mailer.send_email = orig_send_email

    body = captured_html["html"]
    stale_markers = [
        "\U0001F44D",  # 👍 thumbs up
        "\U0001F44E",  # 👎 thumbs down
        "thumbs",
        "upvote",
        "downvote",
        "get better over time",
    ]
    for marker in stale_markers:
        assert marker.lower() not in body.lower(), (
            f"stale thumbs-feedback copy {marker!r} still present in "
            f"send_access_approved's HTML body: {body!r}")


def run():
    print("mailer contract:")
    check("send_email swallows provider errors (returns False)",
          test_send_email_swallows_provider_errors)
    check("send_email with no key returns False", test_send_email_no_key_returns_false)
    check("admin_recipients gathers all admins + override, deduped",
          test_admin_recipients_includes_all_admins_deduped)
    check("send_access_request fans out to every admin",
          test_send_access_request_fans_out_to_every_admin)
    check("send_access_request with no admins is a falsey no-op",
          test_send_access_request_empty_is_falsey_noop)
    check("all three emails use the fixed PRODUCT_NAME constant, never s.app_title",
          test_all_three_emails_use_the_product_name_constant)
    check("send_access_approved HTML has no stale thumbs-feedback copy",
          test_send_access_approved_html_has_no_stale_thumbs_copy)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL MAILER TESTS PASSED")


if __name__ == "__main__":
    run()
