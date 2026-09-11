"""Directory sign-in: bind a username and password against LDAP.

Named `ldapauth`, not `ldap`, so a module in this package never shadows the
third-party library it imports.

**This is the one method where a password crosses the wire**, and everything
below follows from that. `ldap3` is the client -- pure Python, so the image
needs no apt package (python-ldap and bonsai both need C libraries).

The ORDER in `authenticate` is the security property, not an implementation
detail:

1. **Refuse an empty password before touching the directory.** RFC 4513 says a
   simple bind with a zero-length password is an ANONYMOUS bind, which most
   servers answer with success -- "any username, no password" as a sign-in.
   ldap3 happens to refuse it itself (`LDAPPasswordIsMandatoryError`), so in
   THIS codebase the bug it prevents is an uncaught 500 where every other
   rejection is a neutral 401 -- an enumeration oracle wearing a different hat.
   Checking here rather than relying on the library also means the guard is ours
   to keep if ldap3 ever relaxes.
2. **TLS, always verified.** ⚠ `ldap3.Tls()` defaults to `validate=CERT_NONE`,
   so an `ldaps://` URI with a default Tls object accepts ANY certificate -- on
   the one connection carrying a plaintext password. `_tls` never does that.
3. **Escape the username for the syntax it is going into, and they are two
   different syntaxes.** A search filter needs `escape_filter_chars` -- unescaped,
   `*` matches every entry and `)(uid=admin` rewrites the query. A DN needs
   `escape_rdn` (RFC 4514), which escapes `,` `=` `+` `\\` and friends. Filter
   escaping applied to a DN is NOT a substitute and leaves both `,` and `=`
   alone: with it, the direct-bind username `jdoe,ou=admins` turns
   `uid={username},ou=people,dc=x` into `uid=jdoe,ou=admins,ou=people,dc=x`,
   aiming the bind at a different subtree.
4. **Exactly one search result.** Zero refuses; two or more refuses loudly,
   because an ambiguous filter is a configuration error and picking one is how
   somebody signs in as the wrong person.
5. **Rebind as the found DN on the SAME connection.** A second connection costs
   an extra TCP connect and TLS handshake, and only when the username EXISTS --
   tens of milliseconds of wall clock that turn the deliberately identical 401
   into a username oracle. The search is already done by then, so demoting the
   connection from the service account to the user costs nothing, and it is
   unbound in a `finally` either way.

Every rejection -- wrong password, unknown user, no group, unreachable server --
raises the same `LdapError`, and the route turns all of them into one identical
401. Anything else is a directory enumeration oracle. The reason is logged
server-side, where only an admin sees it.

The test seam is `connect`: `authenticate` takes a factory, defaulting to the
real one, the same injectable idiom `nces.py` uses for its httpx client.
`backend/tests/test_ldap.py` passes ldap3's own MOCK_SYNC strategy, which is a
real in-process ldap3 server -- binds succeed or fail for real -- plus a spy on
`_server` for the TLS posture, which MOCK_SYNC cannot express.
"""
from __future__ import annotations

import logging
import ssl

import ldap3
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars
from ldap3.utils.dn import escape_rdn

from app.authmethod import ldap_bind_mode
from app.config import _log_safe, get_settings

log = logging.getLogger("ipeds.ldap")


class LdapError(Exception):
    """A sign-in that cannot proceed. The reason is for the log, never for the
    caller -- see the module note on the neutral 401."""


def _tls(s) -> ldap3.Tls:
    """A TLS context that actually verifies the certificate.

    ldap3's `Tls()` default is `validate=ssl.CERT_NONE`. An `ldaps://` URI built
    with that default encrypts the password and then hands it to anyone willing
    to present a self-signed certificate, which is the whole attack it looks
    like it prevents. Constructed explicitly, every time."""
    return ldap3.Tls(validate=ssl.CERT_REQUIRED,
                     ca_certs_file=s.ldap_tls_ca_certs_file.strip() or None,
                     version=ssl.PROTOCOL_TLS_CLIENT)


def _timeout(s) -> int:
    """ldap3 wants an INTEGER, and will not say so politely.

    `ldap_timeout_seconds` is a float, like its `oidc_http_timeout_seconds`
    sibling. Handing that float to ldap3 raises `error: required argument is not
    an integer` the moment it opens a real socket -- which `authenticate` then
    converts into "the directory could not be reached", so every sign-in on
    every real directory failed with a neutral 401 and a misleading log line.

    Nothing in the test suite could catch it: MOCK_SYNC never opens a socket, so
    the value is never used. It took running the thing against a real server.
    Floored at 1 so a sub-second setting cannot round down to 0, which ldap3
    reads as "no timeout".
    """
    return max(1, round(s.ldap_timeout_seconds))


def _server(s) -> ldap3.Server:
    """The directory, as ldap3 sees it. A module-level function so a test can
    substitute it and inspect the Tls object it was handed -- the only way to
    pin the CERT_NONE regression, which MOCK_SYNC cannot express.

    `get_info=NONE` skips a schema round trip on every single sign-in."""
    uri = s.ldap_server_uri.strip()
    use_ssl = uri.lower().startswith("ldaps://")
    return ldap3.Server(uri, use_ssl=use_ssl, tls=_tls(s),
                        connect_timeout=_timeout(s),
                        # Belt and braces with auto_referrals=False: even if a
                        # referral were followed, no host is trusted with this
                        # connection's credentials. ldap3's default here is
                        # [('*', True)] -- ANY host, send the password.
                        allowed_referral_hosts=[],
                        get_info=ldap3.NONE)


def _connection(server, *, user=None, password=None, **kw) -> ldap3.Connection:
    """THE injection point. `raise_exceptions=False` because a failed bind is a
    NORMAL outcome here and must be a boolean, not an exception.

    ⚠ `auto_referrals=False` is the load-bearing argument, and ldap3's default is
    True. With referrals followed, `create_referral_connection` opens a
    connection to whatever host a referral names, COPIES this connection's user
    and password into it, and binds -- so a referral is (a) an exfiltration
    channel for LDAP_BIND_PASSWORD, over plaintext, since the referral
    connection is built with TLS only if the referral URL says `ldaps`, and
    (b) a way to substitute the search RESULT: the referred server's answer
    replaces the original, so an attacker able to create one referral object
    inside LDAP_BASE_DN can return `mail: admin@example.edu` and a satisfying
    `memberOf` while the password check still runs against the real directory
    as their own account. ldap3 refuses to follow a referral on a BIND, which is
    why the search is the reachable half.

    `receive_timeout` bounds each recv, so a directory that dribbles bytes
    cannot hold one of the app's 40 shared threadpool slots indefinitely.
    """
    return ldap3.Connection(server, user=user, password=password,
                            raise_exceptions=False, auto_bind=False,
                            auto_referrals=False,
                            receive_timeout=_timeout(get_settings()),
                            **kw)


def _user_filter(s, username: str) -> str:
    """The search filter, with the username escaped at the ONE place it is
    interpolated. `escape_filter_chars` turns `*` into `\\2a` and `(`/`)` into
    their escapes, so a username cannot widen or rewrite the query."""
    return s.ldap_user_filter.replace("{username}", escape_filter_chars(username))


def _start_tls_if_needed(s, conn) -> None:
    if s.ldap_server_uri.strip().lower().startswith("ldaps://"):
        return
    if not s.ldap_start_tls:
        # Only reachable with LDAP_ALLOW_INSECURE, which authmethod refuses to
        # resolve without and shouts about at boot.
        return
    # ldap3 RAISES LDAPStartTLSError on a refusal rather than returning False --
    # `raise_exceptions=False` does not suppress it, and the only False returns
    # are "already established" and "operations in progress", neither reachable
    # here. Without the raise arm this branch could never produce its own
    # message, and a refusing directory gave the operator a traceback instead.
    if not conn.start_tls():
        raise LdapError(f"StartTLS refused: {conn.result}")


def _attr(entry, attribute: str):
    """Look an attribute up the way LDAP names work: case-insensitively.

    `entry_attributes_as_dict` is keyed by the case the SERVER returned, not the
    case we asked for. So `LDAP_GROUP_MEMBER_ATTRIBUTE=memberof` -- the spelling
    most Active Directory documentation uses -- against a server answering
    `memberOf` silently yields nothing, and the group fence then refuses
    everybody. Fail-closed, but indistinguishable from a real refusal."""
    if attribute in entry:
        return entry[attribute]
    wanted = attribute.lower()
    for key, value in entry.items():
        if key.lower() == wanted:
            return value
    return None


def _single_value(entry, attribute: str) -> str:
    """The ONE value of `attribute`, or "" when there is not exactly one.

    A multi-valued attribute is refused rather than resolved. LDAP attribute
    sets have no defined order, so `raw[0]` is whatever the server happened to
    send first -- an entry with two `mail` values would land in either of two
    app accounts run to run, and if one of them is an ADMIN_EMAILS address that
    is an admin account. Refusing is the same call the one-result rule makes
    about an ambiguous search."""
    raw = _attr(entry, attribute)
    if isinstance(raw, list):
        return str(raw[0]) if len(raw) == 1 else ""
    return str(raw or "")


def _values(entry, attribute: str) -> list[str]:
    raw = _attr(entry, attribute)
    if isinstance(raw, list):
        return [str(v) for v in raw]
    return [str(raw)] if raw else []


def _check_group(s, entry) -> None:
    required = s.ldap_required_group_dn.strip()
    if not required:
        return
    # DNs are case-insensitive and vary in spacing between directories, so
    # compare normalised rather than byte-for-byte.
    def norm(dn: str) -> str:
        return ",".join(part.strip() for part in dn.lower().split(","))

    if norm(required) not in {norm(g) for g in _values(entry, s.ldap_group_member_attribute)}:
        raise LdapError(f"not a member of {required!r}")


def authenticate(username: str, password: str, *, connect=None) -> str:
    """Bind `username`/`password` and return the verified email address.

    Raises `LdapError` for every rejection, with the reason for the log only.
    """
    s = get_settings()
    connect = connect or _connection
    username = username.strip()
    if not username:
        raise LdapError("empty username")
    # See the module docstring, point 1. Before ANY connection.
    if not password or not password.strip():
        raise LdapError("empty password (an anonymous bind is not a sign-in)")

    mode = ldap_bind_mode(s)
    try:
        return _bind_and_read(s, mode, username, password, connect)
    except LdapError:
        raise
    except LDAPException as e:
        # Everything ldap3 can throw becomes one outcome, because the caller's
        # contract is "LdapError or an email". An unreachable directory and a
        # certificate that does not verify both land here -- and the second is
        # the likeliest first-day failure of this feature, when the operator has
        # not yet pointed LDAP_TLS_CA_CERTS_FILE at their internal CA. Without
        # this the route's catch-all is the only thing making the app behave as
        # documented, and any future caller would get an unhandled exception.
        raise LdapError(f"the directory could not be reached: {e}") from e


def _bind_and_read(s, mode: str, username: str, password: str, connect) -> str:
    server = _server(s)

    if mode == "direct":
        # escape_rdn, NOT escape_filter_chars: this is a DN, and filter escaping
        # leaves `,` and `=` untouched -- see point 3 of the module docstring for
        # the subtree-redirection that allows.
        user_dn = s.ldap_user_dn_template.replace("{username}", escape_rdn(username))
        conn = connect(server, user=user_dn, password=password)
        try:
            _start_tls_if_needed(s, conn)
            if not conn.bind():
                raise LdapError(f"bind failed for {user_dn!r}: {conn.result}")
            # Read the entry back over the user's own connection.
            if not conn.search(user_dn, "(objectClass=*)", search_scope=ldap3.BASE,
                               attributes=[s.ldap_email_attribute,
                                           s.ldap_group_member_attribute]):
                raise LdapError(f"could not read {user_dn!r} after binding")
            entry = conn.entries[0].entry_attributes_as_dict if conn.entries else {}
        finally:
            conn.unbind()
    else:
        entry, user_dn = _search_then_bind(s, server, username, password, connect)

    _check_group(s, entry)
    email = _single_value(entry, s.ldap_email_attribute).strip().lower()
    if not email or "@" not in email:
        raise LdapError(
            f"{user_dn!r} has no single usable {s.ldap_email_attribute!r} attribute")
    domains = s.ldap_allowed_domain_list
    if domains and email.rsplit("@", 1)[-1] not in domains:
        raise LdapError(f"{email} is outside LDAP_ALLOWED_DOMAINS")
    return email


def _search_then_bind(s, server, username: str, password: str, connect):
    """Find the user with the service account, then bind AS them on a separate
    connection -- so a failed user bind cannot leave the service connection in a
    half-bound state that a later call would reuse."""
    finder = connect(server, user=s.ldap_bind_dn.strip(),
                     password=s.ldap_bind_password)
    try:
        _start_tls_if_needed(s, finder)
        if not finder.bind():
            raise LdapError(f"the service account could not bind: {finder.result}")
        found = finder.search(s.ldap_base_dn.strip(), _user_filter(s, username),
                              attributes=[s.ldap_email_attribute,
                                          s.ldap_group_member_attribute],
                              # Two is all the ">1" check below needs, and it
                              # stops a broad filter materialising the directory
                              # into memory before the check ever runs.
                              size_limit=2)
        entries = list(finder.entries) if found else []
        if not entries:
            raise LdapError(f"no directory entry for {_log_safe(username)!r}")
        if len(entries) > 1:
            # Loudly: an ambiguous filter is a configuration error, and picking
            # one entry is how somebody signs in as the wrong person.
            raise LdapError(
                f"{len(entries)} entries match {_log_safe(username)!r} — "
                f"LDAP_USER_FILTER is too broad")
        user_dn = entries[0].entry_dn
        attrs = entries[0].entry_attributes_as_dict
        # REBIND on the same connection rather than opening a second one. A
        # second connection costs an extra TCP connect and TLS handshake, which
        # happens only when the username EXISTS -- a wall-clock difference of
        # tens of milliseconds that turns the deliberately-neutral 401 into a
        # username oracle. The search is already done, so demoting this
        # connection from the service account to the user costs nothing, and it
        # is unbound in the `finally` either way.
        if not finder.rebind(user=user_dn, password=password):
            raise LdapError(f"bind failed for {user_dn!r}: {finder.result}")
    finally:
        finder.unbind()
    return attrs, user_dn
