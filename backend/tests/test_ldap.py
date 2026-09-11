"""Directory sign-in, against ldap3's own in-process mock server.

There is no directory to develop against, so the double is `ldap3.MOCK_SYNC` --
a real ldap3 strategy, not a stub: it stores entries, checks `userPassword` on a
bind, and answers searches. A wrong password returns False because ldap3 decided
so, not because a mock was told to.

What MOCK_SYNC cannot express -- StartTLS, certificate validation, timeouts, an
unreachable server -- is covered by inspecting what `ldapauth._server` actually
builds. That is the only way to pin the `CERT_NONE` regression, and it is the
sharpest trap in this module: an `ldaps://` URI with ldap3's default `Tls()`
object encrypts the password and then accepts ANY certificate.

Honest limit, stated: MOCK_SYNC's filter parser is not full RFC 4515, so the
injection case asserts the CONSTRUCTED FILTER STRING as well as the search
finding nothing. The string assertion is the one that cannot lie.
"""
from __future__ import annotations

import logging
import os
import ssl
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

tmp = tempfile.mkdtemp()
os.environ["APP_DB_PATH"] = str(Path(tmp) / "app.db")
os.environ["ADMIN_EMAILS"] = "admin@example.edu"
os.environ["COOKIE_SECURE"] = "false"
os.environ["EMAIL_DOMAIN"] = ""
os.environ["CHAT_RATE_MAX_PER_USER"] = "0"
os.environ["AUTH_RATE_MAX_PER_EMAIL"] = "1000"
os.environ["AUTH_RATE_MAX_PER_IP"] = "1000"
os.environ["AUTH_METHOD"] = "ldap"
os.environ["LDAP_SERVER_URI"] = "ldaps://ldap.example.test:636"
os.environ["LDAP_BIND_DN"] = "cn=svc,dc=example,dc=edu"
os.environ["LDAP_BIND_PASSWORD"] = "service-password"
os.environ["LDAP_BASE_DN"] = "ou=people,dc=example,dc=edu"

import ldap3  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import mailer  # noqa: E402

mailer.send_magic_link = lambda to, link: True
mailer.send_access_request = lambda *a, **k: True
mailer.send_access_approved = lambda to: True

from app import ldapauth  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import admin as admin_router  # noqa: E402

get_settings.cache_clear()
init_db()

SVC_DN = "cn=svc,dc=example,dc=edu"
USER_DN = "uid=jdoe,ou=people,dc=example,dc=edu"
USER_PW = "correct horse battery staple"
GROUP_DN = "cn=ipeds,ou=groups,dc=example,dc=edu"

class Directory:
    """The entries every connection in one test sees, plus what was asked.

    Each instance builds its OWN ldap3.Server: MOCK_SYNC keeps its entries on
    the server object, not the connection, so a module-level one leaks entries
    between tests. That is not hypothetical -- the extra entry from the
    ambiguous-filter case made three later sign-ins fail with "2 entries match",
    and only because those tests asserted success did it surface at all."""

    def __init__(self, entries: dict) -> None:
        self.entries = entries
        self.server = ldap3.Server("fake", get_info=ldap3.OFFLINE_SLAPD_2_4)
        self.filters: list[str] = []
        self.binds: list[tuple] = []
        # The server object the APP built, kept so a test can assert on the
        # posture the sign-in path actually used. Without this a test can only
        # inspect what `_server()` returns when called directly, which a
        # regression in the sign-in path would never touch.
        self.app_servers: list = []
        self.start_tls_calls = 0
        self.raise_on_connect: Exception | None = None

    def connect(self, server, *, user=None, password=None, **kw):
        self.app_servers.append(server)
        if self.raise_on_connect is not None:
            raise self.raise_on_connect
        self.binds.append((user, password))
        conn = ldap3.Connection(self.server, user=user, password=password,
                                client_strategy=ldap3.MOCK_SYNC,
                                raise_exceptions=False, auto_bind=False)
        for dn, attrs in self.entries.items():
            conn.strategy.add_entry(dn, attrs)
        directory = self

        real_search = conn.search

        def recording_search(base, filt, *a, **kw):
            directory.filters.append(filt)
            return real_search(base, filt, *a, **kw)

        conn.search = recording_search

        def recording_start_tls(*a, **kw):
            directory.start_tls_calls += 1
            return True

        conn.start_tls = recording_start_tls
        return conn


def _directory(**overrides) -> Directory:
    user = {"userPassword": USER_PW, "objectClass": "inetOrgPerson",
            "uid": "jdoe", "sn": "Doe", "mail": "jdoe@example.edu",
            "memberOf": [GROUP_DN]}
    user.update(overrides)
    return Directory({
        SVC_DN: {"userPassword": "service-password", "sn": "svc",
                 "objectClass": "inetOrgPerson"},
        USER_DN: user,
    })


def _env(**kw) -> None:
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()


def _rows(sql: str, args: tuple = ()):
    con = connect()
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _wipe(email: str = "jdoe@example.edu") -> None:
    con = connect()
    try:
        con.execute("DELETE FROM allowlist WHERE email=?", (email,))
        con.execute("DELETE FROM access_requests WHERE LOWER(email)=?", (email.lower(),))
        con.execute("DELETE FROM auth_request_attempts")
        con.commit()
    finally:
        con.close()


def _login(directory: Directory, username="jdoe", password=USER_PW):
    """Drive the real route, with the directory injected at ldapauth's seam."""
    real = ldapauth._connection
    ldapauth._connection = directory.connect
    try:
        with TestClient(app) as c:
            return c.post("/api/auth/ldap",
                          json={"username": username, "password": password})
    finally:
        ldapauth._connection = real


def _signed_in(resp) -> bool:
    name = get_settings().cookie_name
    return any(h.startswith(f"{name}=") for h in resp.headers.get_list("set-cookie"))


# --- the two bind modes -----------------------------------------------------

def test_search_then_bind_signs_in():
    _wipe()
    r = _login(_directory())
    assert r.status_code == 200, r.text
    assert _signed_in(r), "no session cookie"
    assert r.json()["email"] == "jdoe@example.edu", r.json()


def _direct_mode(**extra):
    """Direct bind, with the search settings CLEARED.

    Leaving LDAP_BIND_DN/LDAP_BASE_DN set (they are set at import) leaves a
    perfectly good search-then-bind deployment configured, so a direct-mode test
    is satisfied by the search path and passes with the direct branch never
    entered -- mutation-verified: forcing ldap_bind_mode to return "search" kept
    every test green."""
    _env(LDAP_USER_DN_TEMPLATE="uid={username},ou=people,dc=example,dc=edu",
         LDAP_BIND_DN="", LDAP_BASE_DN="", LDAP_REQUIRED_GROUP_DN="", **extra)


def _search_mode():
    _env(LDAP_USER_DN_TEMPLATE="", LDAP_BIND_DN=SVC_DN,
         LDAP_BASE_DN="ou=people,dc=example,dc=edu")


def test_direct_bind_signs_in():
    _wipe()
    _direct_mode()
    try:
        directory = _directory()
        r = _login(directory)
        assert r.status_code == 200, r.text
        assert _signed_in(r)
        # It really took the direct branch: the ONLY bind is as the user's DN,
        # with no service-account bind before it.
        assert directory.binds == [(USER_DN, USER_PW)], directory.binds
    finally:
        _search_mode()


def test_a_wrong_password_is_refused():
    _wipe()
    r = _login(_directory(), password="not the password")
    assert r.status_code == 401, r.text
    assert not _signed_in(r)


# --- the RFC 4513 trap ------------------------------------------------------

def test_an_empty_password_is_refused_before_any_connection():
    """A simple bind with a zero-length password is an ANONYMOUS bind, which
    most servers answer with success — "any username, no password" as a
    sign-in. ldap3 refuses it itself, which in THIS codebase would surface as an
    uncaught 500 where every other rejection is a neutral 401 — an enumeration
    oracle of a different kind. So it is refused before a connection exists."""
    _wipe()
    for pw in ("", "   "):
        directory = _directory()
        r = _login(directory, password=pw)
        assert r.status_code == 401, f"{pw!r}: {r.text}"
        assert not _signed_in(r)
        assert directory.binds == [], (
            f"{pw!r} reached the directory: {directory.binds}")


# --- injection and ambiguity ------------------------------------------------

def test_a_username_with_filter_metacharacters_is_escaped():
    """Unescaped, `*` matches every entry and `)(uid=admin` rewrites the query.
    The FILTER STRING is asserted as well as the outcome: MOCK_SYNC's parser is
    not full RFC 4515, so only the string assertion cannot lie."""
    _wipe()
    for hostile in ("*", ")(uid=jdoe", "jdoe)(|(uid=*"):
        directory = _directory()
        r = _login(directory, username=hostile)
        assert r.status_code == 401, f"{hostile!r} was accepted"
        assert not _signed_in(r)
        sent = " ".join(directory.filters)
        assert "\\2a" in sent or hostile not in sent, (
            f"{hostile!r} reached the filter unescaped: {sent}")


def test_a_username_with_dn_metacharacters_cannot_redirect_the_bind():
    """A DN is not a filter, and they escape differently.

    `escape_filter_chars` leaves `,` and `=` alone, so using it on the direct-bind
    template lets the username `jdoe,ou=admins` turn
    `uid={username},ou=people,dc=x` into `uid=jdoe,ou=admins,ou=people,dc=x` --
    a bind aimed at a different subtree. RFC 4514's escaping (`escape_rdn`) is
    what this path needs."""
    _wipe()
    _direct_mode()
    try:
        directory = _directory()
        r = _login(directory, username="jdoe,ou=admins")
        assert r.status_code == 401, r.text
        assert not _signed_in(r)
        # Assert the DN that was ACTUALLY built, not the absence of a substring
        # from a list that could be empty — an empty list satisfies "for dn in
        # attempted" trivially, which is how this passed on the search path.
        assert directory.binds, "no bind was attempted at all"
        dn = directory.binds[0][0]
        assert dn == "uid=jdoe\\,ou\\=admins,ou=people,dc=example,dc=edu", dn
    finally:
        _search_mode()


def test_more_than_one_match_is_refused():
    """An ambiguous filter is a configuration error, and picking one entry is
    how somebody signs in as the wrong person."""
    _wipe()
    directory = _directory()
    # A SECOND entry carrying the same uid, so the ordinary filter matches both.
    # Deliberately not done by widening LDAP_USER_FILTER: a filter without its
    # {username} placeholder is now a config problem in its own right, so that
    # version tested the wrong refusal.
    directory.entries["uid=jdoe2,ou=people,dc=example,dc=edu"] = {
        "userPassword": USER_PW, "objectClass": "inetOrgPerson",
        "uid": "jdoe", "sn": "Doe2", "mail": "other@example.edu"}
    r = _login(directory)
    assert r.status_code == 401, r.text
    assert not _signed_in(r)


# --- the fence --------------------------------------------------------------

def test_the_group_fence_refuses_a_non_member():
    _wipe()
    _env(LDAP_REQUIRED_GROUP_DN="cn=other,ou=groups,dc=example,dc=edu")
    try:
        r = _login(_directory())
        assert r.status_code == 401, r.text
        assert not _signed_in(r)
    finally:
        _env(LDAP_REQUIRED_GROUP_DN="")


def test_the_group_fence_admits_a_member():
    """Both halves: with only the refusal case, a fence that refused EVERYONE
    would stay green."""
    _wipe()
    _env(LDAP_REQUIRED_GROUP_DN=GROUP_DN)
    try:
        r = _login(_directory())
        assert r.status_code == 200, r.text
        assert _signed_in(r), "a group member was refused"
    finally:
        _env(LDAP_REQUIRED_GROUP_DN="")


def test_a_multi_valued_mail_attribute_is_refused():
    """LDAP attribute sets have no defined order, so picking `raw[0]` means the
    same entry can land in either of two app accounts run to run — and if one of
    those addresses is an ADMIN_EMAILS one, that is an admin account. Refused,
    the same call the one-result rule makes about an ambiguous search."""
    _wipe()
    directory = _directory()
    directory.entries[USER_DN]["mail"] = ["jdoe@example.edu", "admin@example.edu"]
    r = _login(directory)
    assert r.status_code == 401, r.text
    assert not _signed_in(r)


def test_the_domain_fence_refuses_an_outside_address():
    """A directory holds contacts and shared mailboxes, not only staff."""
    _wipe()
    _env(LDAP_ALLOWED_DOMAINS="example.edu")
    try:
        directory = _directory()
        directory.entries[USER_DN]["mail"] = "someone@elsewhere.test"
        r = _login(directory)
        assert r.status_code == 401, r.text
        assert not _signed_in(r)
    finally:
        _env(LDAP_ALLOWED_DOMAINS="")


def test_the_domain_fence_admits_an_inside_address():
    _wipe()
    _env(LDAP_ALLOWED_DOMAINS="example.edu")
    try:
        r = _login(_directory())
        assert r.status_code == 200, r.text
        assert _signed_in(r), "an address inside the fence was refused"
    finally:
        _env(LDAP_ALLOWED_DOMAINS="")


def test_the_group_attribute_is_matched_case_insensitively():
    """`entry_attributes_as_dict` is keyed by the case the SERVER returned, so
    LDAP_GROUP_MEMBER_ATTRIBUTE=memberof — the spelling most AD documentation
    uses — against a server answering memberOf would refuse everybody, and look
    exactly like a real refusal."""
    _wipe()
    _env(LDAP_REQUIRED_GROUP_DN=GROUP_DN, LDAP_GROUP_MEMBER_ATTRIBUTE="memberof")
    try:
        r = _login(_directory())
        assert r.status_code == 200, r.text
    finally:
        _env(LDAP_REQUIRED_GROUP_DN="", LDAP_GROUP_MEMBER_ATTRIBUTE="memberOf")


def test_an_entry_with_no_mail_attribute_provisions_nobody():
    _wipe()
    before = len(_rows("SELECT email FROM allowlist"))
    directory = _directory()
    directory.entries[USER_DN].pop("mail")
    r = _login(directory)
    assert r.status_code == 401, r.text
    assert len(_rows("SELECT email FROM allowlist")) == before


# --- provisioning and revocation -------------------------------------------

def test_first_sign_in_provisions_a_user_and_an_allowlist_row():
    _wipe()
    r = _login(_directory())
    assert r.status_code == 200, r.text
    rows = _rows("SELECT added_by FROM allowlist WHERE email=?", ("jdoe@example.edu",))
    assert rows and rows[0]["added_by"] == "ldap", rows


def test_a_blocked_address_is_refused():
    _wipe()
    con = connect()
    try:
        admin_router._block_canonical(con, "jdoe@example.edu")
        con.commit()
    finally:
        con.close()
    r = _login(_directory())
    assert r.status_code == 401, r.text
    assert not _signed_in(r)
    assert not _rows("SELECT email FROM allowlist WHERE email=?", ("jdoe@example.edu",)), (
        "a blocked address was provisioned on its way to being refused")


def test_a_re_added_user_can_sign_in_again():
    """The allowlist wins over a denial — `auth.is_denied`'s documented
    invariant, and the same ordering the OIDC callback uses."""
    _wipe()
    con = connect()
    try:
        admin_router._block_canonical(con, "jdoe@example.edu")
        con.execute("INSERT INTO allowlist(email, note, added_by, added_at) "
                    "VALUES (?,?,?,?) ON CONFLICT(email) DO NOTHING",
                    ("jdoe@example.edu", "re-added in bulk", "admin@example.edu", 1.0))
        con.commit()
    finally:
        con.close()
    r = _login(_directory())
    assert r.status_code == 200, r.text


# --- the transport posture (what MOCK_SYNC cannot express) ------------------

def test_referrals_are_never_followed():
    """⚠ ldap3 follows referrals by DEFAULT, and trusts any host with this
    connection's credentials (`allowed_referral_hosts=[('*', True)]`).

    `create_referral_connection` copies the user and password into a connection
    to whatever host a referral names — over plaintext unless the referral URL
    says ldaps — and the referred server's answer REPLACES the search result. So
    one referral object inside LDAP_BASE_DN is both an exfiltration channel for
    LDAP_BIND_PASSWORD and a way to return `mail: admin@example.edu` while the
    password check still runs against the real directory as the attacker's own
    account.

    Asserted structurally because a referral needs a second, referring server
    that MOCK_SYNC cannot be."""
    server = ldapauth._server(get_settings())
    conn = ldapauth._connection(server, user="cn=probe", password="x")
    assert conn.auto_referrals is False, "ldap3 will chase referrals"
    assert server.allowed_referral_hosts == [], (
        f"referral hosts are trusted with our credentials: "
        f"{server.allowed_referral_hosts}")
    assert conn.receive_timeout, "no recv deadline: a slow server holds a worker"


def test_the_timeouts_handed_to_ldap3_are_integers():
    """ldap3 raises `error: required argument is not an integer` on a FLOAT
    timeout, the moment it opens a real socket.

    `ldap_timeout_seconds` is a float (as its OIDC sibling is), so the unfixed
    version turned every sign-in against every real directory into "the
    directory could not be reached" -- a neutral 401 and a log line blaming the
    network. MOCK_SYNC never opens a socket, so no amount of testing here could
    have found it; it took running against a real server. Asserted on the values
    ldap3 is actually handed."""
    server = ldapauth._server(get_settings())
    conn = ldapauth._connection(server, user="cn=probe", password="x")
    assert isinstance(server.connect_timeout, int), (
        f"connect_timeout is {type(server.connect_timeout).__name__}, "
        f"ldap3 needs int")
    assert isinstance(conn.receive_timeout, int), (
        f"receive_timeout is {type(conn.receive_timeout).__name__}, "
        f"ldap3 needs int")
    assert server.connect_timeout >= 1 and conn.receive_timeout >= 1


def test_certificates_are_always_verified_on_the_sign_in_path():
    """⚠ ldap3's own `Tls()` default is `validate=CERT_NONE`, so an ldaps:// URI
    built with it accepts ANY certificate — on the one connection in this app
    that carries a plaintext password.

    Asserted on the server object the SIGN-IN actually built, not on what
    `_server()` returns when called directly. The direct version passes with the
    sign-in path swapped to a default-Tls server, because nothing in it ever
    observes the object the real path uses — mutation-verified."""
    _wipe()
    directory = _directory()
    r = _login(directory)
    assert r.status_code == 200, r.text
    assert directory.app_servers, "the sign-in path built no server"
    for server in directory.app_servers:
        assert server.tls is not None, "no TLS context at all"
        assert server.tls.validate == ssl.CERT_REQUIRED, (
            f"certificate validation is {server.tls.validate}, not CERT_REQUIRED")


def test_starttls_runs_on_a_plain_ldap_deployment():
    """`ldap://` + StartTLS is a configuration `ldap_config_problems` blesses, so
    it needs a test: without one, deleting `_start_tls_if_needed`'s body sends
    every password in the clear with the suite green."""
    _wipe()
    _env(LDAP_SERVER_URI="ldap://ldap.example.test:389", LDAP_START_TLS="true")
    try:
        directory = _directory()
        r = _login(directory)
        assert r.status_code == 200, r.text
        assert directory.start_tls_calls >= 1, "StartTLS never ran on an ldap:// URI"
    finally:
        _env(LDAP_SERVER_URI="ldaps://ldap.example.test:636", LDAP_START_TLS="false")


def test_an_unreachable_directory_is_the_same_neutral_401():
    """The likeliest first-day failure of this feature is a certificate that
    does not verify, which ldap3 raises rather than returning False.

    This pins the PROPERTY, not one implementation of it: two layers hold it
    (`authenticate` converts LDAPException to LdapError, and the route has a
    catch-all), and removing either alone keeps the test green. That is genuine
    defence in depth rather than a gap — measured: the case fails only with both
    removed."""
    _wipe()
    directory = _directory()
    directory.raise_on_connect = ldap3.core.exceptions.LDAPSocketOpenError(
        "certificate verify failed")
    r = _login(directory)
    assert r.status_code == 401, r.text
    assert not _signed_in(r)
    assert "Check your username and password" in r.text, r.text


def test_plain_ldap_without_tls_is_not_a_usable_configuration():
    """Refused at RESOLUTION, so the operator finds out at boot rather than from
    a user who cannot sign in — and the route closes with the method."""
    _env(LDAP_SERVER_URI="ldap://ldap.example.test:389")
    try:
        problems = " ".join(
            __import__("app.authmethod", fromlist=["x"]).ldap_config_problems(get_settings()))
        assert "LDAP_START_TLS" in problems, problems
        with TestClient(app) as c:
            assert c.post("/api/auth/ldap",
                          json={"username": "jdoe", "password": USER_PW}
                          ).status_code == 404
    finally:
        _env(LDAP_SERVER_URI="ldaps://ldap.example.test:636")


# --- no oracle, no leak -----------------------------------------------------

def test_every_rejection_is_the_same_answer():
    """Anything that varies by cause tells an attacker which usernames exist."""
    _wipe()
    answers = set()
    cases = [
        ("jdoe", "wrong-password"),
        ("nobody-here", USER_PW),
    ]
    for username, password in cases:
        r = _login(_directory(), username=username, password=password)
        answers.add((r.status_code, r.text))
    _env(LDAP_REQUIRED_GROUP_DN="cn=other,ou=groups,dc=example,dc=edu")
    try:
        r = _login(_directory())
        answers.add((r.status_code, r.text))
    finally:
        _env(LDAP_REQUIRED_GROUP_DN="")
    assert len(answers) == 1, f"the refusals are distinguishable: {answers}"


def test_the_password_never_reaches_a_log_record():
    _wipe()
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    root = logging.getLogger()
    handler = _Capture(level=logging.DEBUG)
    root.addHandler(handler)
    previous = root.level
    root.setLevel(logging.DEBUG)
    try:
        _login(_directory(), password="wrong-but-distinctive-zxcv")
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)
    leaked = [r.getMessage() for r in records if "zxcv" in r.getMessage()]
    assert not leaked, f"the password reached the log: {leaked}"


def test_the_rate_limiter_refuses_after_the_cap():
    """The one method where online password guessing is possible."""
    _wipe()
    _env(AUTH_RATE_MAX_PER_EMAIL="3")
    try:
        codes = [_login(_directory(), password="wrong").status_code for _ in range(6)]
    finally:
        _env(AUTH_RATE_MAX_PER_EMAIL="1000")
    assert 429 in codes, f"the limiter never fired: {codes}"


def test_a_successful_sign_in_does_not_consume_the_budget():
    """A password form is mistyped far more often than an email address is, so
    charging successes too would walk somebody who signs in daily into a lockout
    for no reason. Wrong answers still count — that is what bounds guessing."""
    _wipe()
    _env(AUTH_RATE_MAX_PER_EMAIL="3")
    try:
        codes = [_login(_directory()).status_code for _ in range(6)]
    finally:
        _env(AUTH_RATE_MAX_PER_EMAIL="1000")
    assert codes == [200] * 6, f"a successful sign-in was rate-limited: {codes}"


# --- the method guard -------------------------------------------------------

def test_the_ldap_route_404s_when_the_method_is_not_ldap():
    _env(AUTH_METHOD="magic_link")
    try:
        with TestClient(app) as c:
            assert c.post("/api/auth/ldap",
                          json={"username": "jdoe", "password": USER_PW}
                          ).status_code == 404
    finally:
        _env(AUTH_METHOD="ldap")


def test_the_magic_link_routes_404_under_ldap():
    with TestClient(app) as c:
        assert c.post("/api/auth/request",
                      json={"email": "jdoe@example.edu"}).status_code == 404
        assert c.post("/api/auth/verify", json={"token": "x" * 20}).status_code == 404


FAILURES: list[str] = []


def check(name: str, fn) -> None:
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        print(f"  ✗ {name}: {e}")
        FAILURES.append(name)
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ {name}: unexpected {type(e).__name__}: {e}")
        FAILURES.append(name)


def run():
    print("\n1. binding")
    check("search-then-bind signs in", test_search_then_bind_signs_in)
    check("direct bind signs in", test_direct_bind_signs_in)
    check("a wrong password is refused", test_a_wrong_password_is_refused)
    check("an empty password is refused before any connection",
          test_an_empty_password_is_refused_before_any_connection)

    print("\n2. injection and ambiguity")
    check("a username with filter metacharacters is escaped",
          test_a_username_with_filter_metacharacters_is_escaped)
    check("a username with DN metacharacters cannot redirect the bind",
          test_a_username_with_dn_metacharacters_cannot_redirect_the_bind)
    check("more than one match is refused", test_more_than_one_match_is_refused)

    print("\n3. the fence")
    check("the group fence refuses a non-member", test_the_group_fence_refuses_a_non_member)
    check("the group fence admits a member", test_the_group_fence_admits_a_member)
    check("a multi-valued mail attribute is refused",
          test_a_multi_valued_mail_attribute_is_refused)
    check("the domain fence refuses an outside address",
          test_the_domain_fence_refuses_an_outside_address)
    check("the domain fence admits an inside address",
          test_the_domain_fence_admits_an_inside_address)
    check("the group attribute is matched case-insensitively",
          test_the_group_attribute_is_matched_case_insensitively)
    check("an entry with no mail attribute provisions nobody",
          test_an_entry_with_no_mail_attribute_provisions_nobody)

    print("\n4. provisioning and revocation")
    check("first sign-in provisions a user and an allowlist row",
          test_first_sign_in_provisions_a_user_and_an_allowlist_row)
    check("a blocked address is refused", test_a_blocked_address_is_refused)
    check("a re-added user can sign in again", test_a_re_added_user_can_sign_in_again)

    print("\n5. transport posture")
    check("referrals are never followed", test_referrals_are_never_followed)
    check("the timeouts handed to ldap3 are integers",
          test_the_timeouts_handed_to_ldap3_are_integers)
    check("certificates are always verified on the sign-in path",
          test_certificates_are_always_verified_on_the_sign_in_path)
    check("StartTLS runs on a plain ldap deployment",
          test_starttls_runs_on_a_plain_ldap_deployment)
    check("an unreachable directory is the same neutral 401",
          test_an_unreachable_directory_is_the_same_neutral_401)
    check("plain ldap without TLS is not a usable configuration",
          test_plain_ldap_without_tls_is_not_a_usable_configuration)

    print("\n6. no oracle, no leak")
    check("every rejection is the same answer", test_every_rejection_is_the_same_answer)
    check("the password never reaches a log record",
          test_the_password_never_reaches_a_log_record)
    check("the rate limiter refuses after the cap",
          test_the_rate_limiter_refuses_after_the_cap)
    check("a successful sign-in does not consume the budget",
          test_a_successful_sign_in_does_not_consume_the_budget)

    print("\n7. the method guard")
    check("the ldap route 404s when the method is not ldap",
          test_the_ldap_route_404s_when_the_method_is_not_ldap)
    check("the magic-link routes 404 under ldap", test_the_magic_link_routes_404_under_ldap)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL LDAP TESTS PASSED")


if __name__ == "__main__":
    run()
