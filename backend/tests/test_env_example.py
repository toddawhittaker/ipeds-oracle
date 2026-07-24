"""`.env.example` must stay a complete inventory of what the app reads.

THE REGRESSION THIS CATCHES: adding a field to `config.Settings` without
documenting it in `.env.example`. That drift is invisible — nothing fails, the
app runs fine, and a self-hoster who wants to turn off (say) the critic or the
GitHub update check has to read the source to discover the setting exists.
CONTRIBUTING.md claims every setting lives in `.env.example`; when this file was
written that claim was false for 23 of 72 settings.

It also catches the reverse: a `KEY=` left in `.env.example` after the setting
was renamed or removed, which sends an operator chasing a value the app ignores.

Deliberately NOT asserting the documented DEFAULTS match the code. A commented
example line is often illustrative rather than the literal default (`.env.example`
shows `IPEDS_TAG=latest`, `LLM_INPUT_COST_PER_MTOK=0.27`), and pinning them would
be a constant-echo test that fails on every harmless copy edit.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import Settings  # noqa: E402

ENV_EXAMPLE = ROOT / ".env.example"

# Env vars .env.example documents that are NOT app settings — read by compose,
# the Dockerfile or a helper script rather than by config.Settings.
NOT_APP_SETTINGS = {
    "IPEDS_TAG",        # compose.yaml: which published image tag to run
    "SSL_CERTFILE",     # scripts/docker-entrypoint.sh
    "SSL_KEYFILE",      # scripts/docker-entrypoint.sh
    "BACKUP_REMOTE",    # scripts/backup_app_db.py (standalone, not app config)
    "BACKUP_DIR",
    "BACKUP_KEEP",
}

# Settings deliberately left out of .env.example. Empty on purpose — add here
# ONLY with a comment saying why an operator should never set it, and expect to
# justify it in review.
UNDOCUMENTED_ON_PURPOSE: set[str] = set()

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


def _documented_keys() -> set[str]:
    """Every KEY= token in .env.example, commented or not."""
    text = ENV_EXAMPLE.read_text()
    return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", text, re.M))


def _setting_keys() -> set[str]:
    return {name.upper() for name in Settings.model_fields}


def test_every_setting_is_documented():
    missing = sorted(_setting_keys() - _documented_keys() - UNDOCUMENTED_ON_PURPOSE)
    assert not missing, (
        f"{len(missing)} setting(s) in config.Settings are absent from .env.example: "
        f"{missing}. Add each one (with a line saying what it does and when to "
        f"change it) in the same PR that introduces it.")


def test_no_documented_key_is_stale():
    stale = sorted(_documented_keys() - _setting_keys() - NOT_APP_SETTINGS)
    assert not stale, (
        f"{len(stale)} key(s) in .env.example are not read by config.Settings: "
        f"{stale}. Either the setting was renamed/removed (drop the line) or it "
        f"belongs to compose/a script (add it to NOT_APP_SETTINGS with a comment).")


def test_the_opt_out_set_stays_empty_or_justified():
    # A cheap ratchet: the escape hatch exists, but silently growing it would
    # re-open exactly the hole this suite closes.
    assert len(UNDOCUMENTED_ON_PURPOSE) <= 3, (
        "UNDOCUMENTED_ON_PURPOSE has grown past a handful — document the settings "
        "instead of exempting them.")


def run():
    print(".env.example completeness:")
    check("every config.Settings field is documented", test_every_setting_is_documented)
    check("no documented key is stale", test_no_documented_key_is_stale)
    check("the opt-out set stays small", test_the_opt_out_set_stays_empty_or_justified)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL .ENV.EXAMPLE TESTS PASSED")


if __name__ == "__main__":
    run()
