"""EMAIL_DOMAIN access-request gate + GET /api/auth/config contract.

`may_request_access`/`request_login` (app/auth.py): EMAIL_DOMAIN, when set,
restricts unsolicited ACCESS REQUESTS (an address not on the allowlist) to
addresses in that domain — so a stranger can't burn Resend quota or flood the
admin inbox. It is case-insensitive and tolerates a leading '@' in the setting.
It never gates SIGN-IN: an allowlisted address keeps working regardless of its
domain, since the allowlist is the sole authority there. An empty EMAIL_DOMAIN
(the default) preserves today's open-to-any-domain behavior. The response
message must be byte-identical whichever branch is taken -- a distinct message
would let an attacker fingerprint which domain(s) a deployment serves.

GET /api/auth/config is a new unauthenticated endpoint the login form polls to
build its "you@yourschool.edu" placeholder hint; it must expose EXACTLY
{"email_domain": ...} and nothing else, since it needs no session.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

tmp = tempfile.mkdtemp()
os.environ["APP_DB_PATH"] = str(Path(tmp) / "app.db")
os.environ["ADMIN_EMAILS"] = "admin@example.edu"
os.environ["ACCESS_REQUEST_TO"] = ""
# This suite fires many /api/auth/request calls against a handful of
# addresses; keep the per-email/per-IP limiter out of the way (it has its own
# dedicated suite in test_rate_limit.py).
os.environ["AUTH_RATE_MAX_PER_EMAIL"] = "1000"
os.environ["AUTH_RATE_MAX_PER_IP"] = "1000"
os.environ.pop("EMAIL_DOMAIN", None)

from fastapi.testclient import TestClient  # noqa: E402

from app import auth as auth_mod  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connect, init_db  # noqa: E402

get_settings.cache_clear()
init_db()

from app.main import app  # noqa: E402

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


def _set_domain(domain):
    """Set (or clear) EMAIL_DOMAIN and bust the settings cache so the change
    takes effect on the next get_settings() call."""
    if domain:
        os.environ["EMAIL_DOMAIN"] = domain
    else:
        os.environ.pop("EMAIL_DOMAIN", None)
    get_settings.cache_clear()


def _requests_for(email):
    con = connect()
    try:
        return con.execute(
            "SELECT * FROM access_requests WHERE email=?", (email,)).fetchall()
    finally:
        con.close()


def _clear_requests():
    con = connect()
    con.execute("DELETE FROM access_requests")
    con.commit()
    con.close()


def _add_to_allowlist(email):
    con = connect()
    con.execute(
        "INSERT INTO allowlist(email, note, added_by, added_at) VALUES (?,?,?,?) "
        "ON CONFLICT(email) DO NOTHING", (email, "test", "test", 0))
    con.commit()
    con.close()


class _MailSpy:
    """Swap in for auth_mod.send_access_request / send_magic_link, restoring
    the original on exit so suites/tests stay isolated from each other."""

    def __init__(self, attr):
        self.attr = attr
        self.calls = []

    def __enter__(self):
        self._orig = getattr(auth_mod, self.attr)
        setattr(auth_mod, self.attr,
                lambda *a, **k: self.calls.append(a) or True)
        return self

    def __exit__(self, *exc):
        setattr(auth_mod, self.attr, self._orig)


def test_in_domain_request_creates_row_and_notifies_admin():
    _set_domain("example.edu")
    _clear_requests()
    with _MailSpy("send_access_request") as spy:
        with TestClient(app) as c:
            r = c.post("/api/auth/request", json={"email": "newperson@example.edu"})
            assert r.status_code == 200, r.text
    rows = _requests_for("newperson@example.edu")
    assert len(rows) == 1, f"expected one access_requests row, got {rows}"
    assert len(spy.calls) == 1, \
        f"expected exactly one admin-notification send, got {spy.calls}"
    assert spy.calls[0][1] == "newperson@example.edu", spy.calls


def test_out_of_domain_request_makes_no_row_and_no_mail():
    _set_domain("example.edu")
    _clear_requests()
    with _MailSpy("send_access_request") as spy:
        with TestClient(app) as c:
            r = c.post("/api/auth/request", json={"email": "stranger@other.com"})
            assert r.status_code == 200, r.text
    rows = _requests_for("stranger@other.com")
    assert rows == [], f"expected NO access_requests row for out-of-domain address, got {rows}"
    assert spy.calls == [], \
        f"expected NO admin notification for out-of-domain address, got {spy.calls}"


def test_response_message_identical_regardless_of_domain_match():
    """The security-critical assertion: a distinct message between the
    in-domain and out-of-domain branches would let a caller fingerprint which
    domain(s) this deployment serves. Compare the ACTUAL returned bodies to
    each other, not to a hardcoded copy of the string."""
    _set_domain("example.edu")
    _clear_requests()
    with _MailSpy("send_access_request"):
        with TestClient(app) as c:
            in_domain = c.post("/api/auth/request",
                               json={"email": "another@example.edu"})
            out_domain = c.post("/api/auth/request",
                                json={"email": "another@other.com"})
    assert in_domain.status_code == 200, in_domain.text
    assert out_domain.status_code == 200, out_domain.text
    assert in_domain.json() == out_domain.json(), \
        (f"response must be byte-identical across the domain-match branches: "
         f"{in_domain.json()!r} vs {out_domain.json()!r}")


def test_empty_email_domain_accepts_any_domain():
    """Default (unset) EMAIL_DOMAIN preserves today's behavior: an
    out-of-domain, non-allowlisted address still gets an access request filed."""
    _set_domain("")
    _clear_requests()
    with _MailSpy("send_access_request") as spy:
        with TestClient(app) as c:
            r = c.post("/api/auth/request", json={"email": "anyone@wherever.org"})
            assert r.status_code == 200, r.text
    rows = _requests_for("anyone@wherever.org")
    assert len(rows) == 1, f"expected an access_requests row with no EMAIL_DOMAIN set, got {rows}"
    assert len(spy.calls) == 1, spy.calls


def test_allowlisted_out_of_domain_address_still_gets_magic_link():
    """The allowlist is the sole authority on SIGN-IN: an admin/contractor on
    the allowlist but outside EMAIL_DOMAIN must still get their link."""
    _set_domain("example.edu")
    _add_to_allowlist("outsider@elsewhere.net")
    link_captured = {}
    orig_magic = auth_mod.send_magic_link
    orig_access = auth_mod.send_access_request
    auth_mod.send_magic_link = lambda to, link: link_captured.__setitem__("link", link) or True
    auth_mod.send_access_request = lambda *a, **k: True
    try:
        with TestClient(app) as c:
            r = c.post("/api/auth/request", json={"email": "outsider@elsewhere.net"})
            assert r.status_code == 200, r.text
    finally:
        auth_mod.send_magic_link = orig_magic
        auth_mod.send_access_request = orig_access
    assert "link" in link_captured, \
        "allowlisted address outside EMAIL_DOMAIN must still receive a magic link"
    assert "/verify?token=" in link_captured["link"], link_captured
    # And no access-request row was filed for an address that has a real link.
    assert _requests_for("outsider@elsewhere.net") == []


def test_domain_match_is_case_insensitive_and_tolerates_leading_at():
    _set_domain("Example.EDU")
    _clear_requests()
    with _MailSpy("send_access_request") as spy:
        with TestClient(app) as c:
            r = c.post("/api/auth/request", json={"email": "x@example.edu"})
            assert r.status_code == 200, r.text
    rows = _requests_for("x@example.edu")
    assert len(rows) == 1, \
        f"'Example.EDU' setting must match 'x@example.edu' case-insensitively, got {rows}"
    assert len(spy.calls) == 1, spy.calls

    _set_domain("@example.edu")  # leading '@' in the config value itself
    _clear_requests()
    with _MailSpy("send_access_request") as spy2:
        with TestClient(app) as c:
            r = c.post("/api/auth/request", json={"email": "y@example.edu"})
            assert r.status_code == 200, r.text
    rows = _requests_for("y@example.edu")
    assert len(rows) == 1, \
        f"a leading '@' in EMAIL_DOMAIN must be tolerated, got {rows}"
    assert len(spy2.calls) == 1, spy2.calls


def test_auth_config_endpoint_needs_no_session():
    _set_domain("example.edu")
    with TestClient(app) as c:
        r = c.get("/api/auth/config")
    assert r.status_code == 200, r.text
    assert r.json() == {"email_domain": "example.edu"}, r.json()


def test_auth_config_endpoint_exposes_exactly_email_domain():
    """A future edit adding another field to public_config() must not leak it
    through this unauthenticated endpoint without a deliberate test change."""
    _set_domain("example.edu")
    with TestClient(app) as c:
        r = c.get("/api/auth/config")
    assert r.status_code == 200, r.text
    assert set(r.json().keys()) == {"email_domain"}, \
        f"GET /api/auth/config must expose exactly {{'email_domain'}}, got {set(r.json().keys())}"


def test_auth_config_reflects_empty_domain():
    _set_domain("")
    with TestClient(app) as c:
        r = c.get("/api/auth/config")
    assert r.status_code == 200, r.text
    assert r.json() == {"email_domain": ""}, r.json()


def run():
    print("EMAIL_DOMAIN access-request gate + GET /api/auth/config:")
    check("in-domain non-allowlisted request -> row + admin notification",
          test_in_domain_request_creates_row_and_notifies_admin)
    check("out-of-domain non-allowlisted request -> no row, no notification",
          test_out_of_domain_request_makes_no_row_and_no_mail)
    check("response message is byte-identical regardless of domain match",
          test_response_message_identical_regardless_of_domain_match)
    check("empty EMAIL_DOMAIN accepts any domain (today's behavior preserved)",
          test_empty_email_domain_accepts_any_domain)
    check("allowlisted address outside EMAIL_DOMAIN still gets its magic link",
          test_allowlisted_out_of_domain_address_still_gets_magic_link)
    check("domain match is case-insensitive and tolerates a leading '@'",
          test_domain_match_is_case_insensitive_and_tolerates_leading_at)
    check("GET /api/auth/config needs no session", test_auth_config_endpoint_needs_no_session)
    check("GET /api/auth/config exposes exactly {'email_domain'}",
          test_auth_config_endpoint_exposes_exactly_email_domain)
    check("GET /api/auth/config reflects an empty domain",
          test_auth_config_reflects_empty_domain)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL ACCESS-GATE TESTS PASSED")


if __name__ == "__main__":
    run()
