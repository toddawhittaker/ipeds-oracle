"""The one seam every sign-in method converges on.

PR 0 of the OIDC/LDAP work lifted `create_session` and `set_session_cookie` out
of `verify_login` without changing what any of it does. These cases pin the
invariants that extraction has to preserve -- and that the methods stacked on
top of it must not quietly break:

  * exactly one session row per successful sign-in, and exactly ONE place in
    backend/app that writes one at all (the source scan below is what stops a
    later method pasting its own minter);
  * the cookie's attributes, including the `samesite=lax` an external identity
    provider's cross-site redirect back depends on -- tightening it to `strict`
    would land the user signed out with nothing to read;
  * `create_session` joins the caller's transaction instead of committing, so a
    session can never outlive a sign-in step that failed after it;
  * a session on its own is NOT access. The allowlist is re-checked on every
    request, which is precisely the invariant auto-provisioning has to satisfy
    by WRITING an allowlist row rather than by relaxing that check.
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isolate app.db before importing settings (get_settings is lru_cached)
tmp = tempfile.mkdtemp()
os.environ["APP_DB_PATH"] = str(Path(tmp) / "app.db")
os.environ["ADMIN_EMAILS"] = "admin@example.edu"
os.environ["COOKIE_SECURE"] = "false"
os.environ["EMAIL_DOMAIN"] = ""
os.environ["AUTH_RATE_MAX_PER_EMAIL"] = "1000"
os.environ["AUTH_RATE_MAX_PER_IP"] = "1000"
os.environ["CHAT_RATE_MAX_PER_USER"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from app import mailer  # noqa: E402

captured: dict = {}
mailer.send_magic_link = lambda to, link: captured.__setitem__("link", link) or True
mailer.send_access_request = lambda *a, **k: True
mailer.send_access_approved = lambda to: captured.__setitem__("approved", to) or True

from app import auth  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connect  # noqa: E402
from app.main import app  # noqa: E402

APP_DIR = Path(__file__).resolve().parents[1] / "app"


def _session_count() -> int:
    con = connect()
    try:
        return con.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
    finally:
        con.close()


def _sign_in(c: TestClient, email: str):
    r = c.post("/api/auth/request", json={"email": email})
    assert r.status_code == 200, r.text
    token = captured["link"].split("token=")[1]
    v = c.post("/api/auth/verify", json={"token": token})
    assert v.status_code == 200, v.text
    return v


def test_magic_link_sign_in_mints_exactly_one_session_row():
    with TestClient(app) as c:
        before = _session_count()
        _sign_in(c, "admin@example.edu")
        after = _session_count()
    assert after == before + 1, f"expected exactly one new session row, got {after - before}"


def test_the_session_cookie_keeps_its_attributes():
    with TestClient(app) as c:
        v = _sign_in(c, "admin@example.edu")
    raw = v.headers["set-cookie"]
    low = raw.lower()
    s = get_settings()
    assert "httponly" in low, raw
    assert "path=/" in low, raw
    assert f"max-age={s.session_ttl_days * 86400}" in low, raw
    # Not a default worth tightening: `strict` drops the cookie on the
    # cross-site top-level GET an identity provider redirects back with.
    assert "samesite=lax" in low, f"samesite must stay lax: {raw}"
    assert ("secure" in low) == bool(s.cookie_secure), raw


def test_create_session_does_not_commit_on_its_own():
    before = _session_count()
    con = connect()
    try:
        auth.create_session(con, "rollback@example.edu")
        con.rollback()
    finally:
        con.close()
    assert _session_count() == before, (
        "create_session committed by itself -- a sign-in that fails AFTER minting "
        "would leave a usable session behind (magic link marks its token used in "
        "the same transaction)")


# Every `set_cookie` call in backend/app, as (module, what it names). Keyed on
# the ARGUMENT, not just the file: a per-file allowlist passes when a module
# already on the list quietly adds a second call that sets the SESSION cookie,
# which is precisely the regression this exists to catch (verified -- the
# file-level version did not catch it).
EXPECTED_COOKIE_SETTERS = [
    ("auth.py", "s.cookie_name"),        # the session itself
    ("oidc.py", "state_cookie_name()"),  # the short-lived OIDC state binding
]


def _cookie_setters() -> list[tuple[str, str]]:
    found = []
    for path in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "set_cookie"
                    and node.args):
                found.append((str(path.relative_to(APP_DIR)),
                              ast.unparse(node.args[0])))
    return sorted(found)


def test_only_one_place_in_the_app_mints_a_session():
    """The regression this catches is a SECOND session minter.

    Every new sign-in method is a fresh temptation to write its own INSERT and
    its own set_cookie -- at which point the cookie flags, the TTL and the user
    upsert exist twice and drift, which is exactly how the persisted-answer
    field list rotted.

    A sign-in method may legitimately need a cookie of its OWN (OIDC binds its
    `state` to the browser that started the login), so this pins WHICH cookie
    each call sets rather than merely how many calls there are.
    """
    insert_files, n_insert = [], 0
    for path in sorted(APP_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "INSERT INTO sessions" in text:
            insert_files.append(str(path.relative_to(APP_DIR)))
            n_insert += text.count("INSERT INTO sessions")
    assert insert_files == ["auth.py"], f"session INSERT escaped auth.py: {insert_files}"
    assert n_insert == 1, f"expected one session INSERT, found {n_insert}"

    setters = _cookie_setters()
    assert setters == sorted(EXPECTED_COOKIE_SETTERS), (
        f"cookie setters changed.\n  found:    {setters}\n  expected: "
        f"{sorted(EXPECTED_COOKIE_SETTERS)}\nA new entry needs a reason in "
        f"EXPECTED_COOKIE_SETTERS; anything setting the SESSION cookie belongs "
        f"in auth.set_session_cookie.")


def test_a_minted_session_is_still_gated_by_the_allowlist():
    """A session is not access on its own.

    `_user_from_request` re-checks the allowlist on EVERY request, and that is
    the app's only per-request kill switch. Auto-provisioning must therefore
    satisfy this check by writing an allowlist row -- never by making the check
    method-aware, which would strand a removed user's 30-day cookie.
    """
    con = connect()
    try:
        sess, _user = auth.create_session(con, "stranger@example.edu")
        con.commit()
    finally:
        con.close()
    with TestClient(app) as c:
        c.cookies.set(get_settings().cookie_name, sess)
        r = c.get("/api/auth/me")
    assert r.status_code == 401, (
        f"a session for a non-allowlisted address must not authenticate: {r.status_code}")


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
    print("\n1. the magic-link path is unchanged")
    check("sign-in mints exactly one session row",
          test_magic_link_sign_in_mints_exactly_one_session_row)
    check("the session cookie keeps its attributes",
          test_the_session_cookie_keeps_its_attributes)

    print("\n2. the seam's contract")
    check("create_session does not commit on its own",
          test_create_session_does_not_commit_on_its_own)
    check("only one place in backend/app mints a session",
          test_only_one_place_in_the_app_mints_a_session)
    check("a minted session is still gated by the allowlist",
          test_a_minted_session_is_still_gated_by_the_allowlist)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL SESSION SEAM TESTS PASSED")


if __name__ == "__main__":
    run()
