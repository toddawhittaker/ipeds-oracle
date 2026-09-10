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
    MAGIC_LINK,
    OIDC,
    boot_warning,
    oidc_config_problems,
    resolve_auth_method,
)


def _s(**kw):
    base = {"auth_method": MAGIC_LINK, "oidc_issuer": "", "oidc_client_id": "",
            "oidc_client_secret": ""}
    base.update(kw)
    return SimpleNamespace(**base)


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
    means anyone on the path chooses who you are."""
    s = _good_oidc(oidc_issuer="http://idp.example.test")
    assert resolve_auth_method(s) == MAGIC_LINK
    msg = boot_warning(s)
    assert msg and "https" in msg, msg


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
    check("a missing secret is not a problem", test_a_missing_secret_is_not_a_problem)
    check("resolving is silent", test_resolving_is_silent)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL AUTH-METHOD TESTS PASSED")


if __name__ == "__main__":
    run()
