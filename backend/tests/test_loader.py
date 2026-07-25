"""Loader contract for scripts/build_ipeds_db.py — mdb-export failure handling.

The regression this exists to catch: `stream_table` used to ignore
`mdb-export`'s exit status entirely, so a failed extraction was indistinguishable
from an empty table. A whole survey family could load ZERO rows (or a truncated
prefix) and the build would still report success. On a FIRST build there is no
prior dataset for `importer.integrity_checks`' shrink detector to compare
against, so nothing downstream catches it either — the wrong data just ships.

The subtlety, and why the fix is not simply "check returncode": `header_only`
deliberately abandons the stream after one row to probe a table's columns. That
closes the pipe, mdb-export dies of SIGPIPE, and its exit status is non-zero
through no fault of the data. An abandoned stream must therefore stay silent
while a DRAINED one that ended badly must raise.

Runs without mdbtools installed: subprocess.Popen is faked at the module seam.
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root -> `scripts.*`

from scripts import build_ipeds_db as bld

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


class _FakeProc:
    """Enough of Popen for stream_table: a stdout pipe and a deferred exit code."""

    def __init__(self, out: str, rc: int):
        self.stdout = io.StringIO(out)
        self._rc = rc
        self.returncode = None
        self.killed = False

    def wait(self):
        # A real process's returncode is only known once it's reaped.
        if self.returncode is None:
            self.returncode = self._rc
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class _fake_popen:
    """Context manager that swaps build_ipeds_db's Popen for one canned process."""

    def __init__(self, out: str, rc: int):
        self.proc = _FakeProc(out, rc)

    def __enter__(self):
        self._real = bld.subprocess.Popen
        bld.subprocess.Popen = lambda *a, **k: self.proc
        return self.proc

    def __exit__(self, *exc):
        bld.subprocess.Popen = self._real
        return False


GOOD_CSV = "UNITID,INSTNM\n100654,Alabama A & M University\n100663,UAB\n"


def test_clean_export_yields_every_row():
    with _fake_popen(GOOD_CSV, rc=0):
        header, rows = bld.stream_table("x.accdb", "hd")
        got = list(rows)
    assert header == ["unitid", "instnm"], header
    assert len(got) == 2, got


def test_failed_export_with_no_output_raises_instead_of_reporting_an_empty_table():
    """THE regression: rc!=0 and no output silently became a 0-row table.

    mdb-export writes nothing when it can't open the file or the table is
    misnamed. The old code caught StopIteration on the header and returned
    ([], iter(())) — the exact shape of a legitimately empty table.
    """
    raised = None
    with _fake_popen("", rc=1):
        try:
            bld.stream_table("broken.accdb", "c_a")
        except Exception as e:  # noqa: BLE001 - asserting the type below
            raised = e
    assert isinstance(raised, RuntimeError), f"expected RuntimeError, got {raised!r}"
    assert "c_a" in str(raised), f"error must name the table, got: {raised}"


def test_export_that_dies_midway_raises_when_the_stream_is_drained():
    """A partial extraction is worse than none — it looks like real data."""
    raised = None
    with _fake_popen(GOOD_CSV, rc=2):
        header, rows = bld.stream_table("x.accdb", "c_a")
        try:
            list(rows)
        except Exception as e:  # noqa: BLE001 - asserting the type below
            raised = e
    assert header == ["unitid", "instnm"], header
    assert isinstance(raised, RuntimeError), f"expected RuntimeError, got {raised!r}"
    assert "c_a" in str(raised), f"error must name the table, got: {raised}"


def test_header_only_stays_silent_when_it_abandons_the_stream():
    """Guards the FIX from breaking the schema probe.

    header_only reads one row and breaks; the pipe closes and mdb-export exits
    non-zero from SIGPIPE. That is normal, not a failure — raising here would
    make every table-shape probe throw and no build would ever start.
    """
    with _fake_popen(GOOD_CSV, rc=-13):  # SIGPIPE
        header = bld.header_only("x.accdb", "hd")
    assert header == ["unitid", "instnm"], header


def test_an_empty_table_that_exports_cleanly_is_still_empty_not_an_error():
    """A header with no data rows is legitimate — only a bad exit status is not."""
    with _fake_popen("UNITID,INSTNM\n", rc=0):
        header, rows = bld.stream_table("x.accdb", "hd")
        got = list(rows)
    assert header == ["unitid", "instnm"], header
    assert got == [], got


def run():
    print("Loader (scripts/build_ipeds_db.py) contract")
    check("a clean export yields every row",
          test_clean_export_yields_every_row)
    check("a failed export with no output raises, never a silent empty table",
          test_failed_export_with_no_output_raises_instead_of_reporting_an_empty_table)
    check("an export that dies midway raises when drained",
          test_export_that_dies_midway_raises_when_the_stream_is_drained)
    check("header_only stays silent when it abandons the stream",
          test_header_only_stays_silent_when_it_abandons_the_stream)
    check("a cleanly-exported empty table is empty, not an error",
          test_an_empty_table_that_exports_cleanly_is_still_empty_not_an_error)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL LOADER TESTS PASSED")


if __name__ == "__main__":
    run()
