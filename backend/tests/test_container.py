"""Static, statically-verifiable contracts for the non-root-container work
(Dockerfile `USER`, `scripts/docker-entrypoint.sh`, `compose.yaml`,
`.env.example`).

Most of that change is only verifiable by actually building the image —
whether the build succeeds, whether the app can write a bind-mounted `/data`
as uid 10001, whether the baked fastembed cache loads read-only under that
uid. **None of that is covered here** — it needs CI's Docker image job or a
manual `docker build`. This suite only pins the parts of the plan that can be
checked by reading the repo's own files, the same spirit as
`test_env_example.py` and `test_startup_checks.py`: parse the real file, never
embed a copy of its contents, and fail red until the change lands.

Standalone script style (`sys.exit(1)` on failure, no API key), auto-discovered
by `scripts/run_backend_suites.sh`'s `test_*.py` glob.
"""
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import Settings  # noqa: E402

DOCKERFILE = ROOT / "Dockerfile"
ENTRYPOINT = ROOT / "scripts" / "docker-entrypoint.sh"
COMPOSE = ROOT / "compose.yaml"
ENV_EXAMPLE = ROOT / ".env.example"

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


# ---------------------------------------------------------------------------
# 1. Dockerfile: the LAST USER instruction is non-root and numeric.
# ---------------------------------------------------------------------------

# Docker instructions are conventionally uppercase but are case-insensitive to
# the engine; match either. Anchored to the start of the (stripped) line so a
# mention of "USER" inside a comment or RUN command is never mistaken for the
# instruction.
_USER_INSTRUCTION_RE = re.compile(r"^USER\s+(\S+)", re.IGNORECASE)


def _user_instructions(text: str) -> list[str]:
    """Every USER instruction's argument, in file order."""
    out = []
    for line in text.splitlines():
        m = _USER_INSTRUCTION_RE.match(line.strip())
        if m:
            out.append(m.group(1))
    return out


def test_dockerfile_has_at_least_one_user_instruction():
    """THE HEADLINE REGRESSION: today's Dockerfile has NO `USER` instruction at
    all, so the container runs as root while shelling out to `mdb-export` over
    an admin-uploaded `.accdb` — the worst possible privilege level for a
    subprocess fed untrusted input. This must go green only once a `USER` line
    is added; a Dockerfile with none is exactly the vulnerable state."""
    text = DOCKERFILE.read_text()
    users = _user_instructions(text)
    assert users, (
        "Dockerfile has no USER instruction — the container still runs as "
        "root end to end.")


def test_dockerfiles_final_user_is_non_root():
    """A multi-stage/multi-USER Dockerfile is expected here (build steps that
    write files as root, e.g. to bake the fastembed cache into a directory
    the app user doesn't own yet, then switch back) — but whichever USER
    instruction is LAST is the one that governs the actually-running
    container. Regression this catches: a Dockerfile that flips to a
    non-root user for one build step and then (by omission, or a stray
    `USER root` added later for some other RUN) drifts back to root by the
    final layer — every earlier USER line would look reassuring on a diff
    while the shipped image is still root."""
    text = DOCKERFILE.read_text()
    users = _user_instructions(text)
    assert users, "no USER instruction found (see the previous test)"
    last = users[-1]
    assert last.lower() not in ("root", "0", "0:0"), (
        f"the LAST USER instruction is {last!r} — the running container is root.")


def test_dockerfiles_final_user_is_numeric():
    """A `runAsNonRoot`/`runAsUser` admission check (Kubernetes, or any
    container-security scanner) can only verify a NUMERIC uid — it cannot
    resolve a username against /etc/passwd inside the image without running
    it. `USER app` would satisfy this suite's "non-root" test above while
    still failing that check and every SBOM/policy tool that greps the
    Dockerfile for a bare uid. The final USER argument must therefore be
    `<uid>` or `<uid>:<gid>`, both purely numeric."""
    text = DOCKERFILE.read_text()
    users = _user_instructions(text)
    assert users, "no USER instruction found (see the first test in this suite)"
    last = users[-1]
    parts = last.split(":")
    assert len(parts) in (1, 2), f"unexpected USER argument shape: {last!r}"
    assert all(p.isdigit() for p in parts), (
        f"the last USER instruction ({last!r}) is not purely numeric "
        "(uid or uid:gid) — a runAsNonRoot/runAsUser check needs a number, "
        "not a username.")


# ---------------------------------------------------------------------------
# 2. scripts/docker-entrypoint.sh: preflight still runs before `exec
#    uvicorn`, and `--no-proxy-headers` is still passed. Line-index based, so
#    it fails if either is merely PRESENT somewhere but out of order.
# ---------------------------------------------------------------------------

def _line_index(lines: list[str], predicate) -> int:
    for i, line in enumerate(lines):
        if predicate(line):
            return i
    raise AssertionError(f"no line matched {predicate!r} in docker-entrypoint.sh")


def test_entrypoint_runs_preflight_before_exec_uvicorn():
    """THE REGRESSION THIS GUARDS: `python -m app.startup_checks` (the
    filesystem preflight from #304 — see test_startup_checks.py) exists
    specifically to turn an unwritable-`/data`-under-a-new-non-root-uid
    failure into a legible chown message instead of a bare
    sqlite3.OperationalError raised from inside uvicorn's import. This PR is
    EXACTLY the change that makes that failure mode reachable in practice
    (root-owned bind mounts under a container that now runs as uid 10001) —
    so a careless edit to this script that reorders or drops the preflight
    call would silently reintroduce the opaque crash on every affected
    upgrade, at the worst possible moment. A plain substring check
    ('both strings appear in the file') would not catch the preflight being
    moved to AFTER `exec uvicorn` (which never returns) or into a dead
    branch; this asserts strict line order instead."""
    lines = ENTRYPOINT.read_text().splitlines()
    preflight_idx = _line_index(lines, lambda line: "app.startup_checks" in line)
    exec_idx = _line_index(lines, lambda line: re.search(r"\bexec\s+uvicorn\b", line))
    assert preflight_idx < exec_idx, (
        f"the startup preflight (line {preflight_idx}) must run BEFORE "
        f"`exec uvicorn` (line {exec_idx}), not after")


def test_entrypoint_still_passes_no_proxy_headers():
    """`--no-proxy-headers` is the fix for #86 (spoofable X-Forwarded-For):
    without it, uvicorn's own ProxyHeadersMiddleware trusts a loopback-adjacent
    peer and rewrites `scope["client"]` from a header the app already handles
    itself via TRUSTED_PROXY_COUNT — re-opening the per-IP rate-limit spoofing
    hole. Nothing about switching the container to a non-root user should
    touch this flag, but an entrypoint rewrite (e.g. to add a `su-exec`/`gosu`
    step, or to switch how uvicorn is invoked) is exactly the kind of edit
    that could drop it by accident while looking unrelated. Checked on the
    same `set --`/`exec uvicorn "$@"` line pair the file actually uses, and
    ordered before the exec so a flag added to the WRONG (dead) branch still
    fails."""
    lines = ENTRYPOINT.read_text().splitlines()
    flag_idx = _line_index(lines, lambda line: "--no-proxy-headers" in line)
    exec_idx = _line_index(lines, lambda line: re.search(r"\bexec\s+uvicorn\b", line))
    assert flag_idx < exec_idx, (
        "--no-proxy-headers must be assigned before `exec uvicorn` runs "
        f"(found at line {flag_idx}, exec at line {exec_idx})")


# ---------------------------------------------------------------------------
# 3. compose.yaml: every writable Settings path field is redirected onto the
#    bind-mounted /data volume — the drift test. Derived from config.Settings
#    by naming convention (mirrors test_startup_checks.py's
#    test_every_path_setting_is_covered_by_a_probe_or_explicitly_excluded),
#    never hardcoded.
# ---------------------------------------------------------------------------

# The one expected exclusion, with a reason: docs/SCHEMA.md is baked into the
# image at build time (`COPY docs/SCHEMA.md`) and only ever READ — it is never
# written by the running app, so it has no business pointing at the writable
# /data volume, and a self-hoster overriding it has nothing to do with
# non-root permissions. Mirrors EXCLUDED_SETTINGS in startup_checks.py.
EXCLUDED_COMPOSE_PATH_SETTINGS = {"schema_md_path"}


def _path_setting_names() -> set[str]:
    return {
        name for name in Settings.model_fields
        if name.endswith("_path") or name.endswith("_dir")
    }


def _compose_app_environment() -> dict:
    data = yaml.safe_load(COMPOSE.read_text())
    env = data["services"]["app"].get("environment") or {}
    assert isinstance(env, dict), (
        "compose.yaml's services.app.environment must be a mapping (KEY: value), "
        "not the list ('KEY=value') form, for this test to inspect it directly."
    )
    return env


def test_every_writable_path_setting_points_under_data_in_compose():
    """THE HIGH-VALUE DRIFT TEST. A future Path setting added to
    config.Settings (the next NCES_WORK_DIR) that is NOT wired into
    compose.yaml's environment block would default into the image's own
    filesystem (e.g. under /srv, owned by root) — invisible until an admin
    who is now uid 10001 tries to use that feature in production and hits a
    permission error nothing in review would have caught, because the
    setting works perfectly in every dev/CI environment that never runs as a
    restricted uid. Derived from config.Settings by naming convention (every
    field ending in `_path`/`_dir`), exactly like test_startup_checks.py's
    equivalent drift test — a hardcoded list here would be the very thing
    this test exists to prevent."""
    path_names = _path_setting_names()
    # Sanity: prove the derivation isn't accidentally empty or missing a
    # setting known to exist (would make the rest of this test vacuous).
    assert {"ipeds_db_path", "app_db_path", "log_db_path", "data_dir",
            "upload_dir", "nces_work_dir", "schema_md_path"} <= path_names, path_names
    assert len(EXCLUDED_COMPOSE_PATH_SETTINGS) <= 3, (
        "EXCLUDED_COMPOSE_PATH_SETTINGS has grown past a handful — point the "
        "setting at /data instead of exempting it.")

    env = _compose_app_environment()
    missing = []
    not_under_data = []
    for name in sorted(path_names - EXCLUDED_COMPOSE_PATH_SETTINGS):
        env_key = name.upper()
        if env_key not in env:
            missing.append(env_key)
            continue
        value = str(env[env_key])
        if not (value == "/data" or value.startswith("/data/")):
            not_under_data.append((env_key, value))
    assert not missing, (
        f"compose.yaml's services.app.environment does not set {missing} — "
        "a new Settings path field must be redirected onto the bind-mounted "
        "/data volume, or added to EXCLUDED_COMPOSE_PATH_SETTINGS with a reason."
    )
    assert not not_under_data, (
        f"these compose.yaml env values are not under /data: {not_under_data} — "
        "once the container runs as a non-root uid, anything outside the "
        "bind-mounted volume is unwritable."
    )


def test_schema_md_path_is_never_redirected_onto_data_in_compose():
    """The one expected exclusion, pinned so its removal is a deliberate act
    (mirrors test_startup_checks.py's `test_schema_md_path_is_never_probed`):
    SCHEMA.md ships read-only inside the image and is never written, so it
    must not appear in compose.yaml's environment block at all."""
    env = _compose_app_environment()
    assert "SCHEMA_MD_PATH" not in env, (
        "SCHEMA_MD_PATH should not be set in compose.yaml — it's a read-only, "
        "image-baked file, not part of the writable /data volume."
    )


# ---------------------------------------------------------------------------
# 4. compose.yaml sets a `user:` key, and .env.example documents
#    IPEDS_UID/IPEDS_GID.
# ---------------------------------------------------------------------------

def test_compose_sets_a_user_key_on_the_app_service():
    """THE REGRESSION THIS CATCHES: `USER 10001:10001` in the Dockerfile only
    fixes the default — `docker compose run`/`up` can still override the
    running uid, and more importantly a bind-mounted ./srv-data is host-owned
    (usually the operator's own uid, not 10001), so the numbers have to be
    made configurable per deployment rather than hardcoded into the image.
    Without a `user:` key (or it silently vanishing in a later edit),
    compose falls back to whatever the image's own USER resolved to, which
    can't be adjusted to match a self-hoster's host-side ownership without
    rebuilding the image."""
    data = yaml.safe_load(COMPOSE.read_text())
    app = data["services"]["app"]
    assert "user" in app, (
        "compose.yaml's services.app has no `user:` key — the running uid/gid "
        "can't be adjusted per deployment to match a bind-mounted ./srv-data's "
        "host ownership."
    )


def test_env_example_documents_ipeds_uid_and_gid():
    """compose.yaml's `user:` key (previous test) is only configurable if
    IPEDS_UID/IPEDS_GID are documented somewhere an operator will read them —
    otherwise the knob exists in the compose file but nobody self-hosting
    knows to set it, and everyone either fights a permission-denied
    bind-mount or (worse) resorts to `chmod -R 777 srv-data`."""
    text = ENV_EXAMPLE.read_text()
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", text, re.M))
    missing = {"IPEDS_UID", "IPEDS_GID"} - documented
    assert not missing, (
        f".env.example does not document {sorted(missing)} — an operator "
        "self-hosting behind compose.yaml's `user:` key has no way to "
        "discover these exist without reading the compose file itself."
    )


def run():
    print("Non-root container contracts (Dockerfile / entrypoint / compose / .env.example):")
    check("Dockerfile has at least one USER instruction",
          test_dockerfile_has_at_least_one_user_instruction)
    check("Dockerfile's final USER is non-root",
          test_dockerfiles_final_user_is_non_root)
    check("Dockerfile's final USER is numeric",
          test_dockerfiles_final_user_is_numeric)
    check("entrypoint runs the preflight before exec uvicorn",
          test_entrypoint_runs_preflight_before_exec_uvicorn)
    check("entrypoint still passes --no-proxy-headers",
          test_entrypoint_still_passes_no_proxy_headers)
    check("every writable path setting points under /data in compose.yaml",
          test_every_writable_path_setting_points_under_data_in_compose)
    check("schema_md_path is never redirected onto /data in compose.yaml",
          test_schema_md_path_is_never_redirected_onto_data_in_compose)
    check("compose.yaml sets a user: key on the app service",
          test_compose_sets_a_user_key_on_the_app_service)
    check(".env.example documents IPEDS_UID/IPEDS_GID",
          test_env_example_documents_ipeds_uid_and_gid)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL CONTAINER TESTS PASSED")


if __name__ == "__main__":
    run()
