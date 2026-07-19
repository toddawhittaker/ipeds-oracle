"""End-to-end backend test (no LLM): auth round-trip, admin, skills, cache, CSV.

Runs against a throwaway app.db. Patches the mailer to capture the magic link.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isolate app.db before importing settings
tmp = tempfile.mkdtemp()
os.environ["APP_DB_PATH"] = str(Path(tmp) / "app.db")
os.environ["ADMIN_EMAILS"] = "admin@example.edu"

from fastapi.testclient import TestClient

from app import mailer

captured = {}
mailer.send_magic_link = lambda to, link: captured.__setitem__("link", link) or True
mailer.send_access_request = lambda *a, **k: True
mailer.send_access_approved = lambda to: captured.__setitem__("approved", to) or True

from app import skills
from app.config import PRODUCT_NAME
from app.main import app


def run():
    # The FastAPI app title is the fixed PRODUCT_NAME constant, not an
    # institution-configurable setting (that was renamed app_title -> gone).
    assert app.title == PRODUCT_NAME, app.title
    print(f"  ✓ FastAPI app title == PRODUCT_NAME ({PRODUCT_NAME!r})")

    with TestClient(app) as c:
        # --- auth round trip -----------------------------------------------
        assert c.get("/api/auth/me").status_code == 401
        r = c.post("/api/auth/request", json={"email": "admin@example.edu"})
        assert r.status_code == 200, r.text
        link = captured["link"]
        # Emailed link lands on the SPA confirm page; a GET there never consumes.
        assert "/verify?token=" in link, link
        token = link.split("token=")[1]
        g = c.get(f"/api/auth/verify?token={token}", follow_redirects=False)
        assert g.status_code == 303 and "/verify?token=" in g.headers["location"], g.headers
        v = c.post("/api/auth/verify", json={"token": token})
        assert v.status_code == 200, v.text
        me = c.get("/api/auth/me")
        assert me.status_code == 200 and me.json()["is_admin"], me.text
        print("  ✓ magic-link login (POST verify) + admin session")

        # --- /me has_data (fresh-deploy no-data detection) -------------------
        assert isinstance(me.json().get("has_data"), bool), \
            f"/me must include a boolean has_data flag: {me.text}"
        print("  ✓ /me includes a boolean has_data flag")

        from app.routers import auth as auth_router
        orig_has_data = auth_router.has_ipeds_data
        auth_router.has_ipeds_data = lambda: False
        try:
            me_nodata = c.get("/api/auth/me")
        finally:
            auth_router.has_ipeds_data = orig_has_data
        assert me_nodata.status_code == 200, me_nodata.text
        assert me_nodata.json().get("has_data") is False, me_nodata.text
        print("  ✓ /me reports has_data=False when patched to the no-data state")

        # --- TRUST_LLM_PROVIDER parse (privacy warning gate) ----------------
        # The parser must FAIL SAFE: only explicit opt-in tokens are True; every
        # false-ish, blank, or unrecognized value is False so the chat privacy
        # warning stays visible. (Undefined can't be passed to a str parser; the
        # setting's default of "false" covers the "variable absent" acceptance
        # criterion — asserted via /me below.)
        from app.config import is_truthy
        for v in ["true", "TRUE", " True ", "t", "T", "yes", "Yes", "y", "1"]:
            assert is_truthy(v) is True, f"{v!r} should resolve True"
        for v in ["false", "FALSE", "f", "no", "n", "0", "", "   ",
                  "maybe", "trueish", "2", "01", "yep", "on"]:
            assert is_truthy(v) is False, f"{v!r} should resolve False (fail-safe)"
        print("  ✓ is_truthy: only true/t/yes/y/1 are True; all else (incl. blank/invalid) False")

        # /me exposes the RESOLVED boolean (not the raw string), defaulting to
        # False when TRUST_LLM_PROVIDER is unset — so the warning shows.
        assert me.json().get("trust_llm_provider") is False, me.text
        print("  ✓ /me exposes trust_llm_provider=False by default (warning visible)")

        # reused token must fail (already consumed by the POST above)
        c2 = TestClient(app)
        assert c2.post("/api/auth/verify", json={"token": token}).status_code == 400
        print("  ✓ magic-link token is single-use")

        # --- admin: allowlist ----------------------------------------------
        r_add = c.post("/api/admin/allowlist",
                       json={"email": "prof@example.edu", "note": "colleague"})
        assert r_add.status_code == 200
        assert r_add.json().get("invited") is True, "approving should email the approval notice"
        al = c.get("/api/admin/allowlist").json()
        assert any(x["email"] == "prof@example.edu" for x in al)
        print(f"  ✓ allowlist add ({len(al)} entries)")

        # approval emails a NOTICE (no magic link) naming the approved address;
        # the approved user then signs in by requesting their OWN one-time link.
        assert captured.get("approved") == "prof@example.edu", "no approval notice sent"
        prof = TestClient(app)
        prof.post("/api/auth/request", json={"email": "prof@example.edu"})
        atok = captured["link"].split("token=")[1]
        assert prof.post("/api/auth/verify", json={"token": atok}).status_code == 200
        pme = prof.get("/api/auth/me")
        assert pme.status_code == 200 and pme.json()["email"] == "prof@example.edu"
        print("  ✓ approved user can request a working sign-in link")

        # re-adding an existing allowlisted email does NOT re-invite
        dup = c.post("/api/admin/allowlist",
                     json={"email": "prof@example.edu", "note": "dup"})
        assert dup.json().get("invited") is False
        print("  ✓ re-adding an existing member does not re-invite")

        # access request created for a stranger, visible to admin
        c.post("/api/auth/request", json={"email": "stranger@x.com"})
        reqs = c.get("/api/admin/access-requests").json()
        assert any(r["email"] == "stranger@x.com" for r in reqs)
        print("  ✓ access request recorded")

        # --- skills seeded + retrieval -------------------------------------
        sk = c.get("/api/admin/skills").json()
        assert len(sk) >= 3, f"expected seeded skills, got {len(sk)}"
        print(f"  ✓ {len(sk)} skills seeded")
        block, ids = skills.retrieve_skills_block(
            "associate degrees in nursing per year nationwide")
        if ids:
            print(f"  ✓ skill retrieval returned {len(ids)} exemplar(s) via embeddings")
        else:
            print("  ⚠ skill retrieval empty (fastembed not installed — expected offline)")

        # --- usage endpoint ------------------------------------------------
        u = c.get("/api/admin/usage").json()
        assert "totals" in u
        print("  ✓ usage dashboard endpoint")

        # --- CSV export path via a hand-made conversation ------------------
        # create a conversation + assistant message with a known SQL, then export
        import json as _json
        import time

        from app.db import connect
        con = connect()
        conv = con.execute("INSERT INTO conversations(user_id,title,created_at,updated_at)"
                           " VALUES ((SELECT id FROM users WHERE email='admin@example.edu'),"
                           " 'x', ?, ?)", (time.time(), time.time()))
        conv_id = conv.lastrowid
        sql = ("SELECT year, SUM(ctotalt) a FROM c_a "
               "WHERE awlevel=3 AND majornum=1 AND cipcode='99' GROUP BY year")
        con.execute("INSERT INTO messages(conversation_id,role,content,sql_log,created_at)"
                    " VALUES (?,?,?,?,?)", (conv_id, "assistant", "ans",
                                            _json.dumps([sql]), time.time()))
        msg_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.commit(); con.close()
        csv_resp = c.get(f"/api/chat/messages/{msg_id}/download.csv")
        assert csv_resp.status_code == 200 and "year" in csv_resp.text
        rows = csv_resp.text.strip().splitlines()
        print(f"  ✓ CSV export ({len(rows)-1} data rows)")

        # --- SPA deep-link contract (server-side) ---------------------------
        # The client-side router (react-router-dom) owns /chat/:id and
        # /admin/:tab; the server's only job is to serve the SAME index.html
        # shell for those paths as it does for "/", so a hard refresh/direct
        # link doesn't 404. backend/app/main.py's catch-all `spa()` route (and the
        # `WEB_DIST` it serves from) is only registered when frontend/dist has been
        # built (`npm run build`), so skip cleanly rather than failing in a
        # checkout that hasn't built the frontend — same convention as this
        # suite's fastembed-not-installed skips above.
        from app.main import WEB_DIST
        index_html = WEB_DIST / "index.html"
        if index_html.exists():
            shell = index_html.read_text()
            for deep_link in ("/chat/1", "/admin/users"):
                dr = c.get(deep_link, follow_redirects=False)
                assert dr.status_code == 200, \
                    f"{deep_link}: {dr.status_code} {dr.text[:200]!r}"
                assert dr.text == shell, \
                    f"{deep_link} did not serve the built SPA index.html shell verbatim"
            print("  ✓ GET /chat/1 and GET /admin/users serve the SPA index.html "
                  "shell (deep-link contract)")
            # A non-GET method to an UNMATCHED /api/* path must 404, not the
            # misleading 405 the GET-only SPA catch-all would otherwise give:
            # main.py registers a dedicated any-method /api/{path} 404 route
            # ahead of spa() precisely so a removed endpoint reads as "not
            # found", not "method not allowed" (Starlette resolves by path
            # first, so the GET-only catch-all's pattern would match a POST).
            gone = c.post("/api/chat/messages/1/does-not-exist", json={})
            assert gone.status_code == 404, \
                f"unmatched /api/* POST must 404, got {gone.status_code} " \
                "(405 means the any-method api_404 route was lost)"
            print("  ✓ POST to an unmatched /api/* path returns 404, not 405")
        else:
            print("  ⚠ frontend/dist not built — skipping SPA deep-link server contract check")

    print("\nALL BACKEND TESTS PASSED")


if __name__ == "__main__":
    run()
