"""`scripts/ci_env.sh` must actually reproduce CI, not just claim to.

THE REGRESSION THIS CATCHES (it has happened twice — see the file's own header
comment): a maintainer edits `ci_env.sh` by hand and either (a) typos an
env-var name, which silently blanks NOTHING (`export EMIAL_DOMAIN=""` leaves
the real `EMAIL_DOMAIN` however the developer's `.env` set it — zero signal,
and the suite that depends on the blank now fails only in CI, minutes after
merge), or (b) pins a value that DIFFERS from `config.py`'s default, which
makes a developer's box diverge from CI (which has no `.env` and therefore
runs on bare defaults) and can mask a real failure that only CI would catch.
The file's own doctrine states this explicitly: pin *equal to the default* to
reproduce CI; pinning anything else is the trap the file exists to avoid,
with exactly one documented, deliberate exception.

Two contracts, mirroring `test_env_example.py`'s shape for the same reason —
they are siblings guarding the two directions a hand-maintained env list can
drift (documentation vs. behavior):

  1. Every `export NAME=...` in `ci_env.sh` names a real `config.Settings`
     field (contract: NAME.lower() in Settings.model_fields).
  2. Every pinned value, once parsed the way `config.Settings` actually
     parses it, resolves to the SAME effective behavior as that field's own
     class-level default — except an explicit, small, justified allowlist.

Both are computed from the REAL files (`ci_env.sh`, `config.Settings`) by
parsing/importing them — never a hand-copied restatement of either.

"Resolves to the same effective behavior" is deliberately not always a bare
`==` on the raw parsed value. Two fields need more than that, and this suite
gets both from the app's OWN resolution logic rather than re-deriving it (so
a future change to that logic is automatically honored, not silently
outdated):

  * `trust_llm_provider` is a raw string field (kept that way so an invalid
    value fails SAFE instead of crashing startup — see config.py's comment)
    whose default is the STRING `"false"`, not `""`. `ci_env.sh` pins `""`.
    Compared literally these differ; compared through the app's own
    `trust_llm_provider_enabled` property (`is_truthy`) both resolve to
    False, which is the only question that matters — `is_truthy` was
    written specifically so "unset" and "false" behave identically.
  * `mail_backend` defaults to `"auto"`, not `""` — `ci_env.sh` pins `""`.
    `mailer._resolve_backend` reads it as `(s.mail_backend or "auto")`, so a
    blank string and `"auto"` are the SAME branch; combined with
    `resend_api_key=""` and `smtp_host=""` (both pinned, both already equal
    to their own defaults) both resolve to the `"console"` backend. This
    needs the OTHER pinned fields applied at the same time to evaluate
    honestly, so the check builds one "pinned" Settings instance from every
    export in the file at once, and one "pure defaults" instance immune to
    whatever `.env`/os-environ happens to be sitting on the machine running
    this suite — never `Settings()` bare, which would read real ambient
    environment (including, circularly, these very pins if the caller
    sourced `ci_env.sh` first, per this suite's own run instructions) and
    prove nothing about the DEFAULT.

WHAT THIS SUITE DOES NOT AND CANNOT MECHANIZE: *which* settings need pinning
in the first place. That is a judgment call about which production `.env`
values would change a test's outcome — not a derivable set, so nothing here
can flag a setting that SHOULD be in `ci_env.sh` and simply isn't. (Two such
gaps — CRITIC_ENABLED, SKILLS_ENABLED — are known and are the subject of the
PR this test file ships in; until they're added, this suite has nothing to
say about them, by design.) What it mechanizes is drift in what IS written:
a typo that blanks nothing, or a pinned value that quietly stops reproducing
CI.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app import mailer  # noqa: E402
from app.config import Settings  # noqa: E402

CI_ENV_SH = ROOT / "scripts" / "ci_env.sh"

# Documented, deliberate exceptions to "pinned value == config default".
# ci_env.sh's own header comment names exactly this one: FIGURE_RETRY_ENABLED
# defaults to True in production but is pinned False, because the retry makes
# a real (fail-open) LLM call that the key-free/no-network test posture must
# not attempt. Grow this only with the same kind of justification, in both
# this file's comment and ci_env.sh's.
ALLOWED_DIVERGENCES = {"FIGURE_RETRY_ENABLED"}

# Fields whose pinned value must be compared through the app's own resolution
# logic rather than a bare `==`, because the raw value legitimately differs
# from the default while the BEHAVIOR it produces does not. Each entry is a
# `(pinned_settings, default_settings) -> bool` callable and is justified in
# the module docstring above. This is not a general escape hatch — a field
# lands here only when its raw default is provably not "", and its blank pin
# is provably the equivalent choice via existing app code (never re-derived
# here).
SPECIAL_EQUIVALENCE = {
    "trust_llm_provider": lambda pinned, default: (
        pinned.trust_llm_provider_enabled == default.trust_llm_provider_enabled
    ),
    "mail_backend": lambda pinned, default: (
        mailer._resolve_backend(pinned) == mailer._resolve_backend(default)
    ),
}

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


_EXPORT_RE = re.compile(r'^export ([A-Z][A-Z0-9_]*)=(.*)$', re.MULTILINE)


def _parsed_pins() -> dict:
    """Every `export NAME=value` line in ci_env.sh, parsed from the real file
    (never a hand-kept copy of it). Comment lines (which document some of
    these same names in prose, e.g. "* COOKIE_SECURE=false — ...") start with
    `#`, not `export`, so they're never mistaken for a pin."""
    text = CI_ENV_SH.read_text()
    pins = {}
    for name, raw in _EXPORT_RE.findall(text):
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        pins[name] = value
    return pins


def _default_settings() -> Settings:
    """A Settings instance pinned to EVERY field's own class-level default,
    immune to whatever .env/os-environ happens to be sitting on the machine
    running this suite. Passing every default explicitly (pydantic-settings
    gives init kwargs top priority) is what makes this the honest "CI, which
    has no .env" baseline rather than an accidental read of ambient state."""
    defaults = {name: field.default for name, field in Settings.model_fields.items()}
    return Settings(**defaults)


def _pinned_settings(pins: dict) -> Settings:
    """A Settings instance with every OTHER field at its true default and
    every exported name in ci_env.sh applied together, the way they actually
    land on a developer's process. Built from the same defaults dict as
    _default_settings so the only difference between the two instances is
    exactly the exported pins — required for a cross-field check like
    mail_backend's (see SPECIAL_EQUIVALENCE)."""
    defaults = {name: field.default for name, field in Settings.model_fields.items()}
    overlay = {name.lower(): raw for name, raw in pins.items()}
    return Settings(**{**defaults, **overlay})


def test_every_pinned_name_is_a_real_setting():
    pins = _parsed_pins()
    assert pins, (
        "parsed zero `export NAME=value` lines out of ci_env.sh — the parser or the file broke")
    unknown = sorted(n for n in pins if n.lower() not in Settings.model_fields)
    assert not unknown, (
        f"{len(unknown)} name(s) exported by ci_env.sh are not config.Settings fields: "
        f"{unknown}. A typo here blanks NOTHING silently (the real env var is untouched) "
        f"and gives zero signal — fix the name or remove the stale export.")


def test_every_pinned_value_matches_the_config_default_or_is_an_explicit_exception():
    pins = _parsed_pins()
    default_settings = _default_settings()
    pinned_settings = _pinned_settings(pins)

    mismatched = []
    for name in sorted(pins):
        field = name.lower()
        if field not in Settings.model_fields:
            continue  # already reported by the sibling contract
        if name in ALLOWED_DIVERGENCES:
            continue
        equal = (
            SPECIAL_EQUIVALENCE[field](pinned_settings, default_settings)
            if field in SPECIAL_EQUIVALENCE
            else getattr(pinned_settings, field) == getattr(default_settings, field)
        )
        if not equal:
            mismatched.append(
                f"{name}: pinned={getattr(pinned_settings, field)!r} "
                f"default={getattr(default_settings, field)!r}")
    assert not mismatched, (
        f"{len(mismatched)} pin(s) in ci_env.sh no longer reproduce config.py's default, "
        f"and aren't in ALLOWED_DIVERGENCES: {mismatched}. CI has no .env and runs on bare "
        f"defaults, so a pin that drifted from the default makes a local run diverge from "
        f"CI and can mask a real CI-only failure. Either restore the pin to the default, or "
        f"— only with a justification in both this file and ci_env.sh's own comment — add "
        f"the name to ALLOWED_DIVERGENCES.")


def test_the_divergence_allowlist_stays_small_and_actually_divergent():
    # Mirrors test_env_example.py's ratchet on UNDOCUMENTED_ON_PURPOSE: the
    # escape hatch exists, but letting it grow unchecked — or letting a
    # once-real exception go stale after the pin is fixed — quietly
    # re-widens the hole contract 2 exists to close.
    assert len(ALLOWED_DIVERGENCES) <= 3, (
        "ALLOWED_DIVERGENCES has grown past a handful — that's the doctrine eroding, "
        "not a coincidence. Each entry should be rare and well-justified.")

    pins = _parsed_pins()
    default_settings = _default_settings()
    pinned_settings = _pinned_settings(pins)
    for name in sorted(ALLOWED_DIVERGENCES):
        assert name in pins, (
            f"{name} is in ALLOWED_DIVERGENCES but ci_env.sh no longer exports it — "
            f"remove the now-dead exception.")
        field = name.lower()
        equal = (
            SPECIAL_EQUIVALENCE[field](pinned_settings, default_settings)
            if field in SPECIAL_EQUIVALENCE
            else getattr(pinned_settings, field) == getattr(default_settings, field)
        )
        assert not equal, (
            f"{name} is listed as a deliberate divergence from the config default, but its "
            f"current pin actually MATCHES the default — the exception is stale documentation "
            f"and should be removed from ALLOWED_DIVERGENCES (a dead exception invites the next "
            f"real divergence to hide behind it unnoticed).")


def run():
    print("ci_env.sh reproduces CI:")
    check("every exported name is a real Settings field",
          test_every_pinned_name_is_a_real_setting)
    check("every pinned value matches its config default (or is an explicit exception)",
          test_every_pinned_value_matches_the_config_default_or_is_an_explicit_exception)
    check("the divergence allowlist stays small and actually divergent",
          test_the_divergence_allowlist_stays_small_and_actually_divergent)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL CI_ENV.SH TESTS PASSED")


if __name__ == "__main__":
    run()
