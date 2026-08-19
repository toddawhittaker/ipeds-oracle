"""API-key contract (backend/app/apikeys.py + routers/keys.py + the admin half).

Every check below names a regression that has a plausible way of happening:

  * the raw key leaking into storage or into a later read — the whole point of
    hashing is undone by one convenience field;
  * a key outliving its owner's allowlist entry — the failure mode is a standing
    credential for someone an admin believes they removed, and it is invisible
    because the browser session DOES stop working;
  * one user reaching another's keys through a guessable integer id;
  * a non-admin reaching the admin half;
  * last_used_at writing on every request, which would put a write on app.db in
    front of every MCP call.

Uses the standalone-script style the rest of backend/tests/ uses: env before
import, a check() helper, non-zero exit on any failure.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

tmp = tempfile.mkdtemp()
os.environ["APP_DB_PATH"] = str(Path(tmp) / "app.db")
os.environ["ADMIN_EMAILS"] = "admin@example.edu"
os.environ["COOKIE_SECURE"] = "false"
os.environ["LLM_API_KEY"] = ""
os.environ["RESEND_API_KEY"] = ""
# This suite signs in repeatedly; keep the auth limiter from masking a real
# assertion behind a 429.
os.environ["AUTH_RATE_MAX_PER_EMAIL"] = "1000"
os.environ["AUTH_RATE_MAX_PER_IP"] = "1000"

from fastapi.testclient import TestClient  # noqa: E402

from app import mailer  # noqa: E402

captured = {}
mailer.send_magic_link = lambda to, link: captured.__setitem__("link", link) or True
mailer.send_access_request = lambda *a, **k: True
mailer.send_access_approved = lambda to: captured.__setitem__("approved", to) or True

from app import apikeys  # noqa: E402
from app.db import connect  # noqa: E402
from app.main import app  # noqa: E402
from app.security import hash_token  # noqa: E402

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


def sign_in(c, email):
    """Complete a magic-link round trip, leaving `c` authenticated as `email`."""
    r = c.post("/api/auth/request", json={"email": email})
    assert r.status_code == 200, r.text
    token = captured["link"].split("token=")[1]
    v = c.post("/api/auth/verify", json={"token": token})
    assert v.status_code == 200, v.text


def allow(c, email, is_admin=False):
    """Allowlist `email` as the signed-in admin, then sign in as them."""
    r = c.post("/api/admin/allowlist",
               json={"email": email, "is_admin": is_admin})
    assert r.status_code == 200, r.text


def run():
    with TestClient(app) as c:
        sign_in(c, "admin@example.edu")
        allow(c, "user@example.edu")
        allow(c, "other@example.edu")

        # --- minting and the one-shot reveal ---------------------------------
        r = c.post("/api/keys", json={"label": "laptop"})
        assert r.status_code == 200, r.text
        minted = r.json()
        raw = minted["key"]

        def raw_key_is_shaped_and_returned_once():
            assert raw.startswith(apikeys.KEY_PREFIX), raw
            assert len(raw) > len(apikeys.KEY_PREFIX) + 20, "key body too short"
            listed = c.get("/api/keys").json()
            assert len(listed) == 1, listed
            blob = repr(listed)
            assert raw not in blob, "a later read returned the raw key"
            assert "key_hash" not in blob, "the list leaked the hash column"
            assert listed[0]["last4"] == raw[-4:], listed[0]
        check("the raw key is returned once and never by a later read",
              raw_key_is_shaped_and_returned_once)

        def storage_holds_only_the_hash():
            con = connect()
            try:
                row = con.execute(
                    "SELECT key_hash, last4 FROM api_keys WHERE id=?",
                    (minted["id"],)).fetchone()
            finally:
                con.close()
            assert row["key_hash"] == hash_token(raw), "stored hash is not the key's"
            assert raw not in row["key_hash"], "the raw key is inside the stored value"
            assert row["last4"] == raw[-4:], row["last4"]
        check("only the hash is stored, never the raw key",
              storage_holds_only_the_hash)

        def verify_accepts_a_live_key():
            row = apikeys.verify(raw)
            assert row is not None, "a freshly minted key did not verify"
            assert row["email"] == "admin@example.edu", dict(row)
        check("verify accepts a live key and returns its owner",
              verify_accepts_a_live_key)

        def verify_rejects_a_bad_key():
            assert apikeys.verify("") is None
            assert apikeys.verify(apikeys.KEY_PREFIX + "not-a-real-key") is None
        check("verify rejects an empty or unknown key", verify_rejects_a_bad_key)

        # --- last_used_at is rate-limited, not per-request -------------------
        def touch_does_not_write_on_every_call():
            apikeys.touch(minted["id"])
            con = connect()
            try:
                first = con.execute("SELECT last_used_at FROM api_keys WHERE id=?",
                                    (minted["id"],)).fetchone()[0]
            finally:
                con.close()
            assert first is not None, "the first touch did not stamp last_used_at"
            apikeys.touch(minted["id"])
            con = connect()
            try:
                second = con.execute("SELECT last_used_at FROM api_keys WHERE id=?",
                                     (minted["id"],)).fetchone()[0]
            finally:
                con.close()
            assert second == first, \
                "a second touch inside the interval rewrote last_used_at"
            # ...and it does update once the interval has passed.
            con = connect()
            try:
                con.execute("UPDATE api_keys SET last_used_at=? WHERE id=?",
                            (time.time() - apikeys.TOUCH_INTERVAL_SECONDS - 5,
                             minted["id"]))
                con.commit()
            finally:
                con.close()
            apikeys.touch(minted["id"])
            con = connect()
            try:
                third = con.execute("SELECT last_used_at FROM api_keys WHERE id=?",
                                    (minted["id"],)).fetchone()[0]
            finally:
                con.close()
            assert third > first - apikeys.TOUCH_INTERVAL_SECONDS, \
                "touch stopped updating after the interval elapsed"
        check("last_used_at is stamped at most once per interval, and still updates",
              touch_does_not_write_on_every_call)

        # --- revocation ------------------------------------------------------
        def revoked_key_stops_verifying_but_row_survives():
            r2 = c.post("/api/keys", json={"label": "throwaway"})
            doomed_raw, doomed_id = r2.json()["key"], r2.json()["id"]
            assert apikeys.verify(doomed_raw) is not None
            d = c.delete(f"/api/keys/{doomed_id}")
            assert d.status_code == 200, d.text
            assert apikeys.verify(doomed_raw) is None, "a revoked key still verifies"
            con = connect()
            try:
                row = con.execute("SELECT revoked_at FROM api_keys WHERE id=?",
                                  (doomed_id,)).fetchone()
            finally:
                con.close()
            assert row is not None, "revoking deleted the row instead of marking it"
            assert row["revoked_at"] is not None, "revoked_at was not set"
        check("a revoked key stops verifying and its row survives for audit",
              revoked_key_stops_verifying_but_row_survives)

        # --- the allowlist re-check, the one that fails silently -------------
        def de_allowlisting_kills_the_key():
            with TestClient(app) as u:
                sign_in(u, "user@example.edu")
                users_key = u.post("/api/keys", json={"label": "theirs"}).json()["key"]
            assert apikeys.verify(users_key) is not None, "setup failed"
            rm = c.delete("/api/admin/allowlist/user%40example.edu")
            assert rm.status_code == 200, rm.text
            assert apikeys.verify(users_key) is None, \
                ("a key still works after its owner left the allowlist — the same "
                 "removal ends their browser session, so this failure is silent")
        check("removing a user from the allowlist kills their API key too",
              de_allowlisting_kills_the_key)

        # --- cross-user isolation --------------------------------------------
        def a_user_cannot_touch_another_users_key():
            admin_key_id = minted["id"]
            with TestClient(app) as o:
                sign_in(o, "other@example.edu")
                listed = o.get("/api/keys").json()
                assert listed == [], f"a new user saw somebody else's keys: {listed}"
                d = o.delete(f"/api/keys/{admin_key_id}")
                assert d.status_code == 404, \
                    f"revoked another user's key by id (got {d.status_code})"
            assert apikeys.verify(raw) is not None, "the admin's key was revoked"
        check("one user cannot list or revoke another user's key",
              a_user_cannot_touch_another_users_key)

        def a_non_admin_cannot_reach_the_admin_half():
            with TestClient(app) as o:
                sign_in(o, "other@example.edu")
                for method, path, body in (
                        ("get", "/api/admin/keys", None),
                        ("post", "/api/admin/keys",
                         {"email": "other@example.edu", "label": "x"}),
                        ("delete", "/api/admin/keys/1", None)):
                    resp = getattr(o, method)(path, **({"json": body} if body else {}))
                    assert resp.status_code == 403, \
                        f"{method.upper()} {path} gave {resp.status_code}, want 403"
        check("a non-admin gets 403 from every admin key route",
              a_non_admin_cannot_reach_the_admin_half)

        # --- the admin half ---------------------------------------------------
        def admin_can_mint_for_a_user_and_records_who_did_it():
            r3 = c.post("/api/admin/keys",
                        json={"email": "other@example.edu", "label": "issued"})
            assert r3.status_code == 200, r3.text
            body = r3.json()
            assert body["key"].startswith(apikeys.KEY_PREFIX), body
            row = apikeys.verify(body["key"])
            assert row is not None and row["email"] == "other@example.edu", body
            listed = c.get("/api/admin/keys").json()
            mine = [k for k in listed if k["id"] == body["id"]]
            assert mine and mine[0]["created_by"] == "admin@example.edu", mine
            assert mine[0]["email"] == "other@example.edu", mine
        check("an admin can mint for a user, and created_by records who did",
              admin_can_mint_for_a_user_and_records_who_did_it)

        def admin_minting_refuses_a_stranger():
            r4 = c.post("/api/admin/keys", json={"email": "nobody@example.edu"})
            assert r4.status_code == 400, \
                f"minted a key for an address with no account: {r4.status_code}"
        check("minting for an address with no account is refused",
              admin_minting_refuses_a_stranger)

        def an_over_long_label_is_refused():
            r5 = c.post("/api/keys", json={"label": "x" * 500})
            assert r5.status_code == 422, \
                f"an over-long label was accepted ({r5.status_code})"
        check("an over-long label is refused rather than written",
              an_over_long_label_is_refused)

    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL API KEY TESTS PASSED")


if __name__ == "__main__":
    run()
