"""Contract for .githooks/pre-push's deletion-only guard.

The hook skips the local CI gate when a push deletes branches and ships no
code. The regression that matters is the *unsafe* direction: a mixed or
ordinary push silently skipping the gate would turn the pre-push check into a
no-op without anyone noticing (the push still succeeds, so nothing fails
loudly). The reverse — running the full suite to delete a merged branch — is
the minutes-long stall this guard was added to remove.

Runs the real hook with PRE_PUSH_GATE pointed at a marker script, so no test
here ever invokes scripts/run_ci_local.sh.
"""
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / ".githooks" / "pre-push"

ZERO = "0" * 40
SHA_A = "9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c"
SHA_B = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


def run_hook(stdin_text: str, gate_exit: int = 0):
    """Run the hook with a stub gate; return (returncode, gate_ran)."""
    d = Path(tempfile.mkdtemp())
    marker = d / "gate-ran"
    gate = d / "fake-gate.sh"
    gate.write_text(f"#!/usr/bin/env bash\ntouch {marker}\nexit {gate_exit}\n")
    gate.chmod(gate.stat().st_mode | stat.S_IEXEC)

    env = {**os.environ, "PRE_PUSH_GATE": str(gate)}
    proc = subprocess.run(
        ["bash", str(HOOK)],
        input=stdin_text,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )
    return proc.returncode, marker.exists()


def test_single_deletion_skips_the_gate():
    rc, ran = run_hook(f"(delete) {ZERO} refs/heads/chore/old {SHA_A}\n")
    assert rc == 0, f"deletion push should succeed, got rc={rc}"
    assert not ran, "gate ran for a pure branch deletion"


def test_multiple_deletions_skip_the_gate():
    stdin = (
        f"(delete) {ZERO} refs/heads/chore/old {SHA_A}\n"
        f"(delete) {ZERO} refs/heads/docs/old {SHA_B}\n"
    )
    rc, ran = run_hook(stdin)
    assert rc == 0, f"multi-deletion push should succeed, got rc={rc}"
    assert not ran, "gate ran for a deletion-only push of several branches"


def test_ordinary_push_still_runs_the_gate():
    rc, ran = run_hook(f"refs/heads/main {SHA_A} refs/heads/main {SHA_B}\n")
    assert rc == 0, f"expected gate's own exit 0, got rc={rc}"
    assert ran, "gate was SKIPPED for an ordinary code push"


def test_mixed_delete_and_content_still_runs_the_gate():
    """The unsafe case: one deletion must not excuse the code pushed alongside."""
    stdin = (
        f"(delete) {ZERO} refs/heads/chore/old {SHA_A}\n"
        f"refs/heads/main {SHA_B} refs/heads/main {SHA_A}\n"
    )
    rc, ran = run_hook(stdin)
    assert rc == 0, f"expected gate's own exit 0, got rc={rc}"
    assert ran, "gate was SKIPPED for a push mixing a deletion with real commits"


def test_empty_stdin_falls_through_to_the_gate():
    """No ref lines is the unknown case — fail safe by testing, not skipping."""
    for stdin in ("", "\n", "   \n"):
        rc, ran = run_hook(stdin)
        assert rc == 0, f"expected gate's own exit 0, got rc={rc}"
        assert ran, f"gate was SKIPPED on unreadable stdin {stdin!r}"


def test_failing_gate_still_aborts_the_push():
    rc, ran = run_hook(f"refs/heads/main {SHA_A} refs/heads/main {SHA_B}\n", gate_exit=1)
    assert ran, "gate did not run"
    assert rc != 0, "a red gate must abort the push (non-zero exit)"


def run():
    print("pre-push deletion-only guard:")
    check("single deletion skips the gate", test_single_deletion_skips_the_gate)
    check("multiple deletions skip the gate", test_multiple_deletions_skip_the_gate)
    check("ordinary push still runs the gate", test_ordinary_push_still_runs_the_gate)
    check("delete + content still runs the gate", test_mixed_delete_and_content_still_runs_the_gate)
    check("empty stdin falls through to the gate", test_empty_stdin_falls_through_to_the_gate)
    check("failing gate still aborts the push", test_failing_gate_still_aborts_the_push)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL PRE-PUSH HOOK TESTS PASSED")


if __name__ == "__main__":
    run()
