"""`requirements.lock` must actually satisfy `requirements.txt`.

THE REGRESSION THIS CATCHES: bumping a floor in `requirements.txt` without
regenerating the lock. Nothing installs `requirements.txt` — CI (`ci.yml`) and
the Dockerfile both `pip install -r requirements.lock` — so the declared floor
and the installed version can disagree indefinitely with every check green.

It is not hypothetical. Dependabot #253/#254 each raised a floor
(`fastapi>=0.140.7`, `resend>=2.35.0`) and left the lock pinning the versions
below them (`fastapi==0.139.2`, `resend==2.33.0`). Both PRs went fully green,
because the suites ran against the OLD releases: CI proved the thing that had
not changed still worked, and merging either one would have left the repo
asserting a minimum it does not ship. `.github/dependabot.yml` already tells a
maintainer to regenerate the lock in the same PR — this is the part that checks.

Regenerate with:
    pip-compile --generate-hashes --output-file=requirements.lock requirements.txt

Deliberately NOT asserting the lock is minimal, ordered, or free of extra
transitive pins — that is pip-compile's business, and pinning it here would
fail on any harmless resolver change.
"""
import sys
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = ROOT / "backend" / "requirements.txt"
LOCK = ROOT / "backend" / "requirements.lock"

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


def _direct_requirements() -> list[Requirement]:
    """The human-edited direct dependencies, as parsed Requirements.

    Handles extras (`uvicorn[standard]>=0.51.0`) and any PEP 508 specifier;
    skips comments and blank lines.
    """
    out = []
    for raw in REQUIREMENTS.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(Requirement(line))
    return out


def _locked_versions() -> dict[str, str]:
    """Every `name==version` pin in the lock, keyed by canonical name.

    The lock is hash-annotated, so a pin looks like `fastapi==0.140.7 \\` with
    the hashes on continuation lines; only the first line is a pin.

    pip-compile KEEPS extras in the pin (`uvicorn[standard]==0.51.0`), so the
    bracket has to come off before canonicalizing or the one dependency in this
    file that declares an extra never matches and reads as unlocked.
    """
    out = {}
    for raw in LOCK.read_text().splitlines():
        line = raw.split("#", 1)[0].strip().rstrip("\\").strip()
        if not line or line.startswith("--") or "==" not in line:
            continue
        name, _, version = line.partition("==")
        name = name.split("[", 1)[0].strip()
        if name and version:
            out[canonicalize_name(name)] = version.strip()
    return out


def test_every_direct_dependency_is_locked():
    locked = _locked_versions()
    missing = sorted(
        r.name for r in _direct_requirements()
        if canonicalize_name(r.name) not in locked
    )
    assert not missing, (
        f"{len(missing)} direct dependency(ies) in requirements.txt have no pin in "
        f"requirements.lock: {missing}. Regenerate the lock (see this file's "
        f"docstring) — the app installs the lock, so an unlocked dependency is "
        f"simply absent at runtime.")


def test_every_locked_version_satisfies_its_declared_floor():
    locked = _locked_versions()
    violations = []
    for req in _direct_requirements():
        pinned = locked.get(canonicalize_name(req.name))
        if pinned is None:
            continue  # reported by the previous contract
        if not req.specifier.contains(Version(pinned), prereleases=True):
            violations.append(f"{req.name}{req.specifier} but locked at {pinned}")
    assert not violations, (
        f"{len(violations)} dependency(ies) are pinned BELOW the floor "
        f"requirements.txt declares: {violations}. The lock is what actually gets "
        f"installed, so the floor is currently a claim nothing backs. Regenerate "
        f"the lock in the same PR that moves a floor.")


def test_the_parser_actually_reads_both_files():
    """Guards the two contracts above from passing vacuously.

    Both are 'no violations found' assertions, so an empty parse — a renamed
    file, a format change that matches nothing — satisfies them while checking
    nothing at all.
    """
    direct = _direct_requirements()
    locked = _locked_versions()
    assert len(direct) >= 5, (
        f"parsed only {len(direct)} direct requirement(s) from {REQUIREMENTS} — "
        f"the parser is not reading the file it thinks it is.")
    assert len(locked) >= len(direct), (
        f"parsed {len(locked)} pin(s) from {LOCK} but {len(direct)} direct "
        f"requirement(s) — a lock has at least one pin per direct dependency, so "
        f"the lock parser is broken.")


def run():
    print("requirements.lock satisfies requirements.txt:")
    check("every direct dependency is locked", test_every_direct_dependency_is_locked)
    check("every locked version satisfies its floor",
          test_every_locked_version_satisfies_its_declared_floor)
    check("the parsers are not reading empty", test_the_parser_actually_reads_both_files)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL REQUIREMENTS-LOCK TESTS PASSED")


if __name__ == "__main__":
    run()
