"""OIDC sign-in, end to end, against a provider that never touches a socket.

`fakeidp.py` serves discovery, JWKS and the token endpoint over an
httpx.MockTransport and signs REAL RS256 id_tokens, so app/oidc.py runs its real
verification path here. Every case below names a regression that path can
actually have; none of them assert a constant back at a mock.

The four that matter most, because each is a way to be signed in as somebody
else: a token signed with the wrong key, an `alg: none` token, a replayed
`state`, and a callback whose `state` was never issued (which must not even
reach the token endpoint).
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fakeidp  # noqa: E402

tmp = tempfile.mkdtemp()
os.environ["APP_DB_PATH"] = str(Path(tmp) / "app.db")
os.environ["ADMIN_EMAILS"] = "admin@example.edu"
os.environ["COOKIE_SECURE"] = "false"
os.environ["EMAIL_DOMAIN"] = ""
os.environ["CHAT_RATE_MAX_PER_USER"] = "0"
# This suite starts dozens of logins from one address, and /oidc/start is per-IP
# rate limited. Pinned high here so the limiter stays out of the way; the case
# that owns the 429 contract tightens it for itself.
os.environ["AUTH_RATE_MAX_PER_EMAIL"] = "1000"
os.environ["AUTH_RATE_MAX_PER_IP"] = "1000"
# The public origin the redirect_uri is built from. Deliberately NOT the
# TestClient's own "testserver" host, so a redirect_uri accidentally derived
# from the request would be visibly different.
os.environ["APP_PUBLIC_URL"] = "https://app.example.test"
os.environ["AUTH_METHOD"] = "oidc"
os.environ["OIDC_ISSUER"] = fakeidp.ISSUER
os.environ["OIDC_CLIENT_ID"] = fakeidp.CLIENT_ID
os.environ["OIDC_CLIENT_SECRET"] = fakeidp.CLIENT_SECRET

from fastapi.testclient import TestClient  # noqa: E402

from app import mailer  # noqa: E402

mailer.send_magic_link = lambda to, link: True
mailer.send_access_request = lambda *a, **k: True
mailer.send_access_approved = lambda to: True

from app import oidc  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import admin as admin_router  # noqa: E402
from app.security import hash_token  # noqa: E402

# The schema is created by the app's lifespan, i.e. only inside a TestClient
# context. The fixture helpers below touch app.db directly and run before that,
# so build it once here -- the same thing test_access_gate.py does.
get_settings.cache_clear()
init_db()

CALLBACK = "/api/auth/oidc/callback"


def _env(**kw) -> None:
    """Change settings mid-suite. get_settings is lru_cached, so the clear is
    the load-bearing half."""
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()


def _install(idp: fakeidp.FakeIdp) -> None:
    oidc._TRANSPORT = idp.transport()
    oidc._clear_caches()


def _start(c: TestClient, idp: fakeidp.FakeIdp, **kw) -> dict:
    """Run the first leg and teach the fake what a real provider would remember
    from the authorize request (the nonce and the PKCE challenge)."""
    r = c.post("/api/auth/oidc/start", **kw)
    assert r.status_code == 200, r.text
    q = {k: v[0] for k, v in parse_qs(urlsplit(r.json()["authorization_url"]).query).items()}
    idp.nonce = q.get("nonce")
    idp.expected_challenge = q.get("code_challenge")
    return q


def _callback(c: TestClient, state: str, code: str = "AUTH-CODE"):
    return c.get(CALLBACK, params={"code": code, "state": state}, follow_redirects=False)


def _signed_in(resp) -> bool:
    """Did this response actually mint a SESSION?

    Not `"set-cookie" in headers` -- a failure clears the state-binding cookie,
    which is itself a Set-Cookie header, so the loose form reads every refusal
    as a sign-in. Match the session cookie by name; the state cookie is
    `<name>_oidc`, so the trailing `=` keeps them apart."""
    name = get_settings().cookie_name
    return any(h.startswith(f"{name}=") for h in resp.headers.get_list("set-cookie"))


def _error_of(resp) -> str | None:
    loc = resp.headers.get("location", "")
    return parse_qs(urlsplit(loc).query).get("auth_error", [None])[0]


def _rows(sql: str, args: tuple = ()):
    con = connect()
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _fresh(email: str = fakeidp.DEFAULT_EMAIL) -> fakeidp.FakeIdp:
    """A provider ready to authenticate `email`, with that address cleared out of
    app.db so provisioning is observable."""
    con = connect()
    try:
        con.execute("DELETE FROM allowlist WHERE email=?", (email,))
        con.execute("DELETE FROM access_requests WHERE LOWER(email)=?", (email.lower(),))
        con.execute("DELETE FROM sessions", ())
        con.commit()
    finally:
        con.close()
    idp = fakeidp.FakeIdp()
    idp.claims = {"email": email}
    _install(idp)
    return idp


# --- leg 1: the authorization URL ------------------------------------------

def test_start_builds_an_authorization_url_on_the_issuer():
    idp = _fresh()
    with TestClient(app) as c:
        q = _start(c, idp)
    assert q["response_type"] == "code", q
    assert "openid" in q["scope"].split(), q
    assert q["code_challenge_method"] == "S256", q
    assert q["client_id"] == fakeidp.CLIENT_ID, q
    assert q.get("state") and q.get("nonce"), q
    assert q["redirect_uri"] == f"https://app.example.test{oidc.CALLBACK_PATH}", q


def test_redirect_uri_ignores_the_host_header():
    """The magic-link link-poisoning trap, in its OIDC form. A redirect_uri taken
    from the request follows the attacker-controllable Host header, and against a
    provider configured with a wildcard redirect that is account takeover."""
    idp = _fresh()
    with TestClient(app) as c:
        q = _start(c, idp, headers={"Host": "evil.example"})
    assert q["redirect_uri"] == f"https://app.example.test{oidc.CALLBACK_PATH}", q


def test_start_stores_only_the_state_hash():
    idp = _fresh()
    with TestClient(app) as c:
        q = _start(c, idp)
    stored = [r["state_hash"] for r in _rows("SELECT state_hash FROM oidc_logins")]
    assert q["state"] not in stored, "the raw state must never be written to app.db"
    assert hash_token(q["state"]) in stored, "the state's hash should be there"


# --- leg 2: the callback ----------------------------------------------------

def test_callback_signs_in_and_sets_a_session_cookie():
    idp = _fresh()
    with TestClient(app) as c:
        q = _start(c, idp)
        r = _callback(c, q["state"])
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/", r.headers
    assert _signed_in(r), "no session cookie on a successful sign-in"


def test_the_token_exchange_carries_the_pkce_verifier():
    """fakeidp asserts S256(verifier) == the challenge it saw in the URL, so a
    build that stopped sending the verifier fails inside the exchange."""
    idp = _fresh()
    with TestClient(app) as c:
        q = _start(c, idp)
        r = _callback(c, q["state"])
    # Assert the SUCCESS redirect, not merely a 303 -- a failure redirect is a
    # 303 too, so `status_code == 303` alone passes with the exchange broken.
    assert r.headers.get("location") == "/", r.headers
    assert idp.token_requests, "the token endpoint was never called"
    body = idp.token_requests[0]
    assert body["grant_type"] == "authorization_code", body
    assert body["code"] == "AUTH-CODE", body
    assert body.get("code_verifier"), body


def test_replaying_the_same_state_is_refused():
    idp = _fresh()
    with TestClient(app) as c:
        q = _start(c, idp)
        first = _callback(c, q["state"])
        assert first.status_code == 303 and _signed_in(first)
        again = _callback(c, q["state"])
    assert _error_of(again) == "invalid_state", again.headers
    assert not _signed_in(again), "a replayed callback minted a session"


def test_an_unknown_state_never_reaches_the_token_endpoint():
    """The state row IS this leg's CSRF defence: the callback is a GET, so the
    Origin check cannot apply. An unknown state must be refused before anything
    is sent to the provider."""
    idp = _fresh()
    with TestClient(app) as c:
        _start(c, idp)
        r = _callback(c, "state-nobody-issued")
    assert _error_of(r) == "invalid_state", r.headers
    assert idp.token_requests == [], "an unissued state reached the token exchange"


def test_an_expired_state_is_refused():
    idp = _fresh()
    with TestClient(app) as c:
        q = _start(c, idp)
        con = connect()
        try:
            con.execute("UPDATE oidc_logins SET expires_at=?", (time.time() - 1,))
            con.commit()
        finally:
            con.close()
        r = _callback(c, q["state"])
    assert _error_of(r) == "invalid_state", r.headers


def test_a_callback_without_the_binding_cookie_is_refused():
    """LOGIN CSRF / SESSION FIXATION.

    The state row proves a login was started HERE; it does not prove it was
    started by THIS browser. Without the cookie binding, an attacker starts a
    login himself, authenticates as himself, and feeds the victim's browser the
    resulting callback URL — a GET, so the CSRF layer skips it. The victim is
    then signed in AS THE ATTACKER, and everything they type afterwards lands in
    the attacker's account.
    """
    idp = _fresh()
    with TestClient(app) as attacker:
        q = _start(attacker, idp)
        # A different browser: no cookie jar in common with the one that started.
        with TestClient(app) as victim:
            r = _callback(victim, q["state"])
    assert not _signed_in(r), "a callback replayed into another browser signed it in"
    assert _error_of(r) == "invalid_state", r.headers


def test_a_callback_with_the_wrong_binding_cookie_is_refused():
    idp = _fresh()
    with TestClient(app) as c:
        q = _start(c, idp)
        c.cookies.set(oidc.state_cookie_name(), "not-the-state-we-issued")
        r = _callback(c, q["state"])
    assert not _signed_in(r)
    assert _error_of(r) == "invalid_state", r.headers


def test_the_binding_cookie_is_httponly_and_lax():
    """`lax` is required, not chosen: the provider redirects back with a
    cross-site top-level GET, and `strict` would drop the cookie — which is the
    exact state the callback refuses."""
    idp = _fresh()
    with TestClient(app) as c:
        r = c.post("/api/auth/oidc/start")
    raw = next(h for h in r.headers.get_list("set-cookie")
               if h.startswith(oidc.state_cookie_name() + "="))
    low = raw.lower()
    assert "httponly" in low, raw
    assert "samesite=lax" in low, raw
    assert idp.discovery_requests >= 1


# --- the id_token itself ----------------------------------------------------

def _refused_token(**idp_attrs) -> str | None:
    idp = _fresh()
    for k, v in idp_attrs.items():
        setattr(idp, k, v)
    with TestClient(app) as c:
        q = _start(c, idp)
        r = _callback(c, q["state"])
    assert not _signed_in(r), f"signed in anyway with {idp_attrs}"
    return _error_of(r)


def test_a_token_signed_with_the_wrong_key_is_refused():
    """Same kid, different key — so this fails only if the SIGNATURE is checked."""
    assert _refused_token(sign_with_forgery=True) == "provider_error"


def test_an_unsigned_token_is_refused():
    """Honest about who refuses this: joserfc will not even SIGN `alg: none`, so
    this pins the stack's behaviour rather than our allowlist. The case below is
    the one that pins the allowlist."""
    assert _refused_token(alg="none") == "provider_error"


def test_a_symmetric_algorithm_is_refused_even_when_the_provider_publishes_the_key():
    """RS->HS confusion, made genuinely reachable.

    Against an RSA-only JWKS joserfc refuses an HS256 token because no key
    matches the kid -- the right outcome for the wrong reason, and one that
    disappears the moment a provider also publishes a symmetric key, as some do.
    Here the fake publishes one, so the key lookup SUCCEEDS and the only thing
    left standing is oidc._ALLOWED_ALGS refusing on algorithm policy.

    Mutation-verified: with `algorithms=None` passed to the verifier, this token
    is accepted and the caller is signed in as whoever it names."""
    assert _refused_token(alg="HS256", kid=fakeidp.HS_KID,
                          publish_symmetric_key=True) == "provider_error"


def test_a_mismatched_nonce_is_refused():
    """Replaying another login's id_token into this one's callback."""
    idp = _fresh()
    with TestClient(app) as c:
        q = _start(c, idp)
        idp.claims = {**idp.claims, "nonce": "some-other-logins-nonce"}
        r = _callback(c, q["state"])
    assert not _signed_in(r)
    assert _error_of(r) == "provider_error", r.headers


def test_a_wrong_audience_is_refused():
    assert _refused_token(claims={"aud": "some-other-client"}) == "provider_error"


def test_a_wrong_issuer_is_refused():
    assert _refused_token(claims={"iss": "https://idp.evil.test"}) == "provider_error"


def test_an_expired_token_is_refused():
    old = int(time.time()) - 3600
    assert _refused_token(claims={"iat": old, "exp": old + 60}) == "provider_error"


def test_a_token_response_with_no_id_token_is_refused():
    """A plain OAuth2 answer says something was authorised, never WHO."""
    assert _refused_token(omit_id_token=True) == "provider_error"


# --- the fences -------------------------------------------------------------

def test_an_unverified_email_is_refused():
    assert _refused_token(claims={"email_verified": False}) == "not_authorized"


def test_a_missing_email_claim_provisions_nobody():
    # The baseline is taken INSIDE, after _fresh() has wiped its rows -- reading
    # it first makes the test depend on where it sits in run().
    idp = _fresh()
    idp.drop_claims = ("email",)
    before = len(_rows("SELECT email FROM allowlist"))
    with TestClient(app) as c:
        q = _start(c, idp)
        r = _callback(c, q["state"])
    assert not _signed_in(r)
    assert _error_of(r) == "not_authorized", r.headers
    assert len(_rows("SELECT email FROM allowlist")) == before, (
        "a token with no usable email claim created an account anyway")


def test_a_domain_outside_the_fence_is_refused():
    _env(OIDC_ALLOWED_DOMAINS="example.edu")
    try:
        assert _refused_token(claims={"email": "outsider@elsewhere.test"}) == "not_authorized"
    finally:
        _env(OIDC_ALLOWED_DOMAINS="")


def test_the_domain_fence_admits_a_member():
    """Both halves, deliberately. With only the refusal case, changing the fence
    to `if domains:` -- refusing EVERY login whenever a domain fence is set --
    stays green, and that is the configuration the README calls strongly
    recommended."""
    _env(OIDC_ALLOWED_DOMAINS="example.edu")
    try:
        idp = _fresh("insider@example.edu")
        with TestClient(app) as c:
            q = _start(c, idp)
            r = _callback(c, q["state"])
        assert _signed_in(r), "an address inside the fence was refused"
    finally:
        _env(OIDC_ALLOWED_DOMAINS="")


def test_a_user_outside_the_required_group_is_refused():
    _env(OIDC_REQUIRED_GROUP="ipeds-users")
    try:
        assert _refused_token(claims={"groups": ["other-group"]}) == "not_authorized"
    finally:
        _env(OIDC_REQUIRED_GROUP="")


def test_the_required_group_admits_a_member():
    _env(OIDC_REQUIRED_GROUP="ipeds-users")
    try:
        idp = _fresh()
        idp.claims = {**idp.claims, "groups": ["ipeds-users", "other"]}
        with TestClient(app) as c:
            q = _start(c, idp)
            r = _callback(c, q["state"])
        assert _signed_in(r), "a group member was refused"
    finally:
        _env(OIDC_REQUIRED_GROUP="")


def test_a_token_naming_another_client_in_azp_is_refused():
    """`aud` may legitimately be a list. `azp` is then what says the token was
    minted for US rather than merely mentioning us."""
    assert _refused_token(
        claims={"aud": [fakeidp.CLIENT_ID, "some-other-client"],
                "azp": "some-other-client"}) == "provider_error"


def test_openid_is_always_requested_even_if_the_operator_omits_it():
    """Without `openid` the provider runs a plain OAuth2 flow and returns no
    id_token at all -- which fails later, and far less legibly, than here."""
    _env(OIDC_SCOPES="email profile")
    try:
        idp = _fresh()
        with TestClient(app) as c:
            q = _start(c, idp)
        assert "openid" in q["scope"].split(), q["scope"]
    finally:
        _env(OIDC_SCOPES="openid email profile")


# --- provisioning and revocation -------------------------------------------

def test_first_sign_in_provisions_a_user_and_an_allowlist_row():
    """Writing the ALLOWLIST row is what keeps _user_from_request and
    apikeys.verify working unchanged — see auth.provision_from_idp."""
    idp = _fresh("newcomer@example.edu")
    with TestClient(app) as c:
        q = _start(c, idp)
        r = _callback(c, q["state"])
    assert r.headers.get("location") == "/", r.headers
    rows = _rows("SELECT email, added_by FROM allowlist WHERE email=?",
                 ("newcomer@example.edu",))
    assert rows, "no allowlist row: every later request would 401"
    assert rows[0]["added_by"].startswith("oidc:"), rows[0]["added_by"]
    assert _rows("SELECT id FROM users WHERE email=?", ("newcomer@example.edu",))


def test_provisioning_does_not_relabel_an_existing_allowlist_row():
    """An ADMIN_EMAILS bootstrap row, or one an admin curated, records where the
    access came from. A first SSO sign-in must not overwrite that."""
    email = "curated@example.edu"
    idp = _fresh(email)
    con = connect()
    try:
        con.execute("INSERT INTO allowlist(email, note, added_by, added_at) "
                    "VALUES (?,?,?,?)", (email, "added by hand", "admin@example.edu", 1.0))
        con.commit()
    finally:
        con.close()
    with TestClient(app) as c:
        q = _start(c, idp)
        # Assert the SUCCESS redirect: a failure redirect is a 303 too, and a
        # failed callback provisions nothing, so the untouched row below would
        # be trivially true with sign-in completely broken.
        assert _callback(c, q["state"]).headers.get("location") == "/"
    row = _rows("SELECT note, added_by FROM allowlist WHERE email=?", (email,))[0]
    assert row["added_by"] == "admin@example.edu", row["added_by"]
    assert row["note"] == "added by hand", row["note"]


def test_a_blocked_address_is_refused_though_the_provider_authenticated_it():
    """The provider is the authority on identity, not on access to this app."""
    email = "blocked@example.edu"
    idp = _fresh(email)
    con = connect()
    try:
        admin_router._block_canonical(con, email)
        con.commit()
    finally:
        con.close()
    with TestClient(app) as c:
        q = _start(c, idp)
        r = _callback(c, q["state"])
    assert _error_of(r) == "denied", r.headers
    assert not _signed_in(r)
    assert not _rows("SELECT email FROM allowlist WHERE email=?", (email,)), (
        "a blocked address was provisioned on its way to being refused")


def test_removing_a_user_under_oidc_stops_them_signing_back_in():
    """Without the block, Remove is a NO-OP under an IdP: the next sign-in
    re-provisions the allowlist row and the person walks back in."""
    email = "departing@example.edu"
    idp = _fresh(email)
    with TestClient(app) as c:
        q = _start(c, idp)
        assert _callback(c, q["state"]).headers.get("location") == "/"
        assert _rows("SELECT email FROM allowlist WHERE email=?", (email,))

        con = connect()
        try:
            admin_router._remove_user(con, email)
            con.commit()
        finally:
            con.close()
        assert not _rows("SELECT email FROM allowlist WHERE email=?", (email,))

        idp2 = fakeidp.FakeIdp()
        idp2.claims = {"email": email}
        _install(idp2)
        q2 = _start(c, idp2)
        again = _callback(c, q2["state"])
    assert _error_of(again) == "denied", again.headers
    assert not _rows("SELECT email FROM allowlist WHERE email=?", (email,)), (
        "the removed user was re-provisioned by their next sign-in")


def test_an_address_bound_to_another_provider_subject_is_refused():
    """nOAuth. The account key is the email claim, and at several providers that
    claim is neither verified nor immutable — Entra lets a guest set it and emits
    no `email_verified`, so the "is it false?" check never fires. A guest setting
    theirs to an admin's address would otherwise inherit is_admin=1.

    Neither fence helps: the forged claim IS in the allowed domain."""
    email = "victim@example.edu"
    idp = _fresh(email)
    with TestClient(app) as c:
        q = _start(c, idp)
        assert _signed_in(_callback(c, q["state"])), "the legitimate owner could not sign in"

        # Same address, different provider subject.
        impostor = fakeidp.FakeIdp()
        impostor.claims = {"email": email, "sub": "a-different-subject"}
        _install(impostor)
        q2 = _start(c, impostor)
        r = _callback(c, q2["state"])
    assert not _signed_in(r), "an impostor claiming a bound address was signed in"
    assert _error_of(r) == "not_authorized", r.headers


def test_the_binding_is_trust_on_first_use_not_a_lockout():
    """An account that has never signed in via OIDC — every magic-link account,
    and everything predating migration 39 — must bind rather than be refused."""
    email = "firsttime@example.edu"
    idp = _fresh(email)
    con = connect()
    try:
        con.execute("INSERT INTO users(email, created_at) VALUES (?,?)", (email, 1.0))
        con.commit()
    finally:
        con.close()
    with TestClient(app) as c:
        q = _start(c, idp)
        assert _signed_in(_callback(c, q["state"])), "an unbound account was refused"
    row = _rows("SELECT oidc_iss, oidc_sub FROM users WHERE email=?", (email,))[0]
    assert row["oidc_sub"], "the account was not bound on first use"


def test_a_re_added_user_can_sign_in_again():
    """The allowlist wins over a denial — `auth.is_denied`'s own documented
    invariant, which `request_login` honours.

    The reachable path: remove someone (which blocks them under an IdP), then
    re-add them through the CSV bulk import, which deliberately does NOT clear a
    denial. Check the denial first and they are locked out permanently while the
    admin sees them listed as a user."""
    email = "returning@example.edu"
    idp = _fresh(email)
    con = connect()
    try:
        admin_router._block_canonical(con, email)
        con.execute("INSERT INTO allowlist(email, note, added_by, added_at) "
                    "VALUES (?,?,?,?) ON CONFLICT(email) DO NOTHING",
                    (email, "re-added in bulk", "admin@example.edu", 1.0))
        con.commit()
    finally:
        con.close()
    with TestClient(app) as c:
        q = _start(c, idp)
        r = _callback(c, q["state"])
    assert _signed_in(r), "a re-added user is still locked out by a stale denial"


# --- the magic-link door is closed ------------------------------------------

def test_the_magic_link_routes_404_under_oidc():
    """"Exactly one method" has to be true of the CODE, not just the docs.

    Auto-provisioning gives every SSO user an allowlist row, and request_login
    checks the allowlist FIRST — so anyone the provider has deactivated, removed
    from the required group, or put behind MFA could otherwise ask for an email
    link and walk straight past all of it. The browser hides the form, which
    makes the open door invisible to everyone except somebody looking for it."""
    with TestClient(app) as c:
        assert c.post("/api/auth/request",
                      json={"email": "jdoe@example.edu"}).status_code == 404
        assert c.post("/api/auth/verify", json={"token": "x" * 20}).status_code == 404
        assert c.post("/api/auth/verify-info", json={"token": "x" * 20}).status_code == 404
        assert c.get("/api/auth/verify", params={"token": "x" * 20},
                     follow_redirects=False).status_code == 404


# --- the provider's own answers ---------------------------------------------

def test_discovery_naming_an_offhost_endpoint_is_refused_before_use():
    """A doctored discovery document is how a server gets talked into POSTing its
    client secret somewhere else. The endpoint must be rejected BEFORE any
    request is issued to it."""
    idp = _fresh()
    idp.discovery = {"issuer": fakeidp.ISSUER,
                     "authorization_endpoint": f"{fakeidp.ISSUER}/authorize",
                     "token_endpoint": "https://evil.test/token",
                     "jwks_uri": f"{fakeidp.ISSUER}/jwks"}
    with TestClient(app) as c:
        r = c.post("/api/auth/oidc/start")
    assert r.status_code == 502, r.text
    assert not any("evil.test" in u for u in idp.seen_urls), idp.seen_urls


def test_discovery_claiming_another_issuer_is_refused():
    idp = _fresh()
    doc = fakeidp.FakeIdp().discovery_document()
    idp.discovery = {**doc, "issuer": "https://idp.evil.test"}
    with TestClient(app) as c:
        assert c.post("/api/auth/oidc/start").status_code == 502


def test_a_jwks_failure_fails_closed():
    """Deliberately NOT version.py's fail-open: the cost there is a missing
    banner, the cost here is signing somebody in unverified."""
    idp = _fresh()
    with TestClient(app) as c:
        q = _start(c, idp)
        idp.jwks_status = 500
        r = _callback(c, q["state"])
    assert not _signed_in(r), "signed in without verifying the token"
    assert _error_of(r) == "provider_unreachable", r.headers


def test_an_unknown_kid_refetches_the_keys_once():
    """Key rotation has to work without a restart — and a stream of bogus kids
    must not become a stream of requests at the provider."""
    idp = _fresh()
    with TestClient(app) as c:
        q = _start(c, idp)
        idp.kid = "a-kid-the-app-has-never-seen"
        before = idp.jwks_requests
        r = _callback(c, q["state"])
        assert _error_of(r) == "provider_error", r.headers
        after_first = idp.jwks_requests
        q2 = _start(c, idp)
        _callback(c, q2["state"])
    # Two on a cold cache: the ordinary fill, then ONE forced refetch for the
    # unknown kid. The forced one is the rotation path, and it has to happen even
    # though the cache was only just populated -- measuring the floor from any
    # fetch rather than from the last FORCED fetch would suppress exactly this.
    assert after_first == before + 2, (
        f"expected a fill plus one forced refetch, got {after_first - before}")
    assert idp.jwks_requests == after_first, (
        "the refetch floor did not hold: a second unknown kid hit the provider again")


def test_a_token_endpoint_error_is_refused():
    assert _refused_token(token_status=400) == "provider_error"


# --- the method guard -------------------------------------------------------

def test_the_oidc_routes_404_when_the_method_is_not_oidc():
    _env(AUTH_METHOD="magic_link")
    try:
        with TestClient(app) as c:
            assert c.post("/api/auth/oidc/start").status_code == 404
            assert c.get(CALLBACK, params={"code": "x", "state": "y"},
                         follow_redirects=False).status_code == 404
    finally:
        _env(AUTH_METHOD="oidc")


def test_an_incomplete_oidc_config_closes_the_routes_too():
    """A deployment whose OIDC settings are half-filled is SERVING magic link, so
    its OIDC routes must be shut as well."""
    _env(OIDC_ISSUER="")
    try:
        with TestClient(app) as c:
            assert c.post("/api/auth/oidc/start").status_code == 404
    finally:
        _env(OIDC_ISSUER=fakeidp.ISSUER)


def test_starting_a_login_is_rate_limited_per_ip():
    """/oidc/start needs no credential, writes a row, and can issue an outbound
    discovery request from a threadpool worker. Unlimited, a burst is both
    unbounded growth in app.db and a way to park every sync route's thread
    behind a slow provider."""
    idp = _fresh()
    # Earlier cases in this file already filled this IP's window, so clear it --
    # otherwise the cap is exceeded before the first call and the test proves
    # only that the limiter exists, not where it trips.
    con = connect()
    try:
        con.execute("DELETE FROM auth_request_attempts")
        con.commit()
    finally:
        con.close()
    _env(AUTH_RATE_MAX_PER_IP="3")
    try:
        with TestClient(app) as c:
            codes = [c.post("/api/auth/oidc/start").status_code for _ in range(6)]
    finally:
        _env(AUTH_RATE_MAX_PER_IP="1000")
    assert 429 in codes, f"the per-IP limiter never fired: {codes}"
    assert codes[0] == 200, f"it fired immediately instead of after the cap: {codes}"
    assert idp.discovery_requests <= 3, (
        "a rate-limited start still reached the provider")


def test_the_error_code_comes_from_the_closed_set():
    """The redirect target must never carry provider-controlled text."""
    _fresh()
    with TestClient(app) as c:
        r = c.get(CALLBACK, params={"error": "<script>alert(1)</script>",
                                    "code": "c", "state": "s"},
                  follow_redirects=False)
    loc = r.headers["location"]
    assert "<script>" not in loc, loc
    assert _error_of(r) in oidc.AUTH_ERRORS, loc


def test_every_error_code_has_browser_copy():
    """The five codes live in app/oidc.py and again in frontend/src/authcopy.js,
    and nothing else can compare the two. Adding a sixth on this side without
    the other silently degrades a specific message to generic copy, with every
    test on both sides green -- the shape estimate.py/estimate.js already pins
    across the same language boundary."""
    js = (Path(__file__).resolve().parents[2]
          / "frontend" / "src" / "authcopy.js").read_text(encoding="utf-8")
    missing = [c for c in sorted(oidc.AUTH_ERRORS) if f"{c}:" not in js]
    assert not missing, f"no browser copy for {missing} in frontend/src/authcopy.js"


def test_config_reports_the_active_method_and_label():
    with TestClient(app) as c:
        body = c.get("/api/auth/config").json()
    assert body["auth_method"] == "oidc", body
    assert body["oidc_button_label"], body
    assert fakeidp.ISSUER not in str(body), f"the issuer must not be public: {body}"


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
    print("\n1. the authorization URL")
    check("start builds an authorization URL on the issuer",
          test_start_builds_an_authorization_url_on_the_issuer)
    check("redirect_uri ignores the Host header", test_redirect_uri_ignores_the_host_header)
    check("start stores only the state hash", test_start_stores_only_the_state_hash)

    print("\n2. the callback")
    check("callback signs in and sets a session cookie",
          test_callback_signs_in_and_sets_a_session_cookie)
    check("the token exchange carries the PKCE verifier",
          test_the_token_exchange_carries_the_pkce_verifier)
    check("replaying the same state is refused", test_replaying_the_same_state_is_refused)
    check("an unknown state never reaches the token endpoint",
          test_an_unknown_state_never_reaches_the_token_endpoint)
    check("an expired state is refused", test_an_expired_state_is_refused)

    print("\n2b. the browser binding")
    check("a callback without the binding cookie is refused",
          test_a_callback_without_the_binding_cookie_is_refused)
    check("a callback with the wrong binding cookie is refused",
          test_a_callback_with_the_wrong_binding_cookie_is_refused)
    check("the binding cookie is httponly and lax",
          test_the_binding_cookie_is_httponly_and_lax)

    print("\n3. the id_token")
    check("a token signed with the wrong key is refused",
          test_a_token_signed_with_the_wrong_key_is_refused)
    check("an unsigned token is refused", test_an_unsigned_token_is_refused)
    check("a symmetric algorithm is refused even when the provider publishes the key",
          test_a_symmetric_algorithm_is_refused_even_when_the_provider_publishes_the_key)
    check("a mismatched nonce is refused", test_a_mismatched_nonce_is_refused)
    check("a wrong audience is refused", test_a_wrong_audience_is_refused)
    check("a wrong issuer is refused", test_a_wrong_issuer_is_refused)
    check("an expired token is refused", test_an_expired_token_is_refused)
    check("a token response with no id_token is refused",
          test_a_token_response_with_no_id_token_is_refused)

    print("\n4. the fences")
    check("an unverified email is refused", test_an_unverified_email_is_refused)
    check("a missing email claim provisions nobody",
          test_a_missing_email_claim_provisions_nobody)
    check("a domain outside the fence is refused", test_a_domain_outside_the_fence_is_refused)
    check("the domain fence admits a member", test_the_domain_fence_admits_a_member)
    check("a user outside the required group is refused",
          test_a_user_outside_the_required_group_is_refused)
    check("the required group admits a member", test_the_required_group_admits_a_member)
    check("a token naming another client in azp is refused",
          test_a_token_naming_another_client_in_azp_is_refused)
    check("openid is always requested even if the operator omits it",
          test_openid_is_always_requested_even_if_the_operator_omits_it)

    print("\n5. provisioning and revocation")
    check("first sign-in provisions a user and an allowlist row",
          test_first_sign_in_provisions_a_user_and_an_allowlist_row)
    check("provisioning does not relabel an existing allowlist row",
          test_provisioning_does_not_relabel_an_existing_allowlist_row)
    check("a blocked address is refused though the provider authenticated it",
          test_a_blocked_address_is_refused_though_the_provider_authenticated_it)
    check("removing a user under oidc stops them signing back in",
          test_removing_a_user_under_oidc_stops_them_signing_back_in)

    check("an address bound to another provider subject is refused",
          test_an_address_bound_to_another_provider_subject_is_refused)
    check("the binding is trust-on-first-use, not a lockout",
          test_the_binding_is_trust_on_first_use_not_a_lockout)
    check("a re-added user can sign in again", test_a_re_added_user_can_sign_in_again)
    check("the magic-link routes 404 under oidc",
          test_the_magic_link_routes_404_under_oidc)

    print("\n6. what the provider says")
    check("discovery naming an off-host endpoint is refused before use",
          test_discovery_naming_an_offhost_endpoint_is_refused_before_use)
    check("discovery claiming another issuer is refused",
          test_discovery_claiming_another_issuer_is_refused)
    check("a JWKS failure fails closed", test_a_jwks_failure_fails_closed)
    check("an unknown kid refetches the keys once",
          test_an_unknown_kid_refetches_the_keys_once)
    check("a token endpoint error is refused", test_a_token_endpoint_error_is_refused)

    print("\n7. the method guard")
    check("the OIDC routes 404 when the method is not oidc",
          test_the_oidc_routes_404_when_the_method_is_not_oidc)
    check("an incomplete OIDC config closes the routes too",
          test_an_incomplete_oidc_config_closes_the_routes_too)
    check("starting a login is rate limited per IP",
          test_starting_a_login_is_rate_limited_per_ip)
    check("every error code has browser copy", test_every_error_code_has_browser_copy)
    check("the error code comes from the closed set",
          test_the_error_code_comes_from_the_closed_set)
    check("config reports the active method and label",
          test_config_reports_the_active_method_and_label)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL OIDC TESTS PASSED")


if __name__ == "__main__":
    run()
