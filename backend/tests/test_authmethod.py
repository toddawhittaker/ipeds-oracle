"""Which sign-in door a deployment gets, and what it is told when it asked for
another one.

Pure: no DB, no network, no app import. Settings are a SimpleNamespace, the same
way test_mailer.py drives `_resolve_backend` -- these two modules are the same
shape and this suite is the same shape as that one.

The contract that matters most is the DIRECTION of the fallback. Every
misconfiguration must land on magic_link, because magic_link is the only method
whose gate is the manually curated allowlist. A fallback that landed on an
auto-provisioning method would turn a typo into "anyone the provider can
authenticate now has an account".
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.authmethod import (  # noqa: E402
    LDAP,
    MAGIC_LINK,
    OIDC,
    boot_warning,
    fence_warning,
    insecure_ldap_warning,
    ldap_bind_mode,
    ldap_config_problems,
    oidc_config_problems,
    resolve_auth_method,
)


def _s(**kw):
    base = {"auth_method": MAGIC_LINK, "cookie_secure": True,
            "oidc_issuer": "", "oidc_client_id": "",
            "oidc_client_secret": "", "oidc_allowed_domains": "",
            "oidc_required_group": "",
            "ldap_server_uri": "", "ldap_start_tls": False,
            "ldap_allow_insecure": False, "ldap_user_dn_template": "",
            "ldap_bind_dn": "", "ldap_bind_password": "", "ldap_base_dn": "",
            "ldap_user_filter": "(uid={username})",
            "ldap_allowed_domains": "", "ldap_required_group_dn": ""}
    base.update(kw)
    return SimpleNamespace(**base)


def _good_ldap(**kw):
    base = {"auth_method": LDAP, "ldap_server_uri": "ldaps://ldap.example.test",
            "ldap_bind_dn": "cn=svc,dc=x", "ldap_bind_password": "svc-pw",
            "ldap_base_dn": "ou=people,dc=x"}
    base.update(kw)
    return _s(**base)


def _good_oidc(**kw):
    base = {"auth_method": OIDC, "oidc_issuer": "https://idp.example.test",
            "oidc_client_id": "client-1"}
    base.update(kw)
    return _s(**base)


def test_the_default_is_magic_link():
    assert resolve_auth_method(_s()) == MAGIC_LINK
    assert boot_warning(_s()) is None


def test_a_blank_setting_reads_as_magic_link():
    """An operator who comments AUTH_METHOD out gets the default, not an error."""
    assert resolve_auth_method(_s(auth_method="")) == MAGIC_LINK
    assert boot_warning(_s(auth_method="")) is None


def test_case_and_whitespace_are_tolerated():
    assert resolve_auth_method(_good_oidc(auth_method="  OIDC ")) == OIDC


def test_a_fully_configured_oidc_resolves_to_oidc():
    s = _good_oidc()
    assert resolve_auth_method(s) == OIDC
    assert boot_warning(s) is None
    assert oidc_config_problems(s) == []


def test_an_unknown_method_falls_back_and_says_so():
    s = _s(auth_method="saml")
    assert resolve_auth_method(s) == MAGIC_LINK
    msg = boot_warning(s)
    assert msg and "saml" in msg and MAGIC_LINK in msg, msg
    assert OIDC in msg, f"the message should list what IS valid: {msg}"


def test_a_blank_issuer_falls_back_and_names_the_key():
    """The whole cost of this message is its specificity -- the operator reading
    a boot log cannot see the settings object."""
    s = _good_oidc(oidc_issuer="")
    assert resolve_auth_method(s) == MAGIC_LINK
    msg = boot_warning(s)
    assert msg and "OIDC_ISSUER" in msg, msg


def test_a_blank_client_id_falls_back_and_names_the_key():
    s = _good_oidc(oidc_client_id="")
    assert resolve_auth_method(s) == MAGIC_LINK
    assert "OIDC_CLIENT_ID" in (boot_warning(s) or ""), boot_warning(s)


def test_a_plain_http_issuer_is_refused():
    """The id_token's signing keys are fetched from the issuer's origin, so http
    means anyone on the path chooses who you are. Refused even in the dev
    posture, because the host is not loopback."""
    for secure in (True, False):
        s = _good_oidc(oidc_issuer="http://idp.example.test", cookie_secure=secure)
        assert resolve_auth_method(s) == MAGIC_LINK, f"cookie_secure={secure}"
        msg = boot_warning(s)
        assert msg and "https" in msg, msg


def test_a_loopback_http_issuer_is_allowed_only_in_the_dev_posture():
    """The local Keycloak in compose.test.yaml serves plain http on localhost,
    so without this the one provider a developer can actually stand up is the
    one this app refuses. Gated exactly like csrf.py's loopback exception:
    insecure cookies only, which is never a production posture."""
    dev = _good_oidc(oidc_issuer="http://localhost:8081/realms/ipeds-test",
                     cookie_secure=False)
    assert resolve_auth_method(dev) == OIDC, ldap_config_problems(dev) or boot_warning(dev)

    prod = _good_oidc(oidc_issuer="http://localhost:8081/realms/ipeds-test",
                      cookie_secure=True)
    assert resolve_auth_method(prod) == MAGIC_LINK, (
        "a plain-http issuer was accepted with secure cookies")


def test_a_missing_secret_is_not_a_problem():
    """Blank client secret = a public client, which PKCE makes legitimate. A
    resolver that demanded one would refuse a valid configuration."""
    assert resolve_auth_method(_good_oidc(oidc_client_secret="")) == OIDC


def test_resolving_is_silent():
    """It runs on every request that touches a method-gated route. Boot already
    said this once, loudly; saying it again per request would bury the log."""
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
        resolve_auth_method(_s(auth_method="nonsense"))
        resolve_auth_method(_good_oidc(oidc_issuer=""))
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)
    assert records == [], f"resolve_auth_method logged {[r.getMessage() for r in records]}"



# --- LDAP ------------------------------------------------------------------

def test_a_fully_configured_ldap_resolves_to_ldap():
    s = _good_ldap()
    assert resolve_auth_method(s) == LDAP
    assert boot_warning(s) is None
    assert ldap_config_problems(s) == []


def test_the_bind_mode_follows_what_is_configured():
    assert ldap_bind_mode(_good_ldap()) == "search"
    assert ldap_bind_mode(_s(ldap_user_dn_template="uid={username},dc=x")) == "direct"
    assert ldap_bind_mode(_s()) == ""


def test_plain_ldap_without_tls_is_a_config_problem():
    """A simple bind sends the password, so this is refused at RESOLUTION —
    the operator finds out at boot, not from a user who cannot sign in."""
    s = _good_ldap(ldap_server_uri="ldap://ldap.example.test")
    assert resolve_auth_method(s) == MAGIC_LINK
    assert "LDAP_START_TLS" in (boot_warning(s) or ""), boot_warning(s)


def test_plain_ldap_is_allowed_with_starttls_or_the_explicit_opt_out():
    for s in (_good_ldap(ldap_server_uri="ldap://x", ldap_start_tls=True),
              _good_ldap(ldap_server_uri="ldap://x", ldap_allow_insecure=True)):
        assert resolve_auth_method(s) == LDAP, ldap_config_problems(s)


def test_the_insecure_opt_out_still_shouts_at_boot():
    """Reaching this means the operator set it deliberately, and it is still the
    one method where a password crosses the wire."""
    s = _good_ldap(ldap_server_uri="ldap://x", ldap_allow_insecure=True)
    assert "unencrypted" in (insecure_ldap_warning(s) or ""), insecure_ldap_warning(s)
    # Not shouted when the connection is actually protected.
    assert insecure_ldap_warning(
        _good_ldap(ldap_server_uri="ldap://x", ldap_start_tls=True,
                   ldap_allow_insecure=True)) is None
    assert insecure_ldap_warning(_good_ldap()) is None


def test_a_blank_service_password_is_a_config_problem():
    """It fails closed either way (ldap3 refuses an empty simple bind), but
    without this every user is told to check a password that was never the
    problem, and the boot log says nothing at all."""
    s = _good_ldap(ldap_bind_password="")
    assert resolve_auth_method(s) == MAGIC_LINK
    assert "LDAP_BIND_PASSWORD" in (boot_warning(s) or ""), boot_warning(s)


def test_a_template_that_lost_its_placeholder_is_refused():
    """Fail-open in the worst way: every sign-in would bind as ONE fixed DN, so
    whoever knows that one password signs in under any username and inherits
    that entry's email."""
    s = _s(auth_method=LDAP, ldap_server_uri="ldaps://x",
           ldap_user_dn_template="uid=jdoe,ou=people,dc=x")
    assert resolve_auth_method(s) == MAGIC_LINK
    assert "{username}" in (boot_warning(s) or ""), boot_warning(s)

    s2 = _good_ldap(ldap_user_filter="(uid=jdoe)")
    assert resolve_auth_method(s2) == MAGIC_LINK
    assert "LDAP_USER_FILTER" in (boot_warning(s2) or ""), boot_warning(s2)


def test_a_group_fence_without_search_mode_is_refused():
    """A direct bind never reads the entry, so it cannot check a group.
    Silently ignoring the fence would be the worst of the three outcomes."""
    s = _s(auth_method=LDAP, ldap_server_uri="ldaps://x",
           ldap_user_dn_template="uid={username},dc=x",
           ldap_required_group_dn="cn=staff,dc=x")
    assert resolve_auth_method(s) == MAGIC_LINK
    assert "LDAP_REQUIRED_GROUP_DN" in (boot_warning(s) or ""), boot_warning(s)


def test_an_unfenced_auto_provisioning_method_is_called_out():
    assert "NO fence" in (fence_warning(_good_ldap()) or "")
    assert fence_warning(_good_ldap(ldap_required_group_dn="cn=staff,dc=x")) is None
    assert fence_warning(_good_ldap(ldap_allowed_domains="example.edu")) is None
    assert "NO fence" in (fence_warning(
        _s(auth_method=OIDC, oidc_issuer="https://idp.test",
           oidc_client_id="c")) or "")
    # magic_link provisions nobody, so it is never the subject of this warning.
    assert fence_warning(_s()) is None

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
    print("\nsign-in method resolution")
    check("the default is magic_link", test_the_default_is_magic_link)
    check("a blank setting reads as magic_link", test_a_blank_setting_reads_as_magic_link)
    check("case and whitespace are tolerated", test_case_and_whitespace_are_tolerated)
    check("a fully configured oidc resolves to oidc",
          test_a_fully_configured_oidc_resolves_to_oidc)
    check("an unknown method falls back and says so",
          test_an_unknown_method_falls_back_and_says_so)
    check("a blank issuer falls back and names the key",
          test_a_blank_issuer_falls_back_and_names_the_key)
    check("a blank client id falls back and names the key",
          test_a_blank_client_id_falls_back_and_names_the_key)
    check("a plain http issuer is refused", test_a_plain_http_issuer_is_refused)
    check("a loopback http issuer is allowed only in the dev posture",
          test_a_loopback_http_issuer_is_allowed_only_in_the_dev_posture)
    check("a missing secret is not a problem", test_a_missing_secret_is_not_a_problem)
    check("resolving is silent", test_resolving_is_silent)

    print("\nldap")
    check("a fully configured ldap resolves to ldap",
          test_a_fully_configured_ldap_resolves_to_ldap)
    check("the bind mode follows what is configured",
          test_the_bind_mode_follows_what_is_configured)
    check("plain ldap without TLS is a config problem",
          test_plain_ldap_without_tls_is_a_config_problem)
    check("plain ldap is allowed with StartTLS or the explicit opt-out",
          test_plain_ldap_is_allowed_with_starttls_or_the_explicit_opt_out)
    check("the insecure opt-out still shouts at boot",
          test_the_insecure_opt_out_still_shouts_at_boot)
    check("a blank service password is a config problem",
          test_a_blank_service_password_is_a_config_problem)
    check("a template that lost its placeholder is refused",
          test_a_template_that_lost_its_placeholder_is_refused)
    check("a group fence without search mode is refused",
          test_a_group_fence_without_search_mode_is_refused)
    check("an unfenced auto-provisioning method is called out",
          test_an_unfenced_auto_provisioning_method_is_called_out)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL AUTH-METHOD TESTS PASSED")


if __name__ == "__main__":
    run()
