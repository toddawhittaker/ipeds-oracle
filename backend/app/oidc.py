"""OpenID Connect sign-in: authorization code + PKCE, server-side.

**The protocol is Authlib's, not ours.** `OAuth2Client` builds the authorization
URL and runs the token exchange; `joserfc` (Authlib's own JOSE stack) parses the
JWKS and verifies the id_token's signature; `CodeIDToken` validates its claims,
nonce included. Hand-rolling any of that is how `alg: none` and audience-confusion
bugs get written, and none of it is this project's problem to solve.

Two things ARE ours, and both are guards on top of the library rather than
reimplementations of it:

**1. The discovery document is validated before it is trusted.** The issuer must
be https, the document must claim that same issuer (RFC 8414 -- the mix-up
defence), no redirect is followed on the way, and every endpoint it names must
be an https URL, checked BEFORE any request is issued to it.

What it does NOT do is require those endpoints to share the issuer's host. That
was the first version, and it rejected Google Workspace -- whose issuer is
`accounts.google.com` while its token endpoint lives on `oauth2.googleapis.com`
-- a provider this app's README lists as supported. Same-origin was stricter
than the spec, and the security it appeared to add was illusory: the document
arrives over verified TLS from a host the operator configured, so an attacker
able to change what it says is the provider, and a provider can assert any
identity it likes wherever its endpoints sit.

**2. Failure is CLOSED.** This is deliberately not `version.py`, whose outbound
check fails open because the worst case there is a missing update banner. Here
the worst case is signing somebody in without having verified who they are, so
any step that cannot complete refuses the login. A TTL cache serving a still-valid
JWKS through a brief provider outage is fine -- that is what a TTL is for -- but
there is no path that skips verification because a key could not be fetched.

**Authlib does the protocol; this module does the HTTP.** We use its
transport-agnostic builders (`prepare_grant_uri`, `prepare_token_request`,
`create_s256_code_challenge`) rather than its `OAuth2Client`, and the reason is
concrete: Authlib's httpx integration binds **httpx2** whenever that package is
importable, and it is -- the MCP SDK pulls it in. So `OAuth2Client` would quietly
run this app's sign-in on a different HTTP library from every other outbound call
here (`nces.py`, `llmhttp.py`, `version.py`), pinned by nothing in
requirements.txt, and switching underneath us the day `mcp` drops that dependency.
This repo has already been bitten once by httpx2's mere presence changing what
Starlette's TestClient used.

What we give up is a form POST and a query string, both built BY Authlib from its
own parameter helpers. What we keep is one HTTP stack, `nces.py`'s hardening, and
a transport that tests can substitute.

The test seam is therefore `transport`: tests pass an `httpx.MockTransport`,
production passes None. See `backend/tests/fakeidp.py`.
"""
from __future__ import annotations

import hmac
import logging
import sqlite3
import time
from typing import NamedTuple
from urllib.parse import urlsplit

import httpx
from authlib.oauth2.rfc6749.parameters import prepare_grant_uri, prepare_token_request
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from authlib.oidc.core import CodeIDToken
from joserfc import jwt as jose_jwt
from joserfc.jwk import KeySet

from app.config import get_settings
from app.csrf import LOOPBACK_HOSTS
from app.security import hash_token, new_token

log = logging.getLogger("ipeds.oidc")

# Where the provider sends the browser back. A PATH constant, because the origin
# half must come from app_public_url and never from a request -- see redirect_uri.
CALLBACK_PATH = "/api/auth/oidc/callback"

# Every error the callback may name in its redirect back to the SPA. A CLOSED
# SET: the redirect target is otherwise somewhere to reflect provider-controlled
# text, which is the shape of the py/url-redirection alert this repo already
# closed once on the magic-link bounce.
AUTH_ERRORS = frozenset({
    "invalid_state",        # unknown, replayed or expired state -- start again
    "provider_error",       # the provider refused, or its answer did not verify
    "provider_unreachable",  # discovery/JWKS/token could not be fetched
    "not_authorized",       # authenticated, but fenced out by domain/group
    "denied",               # authenticated, but blocked in this app
})

# Signature algorithms accepted for an id_token. A FIXED allowlist passed to the
# verifier: omitting it, or reading it from the token's own header, is what
# admits `alg: none` and RS-to-HS confusion.
_ALLOWED_ALGS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
                 "ES256", "ES384", "ES512")

# Security bounds, deliberately constants rather than settings: every setting
# costs an .env.example line and a decision an operator should not have to make,
# and loosening these is never the right fix for anything.
_STATE_TTL_SECONDS = 600
_DISCOVERY_TTL_SECONDS = 3600
_JWKS_TTL_SECONDS = 3600
# A token that fails verification triggers one JWKS refetch, so key rotation
# works without a restart. It is keyed on "verification failed" rather than
# specifically on an unknown `kid` -- `_decode_id_token` cannot cheaply tell a
# rotated key from a forged signature -- which is why the floor matters: without
# it a stream of forged tokens is a stream of requests at the provider.
_JWKS_MIN_REFETCH_SECONDS = 60

_cache: dict = {"discovery": None, "discovery_at": 0.0,
                "jwks": None, "jwks_at": 0.0, "jwks_forced_at": 0.0}

# THE test seam, and there is deliberately only one. Production leaves this None,
# which is httpx's own default (a real network transport). `backend/tests/
# fakeidp.py` sets it to an httpx.MockTransport, and then the entire flow --
# discovery, JWKS, the token exchange, signature verification -- runs in-process
# against a provider that never touches a socket. Every function below also takes
# an explicit `transport` for direct unit tests; this global is what lets a test
# drive the ROUTES, which have no parameter to pass one through.
_TRANSPORT: httpx.BaseTransport | None = None


def state_cookie_name() -> str:
    """Derived from the session cookie's name so a deployment that renames one
    renames both, and two apps on one host cannot collide."""
    return f"{get_settings().cookie_name}_oidc"


def set_state_cookie(response, state: str) -> None:
    """Bind this login attempt to THIS browser.

    Without it the `state` row is a global bearer ticket: anyone who starts a
    login can hand the resulting callback URL to somebody else's browser, and
    that browser gets signed in as the person who started it -- classic login
    CSRF / session fixation, and everything the victim then types lands in the
    attacker's account. The single-use row alone does not stop it, because
    "used once" is not "used by the browser that asked".

    `samesite="lax"` is required, not merely chosen: the provider redirects back
    with a cross-site top-level GET, which `strict` would drop -- and the cookie
    being absent is exactly the state we refuse.
    """
    s = get_settings()
    response.set_cookie(
        state_cookie_name(), state, max_age=_STATE_TTL_SECONDS,
        httponly=True, secure=s.cookie_secure, samesite="lax", path="/")


def clear_state_cookie(response) -> None:
    """Drop the binding once the attempt is over, whichever way it ended."""
    response.delete_cookie(state_cookie_name(), path="/")


class IdpIdentity(NamedTuple):
    """Who the provider says this is. `subject` is the stable identifier -- the
    email can be reassigned or, at some providers, set by the user -- so it is
    carried out of here rather than discarded (see auth.bind_idp_identity)."""

    email: str
    issuer: str
    subject: str


class OidcError(Exception):
    """A login that cannot proceed. `code` is always a member of AUTH_ERRORS, so
    a caller can put it in a redirect without sanitising anything."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in AUTH_ERRORS:
            raise ValueError(f"OidcError code {code!r} is not in AUTH_ERRORS")
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def _transport(explicit: httpx.BaseTransport | None) -> httpx.BaseTransport | None:
    """An explicitly-passed transport wins; otherwise the module seam (None in
    production)."""
    return explicit if explicit is not None else _TRANSPORT


def _clear_caches() -> None:
    """Drop the discovery/JWKS caches. For tests, and for them alone."""
    _cache.update({"discovery": None, "discovery_at": 0.0,
                   "jwks": None, "jwks_at": 0.0, "jwks_forced_at": 0.0})


def redirect_uri() -> str:
    """The callback URL, built from the CONFIGURED public origin.

    No caller may pass one in, and that is the point: a redirect_uri derived from
    the request follows the attacker-controllable Host header, which against a
    provider configured with a wildcard redirect is account takeover. It is the
    same trap `auth.mint_login_link` documents for the magic-link URL, wearing a
    different hat."""
    base = get_settings().app_public_url.strip().rstrip("/")
    return f"{base}{CALLBACK_PATH}"


def is_secure_url(url: str, s=None) -> bool:
    """True for an https URL -- or a LOOPBACK http one in the dev posture.

    Every endpoint the discovery document names has to clear this, and so does
    the issuer itself. It is deliberately NOT a same-origin check against the
    issuer (see `discover`).

    The carve-out exists because a local identity provider -- the Keycloak in
    `compose.test.yaml` -- serves plain http on localhost, so without it the one
    configuration a developer can actually stand up is the one configuration
    this app refuses. It is gated exactly the way `csrf.py`'s loopback exception
    is: only when `cookie_secure` is false, which is the documented dev posture
    and is never true of a production deployment (the boot check screams if an
    https public URL is served with insecure cookies). And only for a loopback
    host, so it can never admit a remote http issuer.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme == "https" and parts.hostname:
        return True
    if scheme != "http" or not parts.hostname:
        return False
    # `s` is threaded in by authmethod, whose whole contract is purity over a
    # settings object it is handed -- reading get_settings() here would make its
    # boot check consult the real environment while its tests pass a namespace.
    s = s if s is not None else get_settings()
    return not s.cookie_secure and parts.hostname.lower() in LOOPBACK_HOSTS


def _get_json(url: str, transport: httpx.BaseTransport | None) -> dict:
    """One GET, no redirects followed. A redirect here would be a way to move the
    request off the origin we just validated, so it is an error rather than
    something to chase."""
    s = get_settings()
    try:
        with httpx.Client(transport=_transport(transport),
                          timeout=s.oidc_http_timeout_seconds,
                          follow_redirects=False) as c:
            r = c.get(url, headers={"Accept": "application/json"})
            r.raise_for_status()
            return r.json()
    except Exception as e:  # noqa: BLE001 -- every transport/JSON failure is one outcome
        raise OidcError("provider_unreachable", f"GET {url} failed: {e}") from e


def discover(transport: httpx.BaseTransport | None = None) -> dict:
    """The provider's OpenID configuration, cached, and validated before use."""
    now = time.time()
    if _cache["discovery"] and now - _cache["discovery_at"] < _DISCOVERY_TTL_SECONDS:
        return _cache["discovery"]

    issuer = get_settings().oidc_issuer.strip().rstrip("/")
    doc = _get_json(f"{issuer}/.well-known/openid-configuration", transport)

    # RFC 8414: the document has to claim the issuer we asked for. Without this a
    # provider (or anything that can answer for it) can present another issuer's
    # metadata and turn this into a mix-up attack.
    claimed = str(doc.get("issuer", "")).rstrip("/")
    if claimed != issuer:
        raise OidcError("provider_error",
                        f"discovery names issuer {claimed!r}, expected {issuer!r}")

    # Every endpoint must be present and https, checked BEFORE anything is sent
    # to it. Deliberately NOT a same-origin check against the issuer: that is
    # stricter than RFC 8414 ever required, and it breaks conforming providers.
    # Google is the one that matters -- its issuer is accounts.google.com while
    # its token_endpoint is on oauth2.googleapis.com and its jwks_uri on
    # www.googleapis.com, so a same-origin rule rejects Google Workspace
    # outright, which this app's own README lists as supported.
    #
    # What actually defends the client secret is the chain around this: the
    # document is fetched over verified TLS from an https issuer the OPERATOR
    # configured, no redirect is followed on the way, and the document must
    # claim that same issuer (above). An attacker who can alter what that host
    # returns IS the provider, and a provider can assert any identity it likes
    # regardless of which host its endpoints sit on. The https requirement is
    # the part still doing work: it stops a downgrade to http, and stops a
    # document naming a file:// or an internal non-TLS address.
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        value = doc.get(key)
        if not value:
            raise OidcError("provider_error", f"discovery is missing {key}")
        if not is_secure_url(str(value)):
            raise OidcError("provider_error", f"{key} {value!r} is not an https URL")

    _cache["discovery"], _cache["discovery_at"] = doc, now
    return doc


def jwks(transport: httpx.BaseTransport | None = None, *, force: bool = False) -> dict:
    """The provider's signing keys, cached.

    `force` refetches for an id_token naming an unknown `kid` -- key rotation
    without a restart -- but not more often than _JWKS_MIN_REFETCH_SECONDS. When
    that floor blocks a refetch the caller gets the keys we already have, so an
    unknown kid still fails verification. Refusing is the correct outcome; the
    floor only stops it costing the provider a request every time.

    The floor is measured from the last FORCED refetch, not from the last fetch
    of any kind. Measuring it from any fetch looks equivalent and is not: an
    ordinary cache fill would start the clock, so a provider that rotated its
    keys moments later would have every login refused for a minute with the
    refetch that would have fixed it suppressed. Rotation is exactly when this
    path has to work."""
    now = time.time()
    fresh = _cache["jwks"] and now - _cache["jwks_at"] < _JWKS_TTL_SECONDS
    if fresh and not force:
        return _cache["jwks"]
    if force and _cache["jwks"] and now - _cache["jwks_forced_at"] < _JWKS_MIN_REFETCH_SECONDS:
        return _cache["jwks"]

    if force:
        # Stamped BEFORE the fetch, so a FAILING refetch still starts the clock.
        # Recording it only on success means a provider whose JWKS endpoint is
        # down gets a fresh request from every invalid token that arrives --
        # uncapped, which is the exact thing this floor exists to prevent.
        _cache["jwks_forced_at"] = now
    doc = discover(transport)
    data = _get_json(str(doc["jwks_uri"]), transport)
    _cache["jwks"], _cache["jwks_at"] = data, now
    return data


def _decode_id_token(id_token: str, transport: httpx.BaseTransport | None):
    """Verify the signature, refetching the keys once for an unknown kid."""
    try:
        return jose_jwt.decode(id_token, KeySet.import_key_set(jwks(transport)),
                               algorithms=list(_ALLOWED_ALGS))
    except OidcError:
        raise
    except Exception:
        # Could be a rotated key. Try once with a forced refetch; if it still
        # fails, it fails -- there is no third chance and no unverified path.
        try:
            return jose_jwt.decode(id_token,
                                   KeySet.import_key_set(jwks(transport, force=True)),
                                   algorithms=list(_ALLOWED_ALGS))
        except OidcError:
            raise
        except Exception as e:  # noqa: BLE001
            raise OidcError("provider_error", f"id_token did not verify: {e}") from e


def verify_id_token(id_token: str, nonce: str,
                    transport: httpx.BaseTransport | None = None) -> dict:
    """Signature + claims. Returns the verified claims.

    `CodeIDToken` checks the nonce for us when it is passed as a param, along
    with exp/iat/iss/aud -- so the one thing a hand-rolled version reliably
    forgets is the one thing the library does by default."""
    s = get_settings()
    token = _decode_id_token(id_token, transport)
    issuer = str(discover(transport)["issuer"])
    claims = CodeIDToken(
        token.claims, token.header,
        # client_id is here so Authlib runs its own `azp` check, not just the
        # nonce one -- without it `validate_azp` is inert and an id_token naming
        # us in `aud` while naming somebody else in `azp` is accepted.
        params={"nonce": nonce, "client_id": s.oidc_client_id.strip()},
        options={"iss": {"essential": True, "value": issuer},
                 "aud": {"essential": True, "value": s.oidc_client_id.strip()},
                 "exp": {"essential": True}, "iat": {"essential": True},
                 "sub": {"essential": True}},
    )
    try:
        claims.validate(now=int(time.time()), leeway=60)
    except Exception as e:  # noqa: BLE001
        raise OidcError("provider_error", f"id_token claims rejected: {e}") from e

    return dict(token.claims)


def _groups(claims: dict) -> list[str]:
    """Group memberships, accepting the two shapes providers actually emit: a
    list, or a space-separated string."""
    raw = claims.get(get_settings().oidc_groups_claim)
    if isinstance(raw, str):
        return raw.split()
    if isinstance(raw, list):
        return [str(g) for g in raw]
    return []


def email_from_claims(claims: dict) -> str:
    """The address this login is for, after the fences. Pure.

    There is no fallback to `sub` or `preferred_username` when the email claim is
    missing. `users.email` is this app's identity key across users, allowlist,
    access_requests, api_keys and every conversation, and a UUID in that column
    makes an account an admin cannot recognise, address or offboard."""
    s = get_settings()
    email = str(claims.get(s.oidc_email_claim) or "").strip().lower()
    if not email or "@" not in email:
        raise OidcError("not_authorized",
                        f"no usable {s.oidc_email_claim!r} claim on the id_token")
    # Absent is accepted -- plenty of providers omit it. An explicit false is not:
    # an unverified address is one the provider is telling us it cannot vouch for.
    if claims.get("email_verified") is False:
        raise OidcError("not_authorized", f"{email} is not verified at the provider")

    domains = s.oidc_allowed_domain_list
    if domains and email.rsplit("@", 1)[-1] not in domains:
        raise OidcError("not_authorized", f"{email} is outside OIDC_ALLOWED_DOMAINS")

    required = s.oidc_required_group.strip()
    if required and required not in _groups(claims):
        raise OidcError("not_authorized", f"{email} is not in {required!r}")
    return email


def begin_login(con: sqlite3.Connection,
                transport: httpx.BaseTransport | None = None) -> tuple[str, str]:
    """Record a pending login. Returns `(authorization_url, state)`.

    The caller must put that `state` in the browser's cookie via
    `set_state_cookie` -- the DB row proves a login was started HERE, and the
    cookie is what proves it was started by THIS browser.

    Only the state's HASH is stored, the same rule `login_tokens` follows: the
    value that comes back in the query string is the secret, and a dump of app.db
    must not let anyone forge one. Does NOT commit -- the caller owns the
    transaction."""
    s = get_settings()
    doc = discover(transport)
    state, nonce, verifier = new_token(), new_token(), new_token()
    url = prepare_grant_uri(
        str(doc["authorization_endpoint"]),
        client_id=s.oidc_client_id.strip(),
        response_type="code",
        redirect_uri=redirect_uri(),
        scope=" ".join(s.oidc_scope_list),
        state=state,
        nonce=nonce,
        code_challenge=create_s256_code_challenge(verifier),
        code_challenge_method="S256")
    now = time.time()
    con.execute(
        "INSERT INTO oidc_logins(state_hash, nonce, code_verifier, created_at, expires_at) "
        "VALUES (?,?,?,?,?)",
        (hash_token(state), nonce, verifier, now, now + _STATE_TTL_SECONDS))
    return url, state


def claim_state(con: sqlite3.Connection, state: str,
                cookie_state: str | None) -> tuple[str, str]:
    """Check this callback is ours, burn the pending-login row, and hand back
    `(nonce, code_verifier)`.

    Split from the network half deliberately: the caller commits and CLOSES the
    connection before any provider round trip. Holding the app.db write lock
    across a token exchange means a slow provider makes every other writer in
    the app fail on `busy_timeout` -- `admin._approve_allowlist` documents the
    same rule for a mail round trip, and `begin_login` already does discovery
    before its INSERT.

    Two checks, and both are needed. The ROW proves a login was started here.
    The COOKIE proves it was started by this browser -- without it the state is
    a global bearer ticket, and anyone who starts a login can hand the callback
    URL to somebody else's browser and sign that browser in as themselves
    (login CSRF / session fixation). The check lives here rather than in the
    route so a caller cannot forget it.

    The burn is a CONDITIONAL update on `used_at IS NULL` with a rowcount check,
    so two simultaneous callbacks carrying one state cannot both proceed: single
    use is enforced by the database, not by the gap between a SELECT and an
    UPDATE.
    """
    if not cookie_state or not hmac.compare_digest(cookie_state, state):
        raise OidcError("invalid_state",
                        "the callback did not come from the browser that started this login")
    row = con.execute(
        "SELECT nonce, code_verifier, expires_at, used_at FROM oidc_logins WHERE state_hash=?",
        (hash_token(state),)).fetchone()
    if not row or row["used_at"] is not None or row["expires_at"] < time.time():
        raise OidcError("invalid_state", "unknown, replayed or expired state")
    claimed = con.execute(
        "UPDATE oidc_logins SET used_at=? WHERE state_hash=? AND used_at IS NULL",
        (time.time(), hash_token(state)))
    if claimed.rowcount != 1:
        raise OidcError("invalid_state", "the state was consumed concurrently")
    return row["nonce"], row["code_verifier"]


def exchange_code(code: str, nonce: str, code_verifier: str,
                  transport: httpx.BaseTransport | None = None) -> IdpIdentity:
    """Trade the code for an id_token and verify it. Touches NO database -- see
    `claim_state` for why that separation is load-bearing."""
    s = get_settings()
    doc = discover(transport)
    body = prepare_token_request("authorization_code", code=code,
                                 redirect_uri=redirect_uri(),
                                 code_verifier=code_verifier,
                                 client_id=s.oidc_client_id.strip())
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               "Accept": "application/json"}
    secret = s.oidc_client_secret.strip()
    # client_secret_basic when a secret is configured -- the OIDC default and the
    # form every provider accepts. A public client (no secret) sends client_id in
    # the body only, which is what `prepare_token_request` already put there.
    auth_pair = (s.oidc_client_id.strip(), secret) if secret else None
    try:
        with httpx.Client(transport=_transport(transport),
                          timeout=s.oidc_http_timeout_seconds,
                          follow_redirects=False) as c:
            r = c.post(str(doc["token_endpoint"]), content=body, headers=headers,
                       auth=auth_pair)
            r.raise_for_status()
            token = r.json()
    except Exception as e:  # noqa: BLE001 -- a refusal and a timeout are one outcome here
        raise OidcError("provider_error", f"token exchange failed: {e}") from e
    if not isinstance(token, dict) or token.get("error"):
        # Only the provider's OWN error fields. Logging the whole body would put
        # any token it happens to carry alongside the error into the admin Logs
        # tab, and `_REDACT_RE` matches `token=`/`bearer `, not a dict repr.
        detail = ""
        if isinstance(token, dict):
            detail = f"{token.get('error')}: {str(token.get('error_description'))[:120]}"
        raise OidcError("provider_error", f"token endpoint refused the exchange ({detail})")

    id_token = token.get("id_token")
    if not id_token:
        # A plain OAuth2 response. Without an id_token there is nothing that says
        # WHO this is, only that somebody authorised something.
        raise OidcError("provider_error", "the token response carried no id_token")
    claims = verify_id_token(str(id_token), nonce, transport)
    return IdpIdentity(email=email_from_claims(claims),
                       issuer=str(claims.get("iss") or ""),
                       subject=str(claims.get("sub") or ""))
