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
os.environ["ADMIN_EMAILS"] = "boss@franklin.edu"
os.environ["ACCESS_REQUEST_TO"] = "requests@franklin.edu"

from app import auth, mailer  # noqa: E402
from app.db import connect, init_db  # noqa: E402

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


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
                    "VALUES ('dean@franklin.edu', 1, 0) "
                    "ON CONFLICT(email) DO UPDATE SET is_admin=1")
        con.execute("INSERT INTO users(email, is_admin, created_at) "
                    "VALUES ('student@franklin.edu', 0, 0) "
                    "ON CONFLICT(email) DO NOTHING")
        con.commit()
        recips = auth.admin_recipients(con)
        assert "boss@franklin.edu" in recips, recips        # bootstrap admin
        assert "dean@franklin.edu" in recips, recips         # runtime admin
        assert "requests@franklin.edu" in recips, recips     # access_request_to
        assert "student@franklin.edu" not in recips, recips  # non-admin excluded
        assert len(recips) == len(set(recips)), f"duplicates: {recips}"
    finally:
        con.close()


def test_send_access_request_fans_out_to_every_admin():
    sent = []
    orig = mailer.send_email
    try:
        mailer.send_email = lambda to, *a, **k: sent.append(to) or True
        ok = mailer.send_access_request(
            ["a@franklin.edu", "b@franklin.edu"], "new@person.com")
        assert ok is True, "expected True when all sends succeed"
        assert sent == ["a@franklin.edu", "b@franklin.edu"], sent
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
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL MAILER TESTS PASSED")


if __name__ == "__main__":
    run()
