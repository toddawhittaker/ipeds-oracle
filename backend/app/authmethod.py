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
METHODS = (MAGIC_LINK, OIDC)


def requested_method(s) -> str:
    """The raw AUTH_METHOD, normalized. May name a method that cannot run --
    `resolve_auth_method` is what decides that. Blank reads as magic_link, so an
    operator who comments the setting out gets the default rather than an error."""
    return (s.auth_method or MAGIC_LINK).strip().lower()


def oidc_config_problems(s) -> list[str]:
    """Reasons OIDC cannot run, phrased for an operator reading a boot log.
    Empty list = usable. Pure: no network, no DB, no import of the OIDC stack."""
    problems: list[str] = []
    issuer = s.oidc_issuer.strip()
    if not issuer:
        problems.append("OIDC_ISSUER is blank")
    elif not issuer.lower().startswith("https://"):
        # Not pedantry: the id_token's signing keys are fetched from this origin,
        # so plain http means anyone on the path chooses who you are.
        problems.append(f"OIDC_ISSUER must start with https:// (got {issuer!r})")
    if not s.oidc_client_id.strip():
        problems.append("OIDC_CLIENT_ID is blank")
    return problems


def config_problems(s, method: str) -> list[str]:
    """Reasons `method` cannot run. magic_link needs no configuration at all,
    which is exactly why it is the fallback."""
    if method == OIDC:
        return oidc_config_problems(s)
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
    """A CRITICAL for an OIDC deployment with no domain and no group fence.

    Not a config PROBLEM -- it does not fall back, because "everyone my provider
    authenticates" is a legitimate choice for a single-tenant issuer and is the
    model the operator opted into. But it is the difference between "our staff"
    and a whole shared tenant, and pointed at a consumer issuer it is the
    internet, so it must not be a silent default.
    """
    if resolve_auth_method(s) != OIDC:
        return None
    if s.oidc_allowed_domains.strip() or s.oidc_required_group.strip():
        return None
    return ("AUTH_METHOD=oidc with NO fence: neither OIDC_ALLOWED_DOMAINS nor "
            "OIDC_REQUIRED_GROUP is set, so anyone your provider can authenticate "
            "gets an account here on first sign-in. Set one of them unless your "
            "issuer serves only people who should have access.")


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
