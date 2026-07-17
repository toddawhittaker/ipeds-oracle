"""EMAIL_DOMAIN access-request gate + GET /api/auth/config contract.

`may_request_access`/`request_login` (backend/app/auth.py): EMAIL_DOMAIN, when set,
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
import time
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
# Explicit empty string, NOT os.environ.pop(...): popping only removes the OS
# env var, so pydantic-settings falls through to whatever EMAIL_DOMAIN a real
# .env supplies (e.g. a deployment's own franklin.edu) instead of the
# Field(default="") this suite means to simulate. An explicit "" always wins
# over .env, giving the same effective empty-domain behavior these tests
# actually care about.
os.environ["EMAIL_DOMAIN"] = ""

from fastapi.testclient import TestClient  # noqa: E402
from starlette.background import BackgroundTasks  # noqa: E402

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
    takes effect on the next get_settings() call.

    "Clear" sets an explicit empty string rather than os.environ.pop(...):
    popping only removes the OS env var, leaving pydantic-settings to fall
    through to a real .env's EMAIL_DOMAIN (if one is set on this box) instead
    of producing the Field(default="") state this is meant to simulate. An
    explicit "" always wins over .env and reproduces the same effective
    empty-domain behavior.
    """
    os.environ["EMAIL_DOMAIN"] = domain or ""
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


def _deny(email):
    """Write a standalone 'denied' row directly (bypassing the admin deny
    endpoint, which lives in backend/app/routers/admin.py and is covered by its own
    suite). This suite's job is app.auth's is_denied()/request_login
    behavior given a denied row exists -- not the endpoint that produces one."""
    con = connect()
    con.execute(
        "INSERT INTO access_requests(email, status, created_at) VALUES (?,?,?)",
        (email, "denied", time.time()))
    con.commit()
    con.close()


def _deny_all_pending(email):
    """Flip every 'pending' row for `email` to 'denied' -- the per-address
    semantics the real admin endpoint implements. Direct SQL here (not the
    endpoint) for the same reason as _deny above."""
    con = connect()
    con.execute(
        "UPDATE access_requests SET status='denied' WHERE email=? AND status='pending'",
        (email,))
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


# ---------------------------------------------------------------------------
# Deny an access request (backend/app/auth.py is_denied + request_login's new branch).
# Not implemented yet -- app.auth has no is_denied and request_login has only
# the allowlisted/may_request_access branches. Each test below drives ONLY
# through the public HTTP surface (POST /api/auth/request) plus direct SQL
# helpers above, never by importing a not-yet-existing symbol, so a failure
# here is a genuine behavior gap (a row got inserted / mail got sent), not an
# ImportError/AttributeError on a name that doesn't exist yet.
# ---------------------------------------------------------------------------

def test_denied_address_files_no_row_and_sends_no_mail():
    _set_domain("example.edu")
    email = "denied1@example.edu"
    _clear_requests()
    _deny(email)
    before = _requests_for(email)
    assert len(before) == 1 and before[0]["status"] == "denied", before

    with _MailSpy("send_access_request") as spy:
        with TestClient(app) as c:
            r = c.post("/api/auth/request", json={"email": email})
            assert r.status_code == 200, r.text
    after = _requests_for(email)
    # NOT `after == []` -- the denied row itself is still there. The
    # assertion is that no NEW row got added on top of it.
    assert len(after) == len(before), \
        (f"a denied address must not get a NEW access_requests row (the "
         f"existing denied row is expected to remain): before={before!r}, "
         f"after={after!r}")
    assert spy.calls == [], \
        f"a denied address must trigger no admin-notification email, got {spy.calls}"


def test_denied_response_is_byte_identical_to_pending_and_out_of_domain():
    """The no-oracle equivalence test -- the security core of this feature.
    Compares the ACTUAL returned bodies/headers to EACH OTHER, never to a
    hardcoded copy of the message string, so an edit to the copy can't make
    this vacuously pass (same reasoning as
    test_response_message_identical_regardless_of_domain_match above, and see
    that test's docstring / lines 149-153 of this file).

    NOT COVERED HERE: the timing side-channel. Per the architect's security
    analysis, a denied address does strictly LESS work than a fresh pending
    one (two cheap SELECTs, no INSERT/commit/Resend call), so it lands in the
    same fast bucket as an out-of-domain stranger -- timing-indistinguishable
    from "out of domain", but timing-DIStinguishable from "fresh pending" by
    wall clock. That slow/fast split already exists on main (out-of-domain
    vs. in-domain-new); this feature doesn't create a new class of it, and
    closing it entirely would mean making the Resend send fire-and-forget on
    every branch -- a separate change, entangled with the open
    access-request-DDOS backlog item. Not this test's job.
    """
    _set_domain("example.edu")
    _clear_requests()
    fresh_pending = "fresh-oracle-check@example.edu"
    denied_addr = "denied-oracle-check@example.edu"
    stranger_addr = "stranger-oracle-check@notexample.com"
    _deny(denied_addr)

    with _MailSpy("send_access_request"), _MailSpy("send_magic_link"):
        with TestClient(app) as c:
            pending = c.post("/api/auth/request", json={"email": fresh_pending})
            denied = c.post("/api/auth/request", json={"email": denied_addr})
            out_of_domain = c.post("/api/auth/request", json={"email": stranger_addr})

    assert pending.status_code == 200, pending.text
    assert denied.status_code == 200, denied.text
    assert out_of_domain.status_code == 200, out_of_domain.text

    assert denied.json() == pending.json() == out_of_domain.json(), \
        (f"denied/pending/out-of-domain response bodies must be byte-identical: "
         f"denied={denied.json()!r} pending={pending.json()!r} "
         f"out_of_domain={out_of_domain.json()!r}")

    dlen, plen, olen = (denied.headers.get("content-length"),
                       pending.headers.get("content-length"),
                       out_of_domain.headers.get("content-length"))
    assert dlen == plen == olen, \
        (f"response content-length must match across all three: denied={dlen} "
         f"pending={plen} out_of_domain={olen}")

    dkeys, pkeys, okeys = (set(denied.headers.keys()), set(pending.headers.keys()),
                          set(out_of_domain.headers.keys()))
    assert dkeys == pkeys == okeys, \
        (f"no branch may grow a distinguishing response header: "
         f"denied={dkeys} pending={pkeys} out_of_domain={okeys}")


def test_denied_then_allowlisted_gets_a_magic_link():
    """Requirement 4: allowlisting a denied address un-blocks it for free,
    because the allowlisted-check MUST run before the denied-check. Pins the
    1-before-2 branch order -- reordering them must turn this red."""
    _set_domain("example.edu")
    email = "reconsidered@example.edu"
    _clear_requests()
    _deny(email)
    _add_to_allowlist(email)

    link_captured = {}
    orig_magic = auth_mod.send_magic_link
    auth_mod.send_magic_link = lambda to, link: link_captured.__setitem__("link", link) or True
    try:
        with TestClient(app) as c:
            r = c.post("/api/auth/request", json={"email": email})
            assert r.status_code == 200, r.text
    finally:
        auth_mod.send_magic_link = orig_magic

    assert "link" in link_captured, \
        "an allowlisted-but-previously-denied address must still get a magic link"
    assert "/verify?token=" in link_captured["link"], link_captured
    # No NEW request row -- the allowlisted branch never inserts one.
    rows = _requests_for(email)
    assert len(rows) == 1, f"expected only the original denied row, got {rows}"


def test_denial_blocks_regardless_of_email_domain_match():
    """Pins the 2-before-3 branch order: the denied check must run BEFORE
    may_request_access, or an in-domain denied address (which would otherwise
    pass the domain check) keeps inserting rows and emailing admins --
    i.e. the feature does nothing. Reordering branches 2 and 3 must turn
    this red."""
    _set_domain("example.edu")
    email = "denied-in-domain@example.edu"  # matches EMAIL_DOMAIN
    _clear_requests()
    _deny(email)
    before = _requests_for(email)
    assert len(before) == 1 and before[0]["status"] == "denied", before

    with _MailSpy("send_access_request") as spy:
        with TestClient(app) as c:
            r = c.post("/api/auth/request", json={"email": email})
            assert r.status_code == 200, r.text
    after = _requests_for(email)
    assert len(after) == len(before), \
        (f"an in-domain denied address must still be blocked (no new row), "
         f"before={before!r} after={after!r}")
    assert spy.calls == [], \
        f"an in-domain denied address must send no admin notification, got {spy.calls}"


def test_multiple_pending_rows_all_denied_together():
    """Pins per-ADDRESS (not per-row) semantics: several pending rows for one
    address, denied together, must ALL block -- a later request from that
    address must not slip through because only one row was denied."""
    _set_domain("example.edu")
    email = "repeat-requester@example.edu"
    _clear_requests()

    with _MailSpy("send_access_request"):
        with TestClient(app) as c:
            for _ in range(3):
                r = c.post("/api/auth/request", json={"email": email})
                assert r.status_code == 200, r.text
    rows = _requests_for(email)
    assert len(rows) == 3, f"expected 3 pending rows before denial, got {rows}"
    assert all(row["status"] == "pending" for row in rows), rows

    _deny_all_pending(email)
    rows_denied = _requests_for(email)
    assert len(rows_denied) == 3, rows_denied
    assert all(row["status"] == "denied" for row in rows_denied), \
        f"expected all 3 rows for {email} to become denied, got {rows_denied}"

    with _MailSpy("send_access_request") as spy2:
        with TestClient(app) as c:
            r4 = c.post("/api/auth/request", json={"email": email})
            assert r4.status_code == 200, r4.text
    rows_final = _requests_for(email)
    assert len(rows_final) == 3, \
        f"a 4th request from a fully-denied address must file no new row, got {rows_final}"
    assert spy2.calls == [], spy2.calls


# ---------------------------------------------------------------------------
# FIX ROUND -- Defect 1 (HIGH, security review, independently confirmed by
# measurement): a timing oracle. With sends stubbed at a 150ms simulated RTT,
# in the DEFAULT config (EMAIL_DOMAIN="", the effective default this whole
# suite is careful to set explicitly rather than rely on ambient env -- see
# _set_domain's docstring above), `may_request_access` returns True for
# EVERYONE, so there is no out-of-domain bucket and denied is the ONLY fast
# (~0ms) path against {allowlisted, fresh-pending, out-of-domain} (~150ms
# each, a real Resend round-trip): fast <=> denied, unambiguously.
#
# The fix: outbound email becomes fire-and-forget on EVERY branch via a
# caller-supplied `tasks` (BackgroundTasks-like: `.add_task(fn, *a, **k)`)
# object threaded from backend/app/routers/auth.py into app.auth.request_login, so
# request_login returns before ANY network I/O happens on ANY branch.
#
# HOW WE TEST THIS WITHOUT FLAKINESS -- IMPORTANT, READ BEFORE "FIXING" THIS:
#   1. NOT a wall-clock timing assertion. Those flake under CI load and drift
#      with model/library changes; they also can't distinguish "genuinely
#      fixed" from "coincidentally fast on this box today".
#   2. NOT driven through TestClient. Starlette's TestClient runs a response's
#      scheduled BackgroundTasks to completion BEFORE `.post()` returns (it is
#      not truly async from the caller's point of view), so a TestClient-level
#      test cannot observe whether a send happened INLINE inside
#      request_login or was merely SCHEDULED and then immediately run by the
#      test harness -- both look identical from outside. So these tests call
#      `auth_mod.request_login(email, base_url, tasks)` DIRECTLY with a real
#      `starlette.background.BackgroundTasks()` we control, and inspect
#      `tasks.tasks` (plus the mail-spy's call log) the instant request_login
#      returns, before anything has had a chance to execute the scheduled task.
#   3. The assertion is STRUCTURAL: (a) the mail-sending function was NOT
#      invoked synchronously during the request_login call, and (b) the
#      scheduled-task list contains exactly the expected callable + args.
#      That is a durable, non-flaky proxy for "no network I/O occurs before
#      request_login returns", which is the actual security property.
# ---------------------------------------------------------------------------

def test_allowlisted_send_is_scheduled_not_inline():
    """Allowlisted branch: send_magic_link must be SCHEDULED via
    tasks.add_task, never called inline. Currently RED because request_login
    takes only (email, base_url) -- passing a third `tasks` argument raises
    TypeError, which we convert to a normal (informative) test failure rather
    than letting it crash the whole suite."""
    _set_domain("example.edu")
    email = "scheduled-allowlisted@example.edu"
    _add_to_allowlist(email)
    tasks = BackgroundTasks()
    with _MailSpy("send_magic_link") as spy:
        try:
            result = auth_mod.request_login(email, "http://test/", tasks)
        except TypeError as e:
            raise AssertionError(
                "request_login must accept a `tasks` (BackgroundTasks-like) "
                f"third argument so the magic-link send can be SCHEDULED "
                f"instead of called inline: {e}") from e
        # The send must not have happened synchronously inside request_login.
        assert spy.calls == [], (
            f"send_magic_link must be scheduled via tasks.add_task, not "
            f"invoked inline inside request_login -- got {spy.calls}")
        assert len(tasks.tasks) == 1, (
            f"expected exactly one scheduled background task for the "
            f"allowlisted branch, got {len(tasks.tasks)}")
        scheduled = tasks.tasks[0]
        assert scheduled.func is auth_mod.send_magic_link, (
            f"the scheduled task must call app.auth.send_magic_link "
            f"(patched here via _MailSpy so we can identify it), got "
            f"{scheduled.func!r}")
        assert scheduled.args[0] == email, scheduled.args
        assert "/verify?token=" in scheduled.args[1], scheduled.args
    assert isinstance(result, dict) and "message" in result, result


def test_pending_request_send_is_scheduled_not_inline():
    """Fresh in-domain (not yet decided) branch: send_access_request must be
    SCHEDULED, never called inline."""
    _set_domain("example.edu")
    email = "scheduled-pending@example.edu"
    _clear_requests()
    tasks = BackgroundTasks()
    with _MailSpy("send_access_request") as spy:
        try:
            result = auth_mod.request_login(email, "http://test/", tasks)
        except TypeError as e:
            raise AssertionError(
                "request_login must accept a `tasks` third argument so the "
                f"admin-notification send can be scheduled, not called "
                f"inline: {e}") from e
        assert spy.calls == [], (
            f"send_access_request must be scheduled via tasks.add_task, not "
            f"invoked inline inside request_login -- got {spy.calls}")
        assert len(tasks.tasks) == 1, (
            f"expected exactly one scheduled background task for the "
            f"fresh-pending branch, got {len(tasks.tasks)}")
        scheduled = tasks.tasks[0]
        assert scheduled.func is auth_mod.send_access_request, (
            f"the scheduled task must call app.auth.send_access_request "
            f"(patched here via _MailSpy), got {scheduled.func!r}")
        assert scheduled.args[1] == email, scheduled.args
    assert isinstance(result, dict) and "message" in result, result


def test_denied_and_out_of_domain_schedule_no_background_task():
    """Denied and out-of-domain do NO network I/O today, and must continue
    to schedule NOTHING after the fire-and-forget change -- a regression
    that starts scheduling a harmless no-op task on every branch would still
    be functionally safe, but it would be worth catching since it muddies the
    "denied lands in the exact same fast/no-op bucket as out-of-domain"
    property this module's docstrings rely on."""
    _set_domain("example.edu")
    denied_email = "denied-scheduling-check@example.edu"
    stranger_email = "stranger-scheduling-check@notexample.com"
    _deny(denied_email)

    tasks_denied = BackgroundTasks()
    tasks_stranger = BackgroundTasks()
    try:
        auth_mod.request_login(denied_email, "http://test/", tasks_denied)
        auth_mod.request_login(stranger_email, "http://test/", tasks_stranger)
    except TypeError as e:
        raise AssertionError(
            f"request_login must accept a `tasks` third argument: {e}") from e

    assert len(tasks_denied.tasks) == 0, (
        f"a denied address must schedule NO background task, got "
        f"{len(tasks_denied.tasks)} scheduled")
    assert len(tasks_stranger.tasks) == 0, (
        f"an out-of-domain stranger must schedule NO background task, got "
        f"{len(tasks_stranger.tasks)} scheduled")


# ---------------------------------------------------------------------------
# FIX ROUND -- Defect 2 (HIGH, security review, CONFIRMED): plus-addressing
# bypasses a denial. Exact-string matching is fail-CLOSED for an allowlist but
# fail-OPEN for a denylist -- is_denied wrongly reused is_allowlisted's
# exact-match style. The fix stores a canonical form (lowercase + `+tag`
# local-part suffix stripped -- NOT dot-stripped, see the pinned test below)
# in a new indexed `canon_email` column (migration 9), and matches on it.
#
# This test is specifically about the OPPOSITE-polarity guarantee for the
# ALLOWLIST, which must NOT change: pins that allowlisting stays exact-match,
# so a regression here would silently let a stranger sign in as someone
# else's +tag variant of an allowlisted address.
# ---------------------------------------------------------------------------

def test_is_allowlisted_stays_exact_no_plus_tag_bypass():
    """Fail-closed guarantee, MUST NOT regress: unlike the denylist (which is
    intentionally becoming canonical to close a bypass), the ALLOWLIST must
    stay EXACT-match. If a +tag variant were ever treated as equivalent for
    the allowlist, allowlisting bob@example.edu would silently let
    bob+x@example.edu sign in too -- defeating the entire point of an
    allowlist. This is the opposite polarity from is_denied on purpose:
    exact match is fail-CLOSED for an allowlist, fail-OPEN for a denylist."""
    _set_domain("")  # EMAIL_DOMAIN="" -> may_request_access is True for anyone,
    # so a not-actually-allowlisted variant falls through to the "fresh
    # pending" branch instead of silently doing nothing -- letting us
    # positively confirm it was NOT treated as allowlisted, rather than just
    # observing an absence.
    email = "bob@example.edu"
    variant = "bob+x@example.edu"
    _clear_requests()
    _add_to_allowlist(email)

    with _MailSpy("send_magic_link") as magic_spy, \
            _MailSpy("send_access_request") as request_spy:
        with TestClient(app) as c:
            r = c.post("/api/auth/request", json={"email": variant})
            assert r.status_code == 200, r.text

    assert magic_spy.calls == [], (
        f"a +tag variant of an allowlisted address must NOT be treated as "
        f"allowlisted (exact match only) -- a magic link must not be sent, "
        f"got {magic_spy.calls}")
    assert len(request_spy.calls) == 1, (
        f"since the variant is genuinely not allowlisted and EMAIL_DOMAIN is "
        f"empty, it must fall through to the fresh-access-request branch "
        f"exactly like any other new address, got {request_spy.calls}")


# ---------------------------------------------------------------------------
# ROUND 3 (.plan-undeny.md) -- undo a denial (return an address to "never
# requested"). app.auth has no un-deny helper; the real HTTP endpoint (DELETE
# /api/admin/access-requests/{email}/denial) lives in backend/app/routers/admin.py and
# has its own dedicated suite in backend/tests/test_admin_router.py. This suite's job
# is is_denied()/request_login()'s behavior AFTER an undo -- i.e. proving the
# post-undo state is genuinely "never requested" from the gate's point of
# view -- so _undeny below bypasses admin auth/the HTTP layer with direct SQL
# that matches the endpoint's exact contract, mirroring the same convention
# already used by _deny/_deny_all_pending above (this suite has no admin
# login flow to drive the real endpoint through).
# ---------------------------------------------------------------------------

def _undeny(email):
    """Direct-SQL equivalent of DELETE /api/admin/access-requests/{email}/denial:
    delete every 'denied' row sharing `email`'s CANONICAL address. See the
    section comment above for why this bypasses the HTTP endpoint."""
    con = connect()
    target = auth_mod.canon_email(email)
    con.execute(
        "DELETE FROM access_requests WHERE status='denied' "
        "AND COALESCE(canon_email, LOWER(email))=?", (target,))
    con.commit()
    con.close()


def test_undenied_address_requests_exactly_like_a_first_timer():
    """THE stated round-3 requirement, as an EQUIVALENCE test rather than a
    spot-check: a deny->undo->re-request address is compared to a CONTROL
    address that never requested at all, and the two must be identical in
    shape (row count, status, response body, admin-notification count).
    Comparing to the control -- never to hardcoded expectations -- is what
    actually proves "indistinguishable from a first-timer" rather than
    "does something plausible"; same reasoning as
    test_response_message_identical_regardless_of_domain_match above.

    _set_domain("") is the DEFAULT config (EMAIL_DOMAIN unset) -- stated
    explicitly per .plan-undeny.md section 4, whose timing analysis is
    scoped to exactly this default (may_request_access is True for
    everyone, so there is no out-of-domain bucket to complicate the
    comparison).

    NOT COVERED HERE: timing. This test only pins ROW/RESPONSE shape, never
    elapsed time -- see test_denied_response_is_byte_identical_to_pending_
    and_out_of_domain above for why a wall-clock assertion doesn't belong in
    this suite, and .plan-undeny.md section 4 for the timing analysis this
    test deliberately does not attempt to strengthen."""
    _set_domain("")
    _clear_requests()
    control_addr = "control-firsttimer@example.edu"
    undone_addr = "undone-firsttimer@example.edu"
    _deny(undone_addr)
    _undeny(undone_addr)

    with _MailSpy("send_access_request") as control_spy:
        with TestClient(app) as c:
            control_resp = c.post("/api/auth/request", json={"email": control_addr})
    control_rows = _requests_for(control_addr)

    with _MailSpy("send_access_request") as undone_spy:
        with TestClient(app) as c:
            undone_resp = c.post("/api/auth/request", json={"email": undone_addr})
    undone_rows = _requests_for(undone_addr)

    assert control_resp.status_code == 200, control_resp.text
    assert undone_resp.status_code == 200, undone_resp.text
    assert control_resp.json() == undone_resp.json(), (
        f"response bodies must be byte-identical: control={control_resp.json()!r} "
        f"undone={undone_resp.json()!r}")
    assert len(control_rows) == 1 and len(undone_rows) == 1, (
        f"both must file exactly ONE row, got control={control_rows!r} "
        f"undone={undone_rows!r}")
    assert control_rows[0]["status"] == "pending", control_rows[0]
    assert undone_rows[0]["status"] == "pending", undone_rows[0]
    assert control_rows[0]["status"] == undone_rows[0]["status"], \
        (control_rows[0]["status"], undone_rows[0]["status"])
    assert len(control_spy.calls) == 1, control_spy.calls
    assert len(undone_spy.calls) == 1, undone_spy.calls


def test_undo_leaves_no_trace_rows():
    """The literal "never requested" state, checked BEFORE the re-request."""
    _set_domain("")
    _clear_requests()
    email = "notrace@example.edu"
    _deny(email)
    assert _requests_for(email) != [], "sanity: the deny must have written a row"
    _undeny(email)
    rows = _requests_for(email)
    assert rows == [], \
        f"expected ZERO rows after undo (the never-requested state), got {rows}"


def test_undone_denial_no_longer_blocks_any_variant():
    """Mirror of backend/tests/test_admin_router.py's canonical-group undo test, at
    the gate rather than the router: deny the bare address, undo it, then a
    +tag VARIANT that was never itself denied must file cleanly."""
    _set_domain("")
    _clear_requests()
    bare = "gatevariant@example.edu"
    variant = "gatevariant+2@example.edu"
    _deny(bare)
    _undeny(bare)

    with _MailSpy("send_access_request") as spy:
        with TestClient(app) as c:
            r = c.post("/api/auth/request", json={"email": variant})
            assert r.status_code == 200, r.text
    rows = _requests_for(variant)
    assert len(rows) == 1 and rows[0]["status"] == "pending", rows
    assert len(spy.calls) == 1, spy.calls


def test_undo_does_not_reset_the_rate_limit_window():
    """Undo is not a quota grant: the un-denied address must still be
    throttled by auth_request_attempts exactly like anyone else -- pins that
    undo leaves the rate-limit ledger untouched."""
    _set_domain("")
    _clear_requests()
    email = "ratelimited-undo@example.edu"
    con = connect()
    con.execute("DELETE FROM auth_request_attempts WHERE email=?", (email,))
    con.execute(
        "INSERT INTO auth_request_attempts(email, ip, created_at) VALUES (?,?,?)",
        (email, "127.0.0.1", time.time()))
    con.commit()
    con.close()
    _deny(email)

    _undeny(email)

    con = connect()
    n = con.execute(
        "SELECT COUNT(*) FROM auth_request_attempts WHERE email=?", (email,)).fetchone()[0]
    con.close()
    assert n == 1, \
        f"undo must not touch the rate-limit ledger, expected 1 row, got {n}"


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
    check("a denied address files no new row and sends no mail",
          test_denied_address_files_no_row_and_sends_no_mail)
    check("denied response is byte-identical to pending and out-of-domain (no oracle)",
          test_denied_response_is_byte_identical_to_pending_and_out_of_domain)
    check("denied-then-allowlisted gets a magic link (branch 1 before 2)",
          test_denied_then_allowlisted_gets_a_magic_link)
    check("denial blocks regardless of email-domain match (branch 2 before 3)",
          test_denial_blocks_regardless_of_email_domain_match)
    check("multiple pending rows are all denied together (per-address, not per-row)",
          test_multiple_pending_rows_all_denied_together)
    check("allowlisted branch schedules the magic-link send, never inline (defect 1)",
          test_allowlisted_send_is_scheduled_not_inline)
    check("fresh-pending branch schedules the admin-notification send, never inline (defect 1)",
          test_pending_request_send_is_scheduled_not_inline)
    check("denied/out-of-domain branches schedule no background task (defect 1)",
          test_denied_and_out_of_domain_schedule_no_background_task)
    check("is_allowlisted stays exact-match -- no +tag bypass (defect 2, opposite polarity)",
          test_is_allowlisted_stays_exact_no_plus_tag_bypass)
    check("an undenied address requests exactly like a first-timer (round 3, equivalence)",
          test_undenied_address_requests_exactly_like_a_first_timer)
    check("undo leaves no trace rows (never-requested state)",
          test_undo_leaves_no_trace_rows)
    check("an undone denial no longer blocks any variant",
          test_undone_denial_no_longer_blocks_any_variant)
    check("undo does not reset the rate-limit window",
          test_undo_does_not_reset_the_rate_limit_window)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL ACCESS-GATE TESTS PASSED")


if __name__ == "__main__":
    run()
