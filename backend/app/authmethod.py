"""Which sign-in door this deployment opens, and whether it is actually usable.

Exactly ONE method is active at a time, named by AUTH_METHOD. This module is the
only thing that decides which, and it follows `mailer._resolve_backend`: a string
setting, a pure resolver, an unknown value degrades to the safest option, and
nothing here ever raises -- a typo in .env must not stop the app booting.

**The fallback is always magic_link, and the direction is the whole point.**
magic_link is the one method that cannot auto-provision: its gate is the manually
curated allowlist, so falling back can only ever NARROW who can get in. Falling
back the other way -- toward a method that creates an account for anyone a
half-configured provider happens to authenticate -- is the failure this ordering
exists to make impossible.

Why a misconfiguration degrades instead of refusing to boot: the app has exactly
one hard refusal (`db.SchemaTooNewError`), and it protects app.db from being
written by a build that cannot understand it. A typo'd issuer URL corrupts
nothing, and a crash-on-boot inside a `restart: unless-stopped` container locks
the operator out of the very admin console they would fix it from. Degrading
leaves them a working magic-link sign-in and a CRITICAL naming the exact keys.

`resolve_auth_method` is deliberately SILENT -- it runs on every request that
touches a method-gated route, so logging there would be a per-request repeat of
something boot already said once, loudly.
"""
from __future__ import annotations

MAGIC_LINK = "magic_link"
OIDC = "oidc"
LDAP = "ldap"
METHODS = (MAGIC_LINK, OIDC, LDAP)


def requested_method(s) -> str:
    """The raw AUTH_METHOD, normalized. May name a method that cannot run --
    `resolve_auth_method` is what decides that. Blank reads as magic_link, so an
    operator who comments the setting out gets the default rather than an error."""
    return (s.auth_method or MAGIC_LINK).strip().lower()


def _secure_issuer(issuer: str, s) -> bool:
    """Imported lazily: app/oidc.py pulls in Authlib and joserfc, and this module
    is deliberately dependency-free so `main` can call it at boot and tests can
    drive it with a SimpleNamespace. The settings object is threaded through for
    the same reason -- so this stays pure over whatever it was handed."""
    from app.oidc import is_secure_url
    return is_secure_url(issuer, s)


def oidc_config_problems(s) -> list[str]:
    """Reasons OIDC cannot run, phrased for an operator reading a boot log.
    Empty list = usable. Pure: no network, no DB, no import of the OIDC stack."""
    problems: list[str] = []
    issuer = s.oidc_issuer.strip()
    if not issuer:
        problems.append("OIDC_ISSUER is blank")
    elif not _secure_issuer(issuer, s):
        # Not pedantry: the id_token's signing keys are fetched from this origin,
        # so plain http means anyone on the path chooses who you are. A LOOPBACK
        # http issuer is allowed in the dev posture only (insecure cookies) --
        # see oidc.is_secure_url, which this defers to so the boot check and the
        # sign-in path can never disagree about what is acceptable.
        problems.append(f"OIDC_ISSUER must start with https:// (got {issuer!r})")
    if not s.oidc_client_id.strip():
        problems.append("OIDC_CLIENT_ID is blank")
    return problems


def ldap_bind_mode(s) -> str:
    """"direct", "search", or "" when neither is configured.

    Direct bind needs only a DN template and no service account, which suits a
    simple tree. Search-then-bind is what Active Directory deployments actually
    run (a username is not a DN there) and is the only mode that can read a
    group, so the group fence requires it. Pure."""
    if s.ldap_user_dn_template.strip():
        return "direct"
    if s.ldap_bind_dn.strip() and s.ldap_base_dn.strip():
        return "search"
    return ""


def ldap_config_problems(s) -> list[str]:
    """Reasons LDAP cannot run, phrased for an operator reading a boot log.
    Empty list = usable. Pure: no network, no DB, no import of ldap3."""
    problems: list[str] = []
    uri = s.ldap_server_uri.strip()
    if not uri:
        problems.append("LDAP_SERVER_URI is blank")
    elif not uri.lower().startswith(("ldap://", "ldaps://")):
        problems.append(f"LDAP_SERVER_URI must start with ldaps:// or ldap:// (got {uri!r})")
    elif uri.lower().startswith("ldap://") and not (
            s.ldap_start_tls or s.ldap_allow_insecure):
        # A simple bind sends the password. Refusing here rather than at sign-in
        # means the operator finds out at boot, not from a user who cannot log in.
        problems.append("LDAP_SERVER_URI is plain ldap:// — set LDAP_START_TLS=true "
                        "(or LDAPS), since a simple bind sends the user's password")
    mode = ldap_bind_mode(s)
    if not mode:
        problems.append("neither LDAP_USER_DN_TEMPLATE (direct bind) nor "
                        "LDAP_BIND_DN + LDAP_BASE_DN (search-then-bind) is set")
    elif mode == "search" and not s.ldap_bind_password:
        # Fails closed either way (ldap3 refuses an empty simple bind), but
        # without this the symptom is every user being told to check a password
        # that was never the problem, and a boot log that says nothing.
        problems.append("LDAP_BIND_DN is set but LDAP_BIND_PASSWORD is blank")
    # A template that lost its placeholder binds EVERY sign-in as one fixed DN,
    # so whoever knows that one password signs in under any username and gets
    # that entry's email. Fail-open in the worst way, and invisible.
    if mode == "direct" and "{username}" not in s.ldap_user_dn_template:
        problems.append("LDAP_USER_DN_TEMPLATE has no {username} placeholder")
    if mode == "search" and "{username}" not in s.ldap_user_filter:
        problems.append("LDAP_USER_FILTER has no {username} placeholder")
    if s.ldap_required_group_dn.strip() and ldap_bind_mode(s) == "direct":
        # Direct bind never reads the entry's attributes, so it cannot check a
        # group. Silently ignoring the fence would be the worst outcome.
        # Names the key to CLEAR, not the ones to set: a deployment with all
        # four set has already set LDAP_BIND_DN and LDAP_BASE_DN, and being told
        # to set them again is a dead end -- the template is what wins the mode
        # check, so unsetting it is the fix.
        problems.append("LDAP_REQUIRED_GROUP_DN needs search-then-bind, but "
                        "LDAP_USER_DN_TEMPLATE is set and selects direct bind, "
                        "which cannot read groups — clear LDAP_USER_DN_TEMPLATE "
                        "to use LDAP_BIND_DN + LDAP_BASE_DN instead")
    return problems


def config_problems(s, method: str) -> list[str]:
    """Reasons `method` cannot run. magic_link needs no configuration at all,
    which is exactly why it is the fallback."""
    if method == OIDC:
        return oidc_config_problems(s)
    if method == LDAP:
        return ldap_config_problems(s)
    return []


def resolve_auth_method(s) -> str:
    """The method actually in force. Silent by contract -- see the module note."""
    method = requested_method(s)
    if method not in METHODS:
        return MAGIC_LINK
    if config_problems(s, method):
        return MAGIC_LINK
    return method


def fence_warning(s) -> str | None:
    """A CRITICAL for an auto-provisioning method with no fence configured.

    Not a config PROBLEM -- it does not fall back, because "everyone my provider
    authenticates" is a legitimate choice for a single-tenant issuer or a staff
    directory, and is the model the operator opted into. But it is the
    difference between "our staff" and a whole shared tenant or a whole campus,
    so it must not be a silent default.
    """
    method = resolve_auth_method(s)
    if method == OIDC and not (s.oidc_allowed_domains.strip()
                               or s.oidc_required_group.strip()):
        return ("AUTH_METHOD=oidc with NO fence: neither OIDC_ALLOWED_DOMAINS nor "
                "OIDC_REQUIRED_GROUP is set, so anyone your provider can authenticate "
                "gets an account here on first sign-in. Set one of them unless your "
                "issuer serves only people who should have access.")
    if method == LDAP and not (s.ldap_required_group_dn.strip()
                               or s.ldap_allowed_domains.strip()):
        return ("AUTH_METHOD=ldap with NO fence: neither LDAP_REQUIRED_GROUP_DN nor "
                "LDAP_ALLOWED_DOMAINS is set, so anyone who can bind to the "
                "directory gets an account here on first sign-in — students and "
                "former staff included, if the directory holds them. Set one of "
                "them unless every account in the directory should have access.")
    return None


def insecure_ldap_warning(s) -> str | None:
    """A CRITICAL for a directory reached over plain, unencrypted LDAP.

    `ldap_config_problems` already refuses that combination, so reaching this
    means the operator set `LDAP_ALLOW_INSECURE` deliberately. It is still the
    one method where a password crosses the wire, so the escape hatch says so on
    every boot rather than being a line in a .env nobody re-reads."""
    if resolve_auth_method(s) != LDAP:
        return None
    if not s.ldap_allow_insecure:
        return None
    if s.ldap_server_uri.strip().lower().startswith("ldaps://") or s.ldap_start_tls:
        return None
    return ("LDAP_ALLOW_INSECURE is set and the directory is plain ldap:// — every "
            "sign-in sends the user's password unencrypted. Use ldaps:// or "
            "LDAP_START_TLS=true for anything but a local test directory.")


def boot_warning(s) -> str | None:
    """The CRITICAL line for a deployment that asked for a method it will not
    get, or None when the configured method is the one in force.

    It names the missing keys rather than saying "misconfigured", because the
    operator reading this cannot see the settings object and the whole cost of
    the message is in that specificity."""
    method = requested_method(s)
    if method not in METHODS:
        return (f"UNKNOWN AUTH_METHOD={method!r} — falling back to {MAGIC_LINK}. "
                f"Valid values: {', '.join(METHODS)}.")
    problems = config_problems(s, method)
    if problems:
        return (f"AUTH_METHOD={method} is set but not usable: {'; '.join(problems)} — "
                f"falling back to {MAGIC_LINK}. Fix those settings, or set "
                f"AUTH_METHOD={MAGIC_LINK} to make the fallback deliberate.")
    return None
