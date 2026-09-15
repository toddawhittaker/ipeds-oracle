# Auth & access control

## Sign-in methods

`AUTH_METHOD` picks exactly one door: **`magic_link`** (the default), **`oidc`**,
or **`ldap`**. `app/authmethod.py` resolves it, following `mailer._resolve_backend`
— a string setting, a pure resolver, and an unknown or incompletely-configured
value that degrades rather than raising.

**The fallback is always `magic_link`, and the direction is the point.** It is
the one method that cannot auto-provision: its gate is the manually curated
allowlist, so falling back can only ever narrow who gets in. A misconfiguration
logs a CRITICAL at boot naming the exact missing keys
(`main.lifespan` → `authmethod.boot_warning`) and the app still starts, on the
same terms as the cookie-posture and missing-model checks. The only hard refusal
in this app protects `app.db` from corruption; a typo'd issuer corrupts nothing,
and crashing a `restart: unless-stopped` container would lock the operator out
of the console they would fix it from.

**Both sets of routes are gated, not just the new ones.** The OIDC routes 404
unless OIDC is in force — and `POST /api/auth/request`, `POST /api/auth/verify`,
`POST /api/auth/verify-info` and the legacy `GET /api/auth/verify` bounce 404
unless magic link is. Without that second half, "exactly one method" is what
this document says and not what the code does: auto-provisioning gives every
SSO user an allowlist row, and `request_login` checks the allowlist FIRST, so
anyone the provider had deactivated, removed from the required group, or put
behind MFA could ask for an email link and walk straight past all of it. The
browser hides the form, which makes that door invisible to everyone except
somebody looking for it. Bootstrapping is unaffected — `ADMIN_EMAILS` still
grants the first admin, and an operator whose provider breaks changes
`AUTH_METHOD` back, which is also what happens automatically when the config is
incomplete.

The routes are registered **unconditionally** and each re-checks the
resolved method, answering 404 otherwise. Registration that depended on an env
read at import time would make any test that flips `AUTH_METHOD` tell a lie, and
without the check a magic-link deployment would leave a half-working OIDC entry
point exposed. Because it consults the *resolved* method, a deployment whose OIDC
settings are incomplete closes those routes too.

`GET /api/auth/config` carries the active method's NAME and its button label so
the login form knows which door to draw. That widens an endpoint whose comment
says "expose NOTHING else", deliberately: the method is unavoidably public — any
visitor learns it by loading the page — exactly as the email domain always was.
What must never cross is the issuer URL, the client id, the client secret, and
every group/domain fence, each of which is a credential or free reconnaissance
about the institution. `test_access_gate.py` pins the exact key set.

## OIDC (authorization code + PKCE)

**The protocol is Authlib's, not ours.** `prepare_grant_uri` and
`prepare_token_request` build the two requests, `create_s256_code_challenge` does
PKCE, `joserfc` verifies the signature, and `authlib.oidc.core.CodeIDToken`
validates the claims — nonce included. Hand-rolling any of that is how
`alg: none` and audience-confusion bugs get written.

**We do the HTTP ourselves, and that is not an oversight.** Authlib's
`OAuth2Client` binds **httpx2** whenever that package is importable, and it is —
the MCP SDK pulls it in. Using it would run sign-in on a different HTTP library
from every other outbound call here, pinned by nothing in `requirements.txt`, and
switching underneath us the day `mcp` drops that dependency. This repo has
already been bitten once by httpx2's mere presence changing what Starlette's
TestClient used. So the token exchange is a form POST through the same `httpx`
`nces.py` and `llmhttp.py` use, with a transport tests can substitute.

**The discovery document is validated before it is trusted.** The issuer must be
`https://`, the document must claim that same issuer (RFC 8414 — the mix-up
defence), no redirect is followed on the way, and `authorization_endpoint` /
`token_endpoint` / `jwks_uri` must each be an **https URL**, checked *before* any
request is issued to them.

**It does not require those endpoints to share the issuer's host, and that is a
correction.** The first version did, and it rejected **Google Workspace** — whose
issuer is `accounts.google.com` while its token endpoint is on
`oauth2.googleapis.com` and its JWKS on `www.googleapis.com` — a provider this
repo's README lists as supported. Same-origin was stricter than the spec ever
required, and the safety it appeared to add was illusory: the document arrives
over verified TLS from a host the *operator* configured, so anyone able to change
what it says is the provider, and a provider can assert whatever identity it
likes regardless of where its endpoints sit. The https requirement is the part
still doing work — it stops a downgrade to cleartext and stops a document naming
an internal non-TLS address.

Worth noting how this was found: not by review and not by any in-process test,
but by reading a real provider's discovery document while answering "how would I
try this live?". Entra ID, Okta, Keycloak and Auth0 all happen to use same-host
endpoints, so every fake and every reviewer agreed with each other and with the
code. `test_oidc.py` now pins Google's actual shape.

**A loopback `http://` issuer is accepted in the dev posture only** — when
`COOKIE_SECURE` is false, which is never a production deployment (the boot check
screams if an https public URL is served with insecure cookies). It is the same
carve-out `csrf.py` already makes for the Vite dev proxy, and it exists so the
local Keycloak in `compose.test.yaml` — which serves plain http on localhost —
is not the one provider this app refuses. `oidc.is_secure_url` is the single
rule, so the boot check and the sign-in path cannot disagree about it.

**Failure is CLOSED.** Deliberately not `version.py`, whose outbound check fails
open because the worst case there is a missing update banner; here the worst case
is signing somebody in unverified. A TTL cache serving a still-valid JWKS through
a brief outage is fine — that is what a TTL is for — but no path skips
verification because a key could not be fetched.

**The algorithm allowlist is load-bearing, and the test proves it.** `joserfc`
refuses an HS256 token against an RSA-only key set — but for the *wrong reason*
(no key matches the kid), which evaporates the moment a provider publishes a
symmetric key alongside its RSA one, as some do. `oidc._ALLOWED_ALGS` is what
refuses on algorithm policy. `test_oidc.py` makes RS→HS confusion genuinely
reachable by having the fake provider publish that symmetric key, and the case is
mutation-verified: passing `algorithms=None` signs the caller in as whoever the
forged token names.

**`redirect_uri` is built from `app_public_url`, never from the request**, and no
caller may pass one — it is the same Host-header trap `mint_login_link`
documents, and against a provider configured with a wildcard redirect it is
account takeover.

**The `state` is bound to the browser that started the login, and the row alone
is not enough.** `oidc_start` sets a short-lived httponly `<cookie>_oidc` cookie
holding the raw state, and the callback refuses unless it matches. The database
row proves a login was started *here*; the cookie proves it was started by *this
browser*. Without it the state is a global bearer ticket: an attacker starts a
login himself, authenticates as himself, and feeds the victim's browser the
resulting callback URL — a GET, so the CSRF layer skips it — and the victim is
signed in **as the attacker**, with everything they type afterwards landing in
his account. `samesite="lax"` is required rather than chosen: the provider
returns with a cross-site top-level GET, which `strict` would drop, and an
absent cookie is exactly what the callback refuses.

**No app.db transaction spans a provider round trip.** `claim_state` validates
and burns the row in one short transaction that commits and closes; the token
exchange and verification then run holding no connection; a second short
transaction records the outcome. Holding the write lock across the exchange
makes every other writer in the app fail on `busy_timeout` whenever the provider
is slow, which is routine for a cold tenant — the same rule
`admin._approve_allowlist` states for a mail round trip. The burn itself is a
conditional `UPDATE … WHERE used_at IS NULL` with a rowcount check, so single
use is enforced by the database rather than by the gap between a read and a
write.

**`POST /api/auth/oidc/start` is rate-limited per IP.** It needs no credential,
writes a row, and can issue an outbound discovery request from a threadpool
worker — so unlimited it is both unbounded growth in `app.db` and a way to park
every synchronous route's thread behind a slow provider. It shares
`auth_request_attempts` and the per-IP cap with the magic-link limiter, under a
sentinel email that cannot collide with a real address's budget.

**The state, nonce and PKCE verifier live in `oidc_logins` (migration 38)**, not
in a signed cookie. Only the state's HASH is stored, the rule `login_tokens`
already follows. A cookie was genuinely available (`itsdangerous` is a declared
dependency nothing imports) and was rejected for three reasons: single-use needs
a server-side record anyway; the PKCE verifier is a secret and would be handed to
the very browser PKCE protects the exchange from; and a cookie needs a signing
key, a rotation story and a `SameSite` puzzle none of which exist. The rows are
swept by `auth.purge_expired_auth_rows` on the same argument it already makes for
`login_tokens`.

**The callback is a GET, so `CSRFMiddleware` skips it** (`csrf.SAFE_METHODS`).
That is correct rather than a gap: the single-use `state` row IS that leg's CSRF
defence, and an Origin check could not apply to a request arriving from the
provider's redirect. `POST /api/auth/oidc/start` is state-changing and does go
through the origin check, which is one of the reasons it is a POST returning JSON
rather than a GET that redirects.

**Errors come from a closed set** (`oidc.AUTH_ERRORS`) and ride back as
`/?auth_error=<code>`. Never a provider-supplied string: the redirect target is
otherwise somewhere to reflect attacker text, which is the `py/url-redirection`
shape the magic-link bounce already had to close. The browser half
(`authcopy.authErrorMessage`) maps the code through a closed table, so an
unrecognised one renders our wording.

**⚠ The OIDC callback is the one credential that cannot move to a URL fragment**
— a provider's `redirect_uri` has to be a real URL — so `?code=…&state=…` genuinely
reaches uvicorn's access log, exactly the way `?token=` used to.
`logbuffer._ACCESS_CODE_RE` scrubs it, access-log-scoped (in an ordinary log
message `code=` is usually an HTTP status). PKCE means a bare code is not
redeemable alone, so this is defence in depth rather than a live hole — but the
standard here is that credentials do not land in `docker logs`.

## LDAP

`app/ldapauth.py`, on `ldap3` — pure Python, so the image needs no apt package
(`python-ldap` and `bonsai` both need C libraries). One route,
`POST /api/auth/ldap`, taking a username and a password.

**This is the one method where a password crosses the wire**, and the ORDER in
`authenticate` is the security property rather than an implementation detail:

1. **An empty password is refused before the directory is touched.** RFC 4513
   makes a simple bind with a zero-length password an *anonymous* bind, which
   most servers answer with success — "any username, no password" as a sign-in.
   `ldap3` happens to refuse it itself, so in this codebase the bug it prevents
   is an uncaught 500 where every other rejection is a neutral 401 — an
   enumeration oracle wearing a different hat. Checking it here also keeps the
   guard ours if ldap3 ever relaxes.
2. **⚠ Certificates are always verified.** `ldap3.Tls()` defaults to
   `validate=ssl.CERT_NONE`, so an `ldaps://` URI built with the default object
   encrypts the password and then accepts *any* certificate — exactly the attack
   it looks like it prevents. `ldapauth._tls` constructs it explicitly every
   time, and a test asserts the constructed `Server`'s `.tls.validate`.
3. **Plain `ldap://` is not a usable configuration** unless StartTLS is on, or
   `LDAP_ALLOW_INSECURE` is set deliberately — which logs a CRITICAL on every
   boot. Refusing at *resolution* means the operator learns at boot rather than
   from a user who cannot sign in.
4. **The username is escaped where the filter is built.** Unescaped, `*` matches
   every entry and `)(uid=admin` rewrites the query.
5. **Exactly one search result.** Zero refuses; two or more refuses loudly,
   because an ambiguous filter is a configuration error and picking one entry is
   how somebody signs in as the wrong person.
6. **The user bind reuses the finder's connection** rather than opening a second
   one — see the timing note below.

**Every rejection is the same 401** — wrong password, unknown user, not in the
group, blocked here, directory unreachable. Anything that varies by cause is a
directory enumeration oracle; the reason goes to the log, which only an admin
reads. Every LDAP and OIDC refusal line also names the client IP (from
`client_ip`, so it honours `TRUSTED_PROXY_COUNT`), so an operator grepping for a
guessing source finds it beside the reason. The request model uses `SecretStr`,
so FastAPI's own 422 echo cannot quote the password back, and a test drains the
log to prove it appears in no record. The password box has a show/hide toggle;
it flips the input's `type`, never swaps the element, so what was typed
survives the flip.

**Rate-limited before the directory is touched**, reusing the magic-link
limiter: this is the only method where online password guessing is possible, and
an unlimited endpoint would also let an attacker use the directory's round trips
as a work amplifier.

**Two bind modes.** Direct bind (`LDAP_USER_DN_TEMPLATE`) needs no service
account but cannot read an entry's groups, so configuring a group fence with it
is a config *problem* rather than a silently ignored setting. Search-then-bind
(`LDAP_BIND_DN` + `LDAP_BASE_DN`) is what Active Directory deployments run, and
the only mode the fence works in.

**⚠ Referrals are never followed** (`auto_referrals=False`, plus an empty
`allowed_referral_hosts`). ldap3's defaults are the opposite, and both halves
matter: `create_referral_connection` copies this connection's user and password
into a connection to whatever host a referral names — with TLS only if the
referral URL says so — and the referred server's answer then *replaces* the
search result. So a single referral object inside `LDAP_BASE_DN` is both an
exfiltration channel for `LDAP_BIND_PASSWORD` and a way to return
`mail: admin@example.edu` with a satisfying `memberOf`, while the password check
still runs against the real directory as the attacker's own account. ldap3
refuses to follow a referral on a *bind*, which is why the search is the
reachable half. Even with no attacker, a routine multi-domain Active Directory
referral would otherwise ship the service-account password to the referred host.

**A multi-valued `mail` is refused, not resolved.** LDAP attribute sets have no
defined order, so taking the first value means one entry can land in either of
two app accounts run to run — and if one of those addresses is an `ADMIN_EMAILS`
one, that is an admin account. Refusing is the same call the one-result rule
makes about an ambiguous search. Attribute names are matched
case-insensitively for the same class of reason: `LDAP_GROUP_MEMBER_ATTRIBUTE=memberof`
against a server answering `memberOf` would otherwise refuse everybody, and look
exactly like a real refusal.

**The user bind reuses the finder's connection** rather than opening a second
one. A second connection costs an extra TCP connect and TLS handshake, and only
when the username *exists* — tens of milliseconds of wall clock that turn the
deliberately identical 401 into a username oracle.

**⚠ The timeouts handed to ldap3 must be INTEGERS.** `ldap_timeout_seconds` is a
float, like its OIDC sibling, and ldap3 answers a float with
`error: required argument is not an integer` the moment it opens a real socket —
which `authenticate` converts into "the directory could not be reached". So the
unfixed version returned a neutral 401 for *every* sign-in against *every* real
directory, with a log line blaming the network. `MOCK_SYNC` never opens a socket,
so no test here could have found it: it took running against the OpenLDAP in
`compose.test.yaml`. `_timeout()` coerces, and a test asserts the types ldap3 is
handed.

**Not built, and why:** LDAP accounts are not bound to a directory identity the
way OIDC accounts are bound to a provider `sub` (migration 39). The nOAuth
vector does not apply — `mail` is set by the directory administrator, not
self-asserted by the user. What remains is two *different* entries carrying the
same `mail`, which the one-result rule does **not** catch (it de-duplicates
entries matching one username filter, not entries sharing an address): a shared
mailbox, an alias object or a stale duplicate would collapse into one app
account. `LDAP_ALLOWED_DOMAINS` and `LDAP_REQUIRED_GROUP_DN` are the practical
fences. Worth revisiting if a deployment has a directory where `mail` is
user-editable.

## Auto-provisioning (oidc and ldap)

**The provider is the authority on identity; the app still decides access.** This
applies to OIDC and LDAP alike. On a first successful sign-in
`auth.provision_from_idp` writes the **`allowlist` row** (the `users` row comes from `create_session`, as it does for every method).
Writing that allowlist row is the whole design: the allowlist is
re-checked on every authenticated request (`_user_from_request`) and every MCP
call (`apikeys.verify`), and those two checks are the app's only per-request kill
switch. Making them method-aware instead would leave a removed user's 30-day
cookie and their API keys alive. `ON CONFLICT DO NOTHING`, never `DO UPDATE`, so a
bootstrap or admin-curated row is not relabelled by someone's first sign-in.

**An account is bound to the provider SUBJECT that first signed into it**
(`users.oidc_iss`/`oidc_sub`, migration 39). Without that, the account key is
the `email` claim alone — and at several providers that claim is neither
verified nor immutable. Entra ID lets a guest or personal account set it and
emits no `email_verified`, so the "is it false?" check never fires; a guest who
sets theirs to an existing admin's address would inherit `is_admin=1`. That is
the published nOAuth pattern, and **neither fence defends against it**, because
the forged claim is inside the allowed domain. Trust on first use: the first
OIDC sign-in for an address records the pair, every later one must match, and an
unbound account (any magic-link account, anything predating the migration)
binds rather than being refused. Residual, stated: an attacker who forges an
address that has *never* signed in via OIDC still binds it first —
`OIDC_REQUIRED_GROUP` is the fence for that, since a guest is not in the group.

**An OIDC deployment with no fence logs a CRITICAL at boot.** Neither
`OIDC_ALLOWED_DOMAINS` nor `OIDC_REQUIRED_GROUP` set means anyone the provider
authenticates gets an account. That is a legitimate choice for a single-tenant
issuer, so it is not a config *problem* and does not fall back — but pointed at
a consumer issuer it is the internet, so it must not be silent.

Two consequences an operator has to know:

- **`is_denied` still blocks — but the allowlist wins, exactly as it does for
  magic link.** `auth.is_denied`'s own docstring states that invariant, and
  `request_login` honours it. Checking the denial unconditionally locks out
  anyone re-added through the CSV bulk import, which deliberately does not clear
  a denial: the admin sees them listed as a user while sign-in refuses them,
  with no signal anywhere. Order in the callback is browser binding → id_token →
  fences → allowlist/denied → subject binding → provision → session, so a
  blocked or mismatched address is never provisioned on its way to being
  refused.
- **Removing a user in Admin → Users also BLOCKS them** under an external
  provider (`admin._block_canonical`). Without it Remove is a no-op: the next
  sign-in re-provisions the row. The Blocked-users tab's existing unblock control
  is the undo, so this adds no new concept. It keys on the method the operator
  *asked* for, not the resolved one, so a deployment whose OIDC config is
  temporarily broken still blocks rather than silently going back to a no-op.

- Passwordless **magic link**, manual **allowlist**, email via a **pluggable
  backend** (`mail_backend`: `auto`/`resend`/`smtp`/`console`) — Resend (hosted API,
  easy pilot) or the institution's own **SMTP** (Google/Microsoft/relay, stdlib
  `smtplib`), console-log in dev. One seam: `mailer.send_email` dispatches via
  `_resolve_backend`; a backend failure is swallowed (returns False, never 500s the
  login/approval). The Outlook-safe HTML templates are backend-agnostic. The
  allowlist is the **sole authority on sign-in**.
- **The sign-in link is built from the canonical `app_public_url`, NEVER
  `request.base_url`.** `mint_login_link` reads `get_settings().app_public_url`
  internally (no caller passes a base) — a request-derived base follows the
  attacker-controllable `Host` header, so an attacker could make the server email a
  victim a genuine signed link pointing at an attacker domain (link-poisoning →
  account takeover). Every email href is also HTML-attribute-escaped (`mailer.py`).
- **The token rides in the URL FRAGMENT (`/verify#token=…`), never a query
  string** — a security property, not a style choice. `/verify` is an SPA route
  served by `main.py`'s catch-all, so a `?token=` link wrote the raw single-use
  token into **uvicorn's access log on PAGE LOAD**, before any API call; anyone
  who could read `docker logs` (routine on a self-hosted box) could replay it
  into an account takeover. `logbuffer._REDACT_RE` could not help — it only
  scrubs what reaches `logs.db`, and uvicorn sets `propagate: False` on
  `uvicorn.access`. A fragment is **never transmitted to the server**, so it
  can't be logged by us, by the operator's reverse proxy, or by a tunnel — none
  of which we control. Three parts, all load-bearing: `mint_login_link` emits
  the fragment; the legacy scanner-bounce 303 (`routers/auth.py`) redirects to a
  fragment too (a `?token=` target would just move the leak to the redirected
  page load); and **`verify-info` is a POST** with the token in the body (the
  GET form is deleted — no email ever pointed at it). `Verify.jsx` reads
  `location.hash` **with a `location.search` fallback**, which is what keeps
  every link already sitting in an inbox working — without it the fix would be a
  lockout. `logbuffer.install_access_log_redaction()` scrubs `token=` from the
  **`uvicorn.access` logger only** (not root — `make up` prints the full link on
  purpose, the documented local sign-in path) and rewrites **`record.args`, not
  `record.msg`**: uvicorn logs a constant format string with the path in
  `args[2]`, so a msg-only filter passes a naive test and leaks every token.
  Pinned by `test_magic_link_token_never_appears_in_a_server_visible_url` +
  `test_verify_info_takes_the_token_in_a_body_not_a_query_string` +
  the access-log cases in `test_logbuffer.py` + the legacy-link case in
  `frontend/e2e/auth-verify.spec.js`.
- **Boot-time cookie-posture check.** `main._insecure_cookie_warning` logs a
  **CRITICAL** on startup when `app_public_url` is `https://` but `COOKIE_SECURE`
  is false (that combo serves an insecure cookie AND relaxes the CSRF loopback
  carve-out — `csrf.py` `allow_loopback=not cookie_secure`). Logged, not raised, so
  dev/tests aren't broken; a prod misconfig screams in stderr + the admin Logs tab.
- **Approval mints no token.** Only a user's OWN `POST /api/auth/request` mints +
  emails a real one-time sign-in link (`auth.py` `mint_login_link` + `send_magic_link`).
  Admin **approve / manual-add / CSV-import** just add the allowlist row and email a
  **"you're approved — request your sign-in link"** notice (`send_access_approved`,
  no link; the button points at the login page). This keeps a `login_tokens` write
  out of the approval transaction, and — combined with the send happening only after
  commit+close — is why `_approve_allowlist` no longer carries a minted link out to
  the mailer. CSV-import sends its notices via `BackgroundTasks` (a roster can be
  hundreds). The admin toast still classifies delivery
  (`emailed`/`failed`/`logged_to_console`/`already_allowlisted`). The **admin
  access-request notification** (`send_access_request`) deep-links straight to
  `/admin/users/pending` and carries no "Reason" line (nothing ever set one). All
  three emails share one **Outlook-safe HTML shell** in `mailer.py` (`_email_document`
  + a VML bulletproof `_button`: doctype/head, **full-bleed** `role=presentation`
  tables — a teal header band edge-to-edge, no centered card — Arial not
  `system-ui`) in the app's teal palette. The band carries the real **wordmark**
  (`_wordmark_html`: Column mark · mono "IPEDS" · gold rule · serif "Oracle"),
  whose icon ships as an **inline CID attachment** (`_LOGO_PNG`, base64-embedded —
  Gmail and Outlook both refuse `data:` images), attached by *both* transports:
  Resend's `attachments=[{content,content_id,…}]` and SMTP's `add_related(…,
  cid=…)` (which nests the HTML part inside a `multipart/related` — hence
  `msg.walk()`, not `iter_parts()`, in `test_mailer.py`). The PNG is
  cream-shaft/gold-caps on purpose: the app's teal shaft is invisible on the teal
  band. `mailer.py` is E501-exempt in `pyproject.toml` because the templates are
  legitimately long.
- Optional `EMAIL_DOMAIN` keeps *access requests* to the institution's own domain
  (and feeds the login form's hint via unauthenticated `GET /api/auth/config`) — it
  does **not** gate sign-in.
- An admin can **deny** a request: it blocks that address **and every `+tag`/case
  variant**, matched on a canonical form in `access_requests.canon_email`
  (lowercased, `+tag` stripped, **dots left alone** — they can be a different real
  person). A blocked address can file no new request (no row, no admin email) and
  gets the **same neutral response** as every other path.
- **No enumeration oracle:** every branch's outbound send is scheduled via
  `BackgroundTasks`, never inline, so denial leaks nothing by response body **or**
  by wall-clock (a synchronous provider call — Resend or SMTP — on only some
  branches was a measured 400×+ timing oracle). A residual sub-ms DB-local timing difference (denied/unknown
  skip the INSERT the allowlisted/pending branches do) is **accepted** — it doesn't
  isolate the sensitive states, and equalizing it would violate "store nothing on
  deny"; see `auth.request_login`'s docstring.
- **Dead auth rows are swept in-app, not by cron:** `auth.purge_expired_auth_rows`
  deletes consumed/expired `login_tokens` and past-expiry `sessions` — rows the code
  can never accept again, so removing them changes no behaviour (the lookup misses
  instead of failing the timestamp check: same 400, same message). It runs at boot
  (`main.lifespan`, non-fatal like the seeding steps) and at the top of
  `verify_login`, before the token is marked used. Deliberately **not** in
  `mint_login_link`: that runs on only one of `request_login`'s branches, so a DELETE
  there would make "allowlisted" measurably slower than "pending" — reopening the very
  timing oracle above. (`auth_request_attempts` has its own sweep in `ratelimit.py`.)
  Pinned by `test_signing_in_purges_dead_auth_rows_only` in `backend/tests/test_security.py`.
- **Per-IP rate-limit is spoof-resistant:** `POST /api/auth/request` is capped
  per-email and per-IP (`ratelimit.py`), but `X-Forwarded-For` is client-settable.
  `client_ip` trusts it only `TRUSTED_PROXY_COUNT` hops **from the right** (a
  trusted reverse proxy/tunnel appends the real peer); `0` (dev/CI default) ignores
  XFF and uses the socket peer. Set it to **`1`** in production behind a single
  proxy/tunnel hop (via `.env`); combine with `EMAIL_DOMAIN` to close the
  access-request-spam surface. **The app must be the ONLY interpreter of XFF:**
  uvicorn ships `proxy_headers=True` trusting loopback and rewrites
  `scope["client"]` from the header, which silently defeats
  `TRUSTED_PROXY_COUNT=0` behind any loopback-adjacent ingress (ssh -L,
  cloudflared, host-network nginx). `scripts/docker-entrypoint.sh` therefore runs
  uvicorn with **`--no-proxy-headers`** — keep it there, and pass it in any
  non-Docker deployment too.
- **Per-user chat throttle (SEC-3):** `POST /api/chat/stream` is gated only by
  `current_user`, so without a cap an allowlisted user's runaway loop/script could
  burn unbounded provider spend. `ratelimit.enforce_chat_rate_limit(user_id)` — a
  sliding window over `chat_request_attempts` (**migration 28**), the same app.db
  pattern as the auth limiter — caps turns per user per `chat_rate_window_seconds`
  (`chat_rate_max_per_user` default **30/60s**; a **non-positive max DISABLES** it,
  the off-switch for tests/self-hosters). Called at the top of `chat_stream` before
  any streaming/LLM work, so a 429 is a plain JSON error, not mid-SSE. **NOT pinned
  in `ci_env.sh`** (that would mask a future stream-heavy suite that forgets to pin):
  the modules that fire many turns pin `CHAT_RATE_MAX_PER_USER=0` at import
  (`test_chat_router`/`test_guard`/`test_security`); `test_rate_limit.py` sets a tight
  cap and owns the 429 contract.
- **API keys are the MCP endpoint's only credential, and they are stored like
  session tokens.** An MCP client cannot carry a cookie, so `POST /mcp` is gated
  by a static bearer key (`app/apikeys.py`, `api_keys` in **migration 37**) minted
  from `/keys` by its owner or from Admin → Keys by an admin. The key is 32 bytes
  from `secrets.token_urlsafe` behind an `ipeds_mcp_` prefix (recognizable on
  sight in a log or a secret scanner), and **only its SHA-256 hash is stored** —
  the raw value exists exactly once, in the response that minted it, so a dump of
  `app.db` mints nothing and a lost key is replaced, never recovered. Random input,
  not a password, so sha256 is the right primitive: a slow KDF would tax every MCP
  request and buy nothing against an attacker who cannot enumerate the space.
  **`apikeys.verify` re-checks the allowlist**, the same check
  `auth._user_from_request` makes on a session — leaving the allowlist has to end
  every way in, and a key that outlived that check would be a standing grant to
  someone an admin believes they removed. Unknown key, revoked key, and
  de-allowlisted owner are **indistinguishable** to the caller (one 401, one
  wording), so probing cannot map which keys exist; revoking someone else's key
  answers **404, not 403**, for the same reason. Revocation sets `revoked_at` and
  **keeps the row** — "what did that withdrawn key have access to" is exactly the
  question an admin asks afterwards. The gate is an ASGI wrapper around the
  transport app (`app/mcpsrv/auth.py`), not a path-scoped middleware that a later
  route could slip past, and it charges a **per-key** rate limit
  (`MCP_RATE_MAX_PER_KEY`, 60/60s over `mcp_request_attempts`) before delegating;
  `ask` additionally charges the per-user chat limiter above. No cookie means no
  CSRF surface and no ambient credential to borrow — which is also why the SDK's
  DNS-rebinding protection is deliberately **off** (it would 421 every request
  behind the proxy while defending nothing). **`/mcp` is also exempt from
  `CSRFMiddleware`** (`main.py` passes the path in): with no cookie there is no
  ambient credential for a cross-origin page to borrow, and without the exemption
  every browser-hosted MCP client — the MCP Inspector among them — was refused
  with a 403 about cross-origin requests *before* the bearer gate ran. The
  endpoint is **POST-only** (405 otherwise): the route must accept every method
  to be registered at all, and the SDK answers GET with an SSE stream that the
  rate limiter charges once at open and then never sees again, so parked
  connections were free. No OAuth: no `WWW-Authenticate`, no
  `/.well-known/oauth-protected-resource`, on purpose — see
  [`MCP.md`](MCP.md).
  **Removal revokes keys.** `admin._remove_user` revokes the user's API keys in
  the same transaction that drops the allowlist row and deletes their sessions.
  `verify`'s allowlist re-check already refuses those keys while the user is off
  the list, so this is not what stops them today — it is what stops them if the
  address is ever re-added: without it, every key that person ever minted comes
  back to life with the allowlist row, including the leaked one that prompted the
  removal, with no admin action and no signal. A user may hold at most
  `keys.MAX_ACTIVE_KEYS` (10) live keys, because minting charges no limiter and
  each key carries its own MCP request budget. Pinned by
  `backend/tests/test_api_keys.py` + `backend/tests/test_mcp.py`.
- **A question is capped at `MAX_QUESTION_LEN` (4,000 chars).** `BodyLimitMiddleware`
  bounds the whole request at 10 MB, but under that ceiling an unbounded question is
  still written to `app.db` **twice** (the user message + `usage_log.question`) and
  billed as provider tokens. Enforced as a hand-raised **400 with a readable
  sentence**, matching `MAX_TITLE_LEN` — deliberately NOT `Field(max_length=…)`,
  whose 422 sends `detail` as a **LIST**. Mirrored client-side by the composer's
  `maxLength` (keep the two in sync), so the server cap is the backstop, not the UX.
  `authcopy.detailText` flattens a pydantic detail array anyway — FastAPI raises 422
  itself on any malformed body, and the raw array would reach the user as
  `[object Object]`, the same leak `ApiError` exists to end.
- **`GET /api/auth/me` reports the loaded collection years**, from ONE `ipeds_years()`
  probe that also derives `has_data` (so the two can never disagree). The chat empty
  state used to state the range as fact — "collection years 2019-20 through
  2024-25" — while every deployment picks its own years via Admin → Imports and
  `_years` is the only authority. The wording lives in the pure `years.js`
  (vitest-pinned): `year` is the **ending** year, so 2020 renders "2019-20", and a
  single loaded year reads "collection year 2024-25", never "X through X". Guard the
  empties before `Number()` — `Number(null)` is `0` and **finite**, so a naive
  `isFinite` check renders a missing bound as year zero ("-1-00 through 2024-25").
  Pinned in `years.test.js` + the empty-state describe in `chat-interactions.spec.js`
  (which asserts the text FOLLOWS the mocked bounds, so a re-hardcoded literal fails).
- **CSRF defense in depth:** the session cookie is `HttpOnly`+`Secure`+`SameSite=Lax`;
  on top of that a pure-ASGI `CSRFMiddleware` (`csrf.py`) refuses any state-changing
  request whose `Origin` matches neither the request `Host` nor `APP_PUBLIC_URL`.
  Origin-less/non-browser requests pass (SameSite still covers browsers); it's raw
  ASGI so it never buffers the chat SSE stream. In the **dev posture only** (insecure
  cookies) it also accepts loopback origins so the Vite dev-proxy (`changeOrigin`)
  works — production (Secure cookies) enforces strict same-origin.
- **A truncated table says so, and its sort admits its scope.** `run_sql` cuts at
  `sql_row_cap_model` (200) and `QueryResult.truncated` records it, but
  `to_storage` dropped that flag and `get_conversation` never selected `results`
  — so the browser had **no structured signal**, and a 200-row PAGE of an 834-row
  result was byte-identical on screen to a complete one. The only disclosure was
  whatever sentence the model remembered to write. Now plumbed: **migration 30**
  (`messages.results_truncated`), carried on the `done` SSE event so a live turn
  captions without a reload, and selected back on conversation load.
  `tabletruth.js` (pure, vitest) owns the wording. **The caption never states a
  total** — `row_count` is the count AFTER the cut and nothing runs a `COUNT(*)`,
  so "of 3,412" would be invented; it says "First 200 rows · the full result is
  larger". It is **scoped to single-table answers** via the same
  `countMarkdownTables(src) === 1 && messageId != null` gate that decides whether
  the CSV re-runs server-side, because attributing one of N results to one of N
  rendered tables is a heuristic that can pick wrong. **Sorting a truncated table
  is kept but warned**, in `--warn` tone naming the cap ("not a ranking of the
  full result") — the old note appeared only AFTER sorting, in 12px muted text,
  and counted the rows the MODEL transcribed, a number unrelated to both the cap
  and the true total. The CSV button now **names what it does**: `Download full
  result (CSV)` (server re-run at the 100k cap) vs `Download these N rows (CSV)`
  (the transcription). And `downloadServerCsv` **fetches into a blob** instead of
  clicking a bare `<a href>` with no `download` attribute — every error path
  (400/404/**429**/504) used to replace the chat view with a raw JSON page, and a
  slow export timing out is the likeliest failure. Pinned in
  `frontend/e2e/truncated-table.spec.js` + `tabletruth.test.js`.
  **The server re-run PROBES every candidate, and each probe is time-bounded**
  (`CSV_PROBE_TIMEOUT_SECONDS`, 3s). `_select_table_sql` runs each query in the
  answer's `sql_log` at `LIMIT 1` to find which one produced the table you saw —
  and `LIMIT 1` bounds the ROWS returned, never the WORK done, so an unbounded
  probe could burn the full 25s `sql_timeout_seconds`. `sql_log` records **every
  attempt the agent made, failures included**, so 5–8 candidates is routine and
  one export could hold a threadpool worker for two to three minutes. The
  **winning re-run keeps the full default budget** — it is the query the user
  actually asked for. Don't tune the probe timeout down: a probe timeout is
  swallowed by the candidate-skipping `except`, so an over-tight value turns a
  slow-but-valid table query into "No runnable query for this answer." → 400.
  **Numeric columns right-align.** `Markdown.jsx` already computed `numericByCol`
  (via `columnIsNumeric`) to pick a sort comparator; it now also puts `.num` on the
  matching `<th>`/`<td>`, so digits line up on the ones place and magnitudes are
  scannable down the column (`.num` already carried `tabular-nums lining-nums`). The
  CSS stays **`.md`-scoped** — `.num` is shared with the hero figure's big serif
  number, which is centred — and a `thead th` has `padding:0` and delegates to the
  `.th-sort` button, so the *button* is what has to move its content.
- **API errors are typed, and a failure is never silent or raw.** `api.js` threw a
  bare `Error` whose message was the **raw response body** and discarded the
  status, so four call sites re-implemented `JSON.parse(err.message).detail` and
  anything that forwarded `err.message` printed FastAPI's JSON braces at the user
  — an ordinary 429 reached the chat bubble as
  `⚠️ {"detail":"Too many requests…"}`. Now one **`ApiError {status, detail}`**,
  parsed once (and tolerant of a non-JSON body from a proxy). A **401 fires a
  single `setUnauthenticatedHandler` hook** — advisory, not authoritative: the
  handler **re-checks `/api/auth/me` before signing anyone out** (that endpoint is
  exempt from the hook so it can't recurse), and a burst of 401s collapses into
  one confirmation. Trusting the first 401 blindly logs a user out on any
  incidental one — it broke ~226 e2e specs when tried, and would have done the
  same to real users. `App.jsx` distinguishes **expired** (401) from
  **unreachable** (anything else) so a transient 500 doesn't read as "you've been
  logged out". User-facing wording lives in the pure `authcopy.js`
  (vitest-pinned, the `announce.js` split). A failed turn now renders as a
  **condition** — `.msg.assistant.failed`, a `--danger` left edge — not as prose
  that happens to start with an emoji. Admin **Usage / Logs / Skills** render a
  real error instead of "Loading…" forever, **"No log records."**, and "No
  lessons yet" — a load failure must never be indistinguishable from an empty
  result (the `deniedError` precedent, generalized). Pinned in
  `frontend/e2e/error-visibility.spec.js` + `authcopy.test.js`.
- **Security headers on every response:** a pure-ASGI `SecurityHeadersMiddleware`
  (`secheaders.py`, outermost so it stamps even the CSRF 403) sets a restrictive
  **CSP** (`script-src 'self'`, no `unsafe-inline`/`unsafe-eval`; `img-src 'self'
  data:` for chart export; `frame-ancestors 'none'`), plus `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`. The CSP is the
  **second line of defense** under the LLM-markdown render surface — that surface is
  safe today only because react-markdown emits no raw HTML (**no `rehype-raw`, default
  URL sanitizer intact — keep it that way**; a DOM-XSS review confirmed it clean).
- **Pre-auth request-body cap:** FastAPI parses a request body while resolving a
  route's parameters — **before** `solve_dependencies` evaluates
  `Depends(require_admin)`. So an *unauthenticated* POST to `/api/admin/import`
  had its whole multipart body parsed and spooled to the temp dir and only then
  got its 401 (measured: 64 MB in 0.17s; a loop fills a container's writable
  layer). `max_upload_mb` lives *inside* that handler, which never runs. A third
  pure-ASGI layer, `BodyLimitMiddleware` (`bodylimit.py`), refuses the body first:
  **`max_request_body_mb` (10 MB) on every request**, with `/api/admin/import`
  exempt up to `max_upload_mb` **only when a session cookie is PRESENT** —
  presence only, no signature check, no DB hit, no second copy of auth. Honest
  limits, stated in the module docstring: one junk `Cookie:` header, or any
  signed-in non-admin, still reaches the large tier. The three layers run
  **SecurityHeaders → CSRF → BodyLimit → router** (last added = outermost), so a
  cross-origin oversized POST is refused by CSRF having read zero bytes and the
  413 still gets its security headers — pinned by
  `test_the_413_carries_the_security_headers`. `MULTIPART_SLACK_MB` (8) keeps the
  *handler* the authoritative decider for a merely-over-cap upload, so
  `test_security.py`'s import-lock-leak assertion keeps its meaning; the
  middleware only catches a grossly oversized body. Pinned in
  `backend/tests/test_bodylimit.py` (the headline case asserts **413, not 401** —
  a 401 would prove the parser ran).
- A denied row records **both** `created_at` (when the request was filed →
  "Requested") **and** `denied_at` (when it was rejected → "Denied", added in
  migration 11) — kept separate so the admin Blocked-users table shows each; a
  pre-migration denial has a NULL `denied_at` (rendered "—").
- A denial is **reversible**. The Allowlist tab lists every active block (the
  "Blocked users" table, grouped **canonically** since a block spans `+tag`
  variants — deliberately unlike the pending list above it, grouped by the **raw**
  address since Approve is exact). Its undo control
  (`DELETE /api/admin/access-requests/{email}/denial`) DELETEs the denied rows
  outright, returning the address to a genuine *never-requested* state — **grants
  no access, sends no email**. **Allowlisting** a denied address also clears the
  block (its `denied` rows convert to `approved`, canonically, so offboarding a
  variant later can't resurrect it), but is the stronger action: it grants full
  access **and** emails a welcome link — not always what undoing a mistaken denial
  calls for.
- **Bulk row-selection + actions** on all three Allowlist tables (Users,
  Pending requests, Blocked users): checkbox column + tri-state page-header
  checkbox + "select all matching" (client-side only — every list is fetched
  unpaginated, so there's nothing to select on an unloaded page). Three
  endpoints — `POST /api/admin/allowlist/bulk-action` (promote/demote/delete),
  `POST /api/admin/access-requests/bulk` (approve/reject),
  `POST /api/admin/access-requests/denial/bulk` (unblock) — each
  transactional (one connection, one commit), capped at `BULK_MAX_ITEMS`
  (1000) records, and **recomputing eligibility per record** server-side
  (never trusting the browser's stale list); a demote/delete batch that
  includes the caller's own email 400s the *whole* batch before any write. An
  id posted to the wrong endpoint (e.g. a denied row's id sent to the
  pending-only bulk endpoint) is recognized as no-longer-eligible and
  skipped, never mutated — the cross-table safety net. Every mutation goes
  through the same helpers the single-row endpoints already called
  (`_set_admin`, `_remove_user`, `_approve_allowlist`, `_deny_group`,
  `_clear_denial_group`), so the single- and bulk-paths can never drift.
  After an action commits the UI **keeps the whole selection** (rows still in
  the table stay checked — `selection.js`'s `retainedSelectionAfterBulk`):
  promote/demote leave every acted row in place so nothing unchecks;
  delete/approve/reject/unblock drop only the ids the server actually processed
  (those rows are gone) while keeping any it skipped/failed, and freeze an
  "all matching" selection to concrete ids so a later-polled row isn't
  silently pre-selected.
  Frontend: `selection.js` (pure counting/copy logic — tri-state derivation,
  eligibility partitioning, every confirm/toast string — vitest-covered),
  `useTableSelection.js` (the per-table selection-state hook; `Allowlist`
  holds three independent instances so selecting on one table never touches
  another), `BulkBar.jsx` (the **contextual** action toolbar rendered through
  `DataTable`'s opt-in `selectable`/`renderSelectionBar` props — following the
  standard Gmail/Linear pattern it appears **only while ≥1 row is selected**
  (never a persistent strip of disabled buttons), anchors a live "N selected"
  count + Clear on the left, shows **stable-verb** action buttons on the right
  (the count lives in the confirm dialog, not the label) with any **destructive**
  action split off past a divider in the `--danger` color, and carries the
  "select all N matching" banner once a full page is selected across more than
  one page; every existing `DataTable` usage that doesn't pass `selectable`
  renders unchanged).
