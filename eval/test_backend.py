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
os.environ["ADMIN_EMAILS"] = "admin@franklin.edu"

from fastapi.testclient import TestClient

from app import mailer

captured = {}
mailer.send_magic_link = lambda to, link: captured.__setitem__("link", link) or True
mailer.send_access_request = lambda *a, **k: True
mailer.send_access_approved = lambda to, link: captured.__setitem__("approved_link", link) or True

from app import skills
from app.main import app


def run():
    with TestClient(app) as c:
        # --- auth round trip -----------------------------------------------
        assert c.get("/api/auth/me").status_code == 401
        r = c.post("/api/auth/request", json={"email": "admin@franklin.edu"})
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

        # reused token must fail (already consumed by the POST above)
        c2 = TestClient(app)
        assert c2.post("/api/auth/verify", json={"token": token}).status_code == 400
        print("  ✓ magic-link token is single-use")

        # --- admin: allowlist ----------------------------------------------
        r_add = c.post("/api/admin/allowlist",
                       json={"email": "prof@franklin.edu", "note": "colleague"})
        assert r_add.status_code == 200
        assert r_add.json().get("invited") is True, "approving should email an invite"
        al = c.get("/api/admin/allowlist").json()
        assert any(x["email"] == "prof@franklin.edu" for x in al)
        print(f"  ✓ allowlist add ({len(al)} entries)")

        # approval emails a working, single-use sign-in link
        approved_link = captured.get("approved_link")
        assert approved_link and "token=" in approved_link, "no approval sign-in link"
        prof = TestClient(app)
        assert "/verify?token=" in approved_link, approved_link
        atok = approved_link.split("token=")[1]
        assert prof.post("/api/auth/verify", json={"token": atok}).status_code == 200
        pme = prof.get("/api/auth/me")
        assert pme.status_code == 200 and pme.json()["email"] == "prof@franklin.edu"
        print("  ✓ approval emails a working sign-in link")

        # re-adding an existing allowlisted email does NOT re-invite
        dup = c.post("/api/admin/allowlist",
                     json={"email": "prof@franklin.edu", "note": "dup"})
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
                           " VALUES ((SELECT id FROM users WHERE email='admin@franklin.edu'),"
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

    print("\nALL BACKEND TESTS PASSED")


if __name__ == "__main__":
    run()
