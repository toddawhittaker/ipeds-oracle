"""NCES catalog/fetch contract (app/nces.py) — the SSRF-hardened, network-free
seam the year-catalog Imports feature is built on.

Everything here is network-free: httpx is exercised only through
httpx.MockTransport (a fake transport that answers requests in-process — no
socket, no DNS, no real nces.ed.gov). No API key needed.

Contracts pinned:
  * `_zip_url(start_year, release)` builds the exact NCES zip URL and rejects
    anything that isn't a validated int year in range / a known release —
    this is the SSRF guard: URLs are built ONLY from a fixed host + this
    validated template, never from caller-supplied strings.
  * `head_release(start_year, client=None)` HEADs Final, falls back to
    Provisional, returns a 3-tuple (release, url, zip_bytes) — (None, None, None)
    if neither exists — and refuses to trust a redirect that resolves off
    nces.ed.gov. zip_bytes is the HEAD response's declared Content-Length as
    an int, or None if the server didn't send one.
  * `probe_catalog(refresh=False)` builds one entry per start year (each now
    also carrying "zip_bytes") and caches it (TTL) so repeated calls don't
    re-HEAD the whole catalog. Probes run CONCURRENTLY (a ThreadPoolExecutor,
    default width 5) but the returned list is always re-sorted ascending by
    start_year regardless of completion order.
  * `download_zip(url, dest, max_bytes, client=None, *, on_progress=None,
    deadline_seconds=None)` enforces the byte cap both from a declared
    Content-Length AND from the actual running byte count mid-stream (a
    lying/missing header must not bypass the cap), calls `on_progress(written,
    total)` with CUMULATIVE bytes as each chunk arrives, aborts (raising) if
    `deadline_seconds` elapses mid-stream, and cleans up any partial file it
    started in every failure case (cap, deadline, or otherwise).
  * `extract_accdb(zip_path, out_dir, expected_start_year)` picks the one
    accdb member out of a zip, ignores other file types, normalizes the
    output filename's case, guards against zip-slip member names, enforces a
    size cap on the declared (uncompressed) member size before extracting
    (zip-bomb guard), and rejects a member whose embedded year doesn't match
    what was requested.
  * `fetch_year(start_year, work_dir, *, on_progress=None)` orchestrates the
    three steps above for one year and returns a 2-tuple (accdb_path, release)
    (or raises if the year isn't available from NCES at all).

Assumptions this pins for the implementer (see also the test-engineer's report):
  * `head_release`/`download_zip`/`probe_catalog` accept an optional
    `client: httpx.Client | None = None` kwarg for dependency injection —
    tests construct a client backed by httpx.MockTransport and pass it in.
  * `probe_catalog` exposes a test seam `_clear_catalog_cache()` to reset its
    module-level TTL cache between tests.
  * `extract_accdb` accepts an optional `max_extract_bytes: int` kwarg
    (default = a real sane cap) so the zip-bomb guard can be exercised with a
    tiny zip instead of a genuinely huge one.
  * `fetch_year` calls `head_release`, `download_zip`, `extract_accdb` as
    plain module-level names (not captured references), so tests can
    monkeypatch each independently — mirrors the existing importer.py
    monkeypatch convention (see eval/test_importer.py).
"""
import os
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

tmp = tempfile.mkdtemp()
os.environ["APP_DB_PATH"] = str(Path(tmp) / "app.db")
os.environ["ADMIN_EMAILS"] = "admin@franklin.edu"
os.environ["COOKIE_SECURE"] = "false"
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["RESEND_API_KEY"] = ""

import httpx  # noqa: E402

from app import nces  # noqa: E402

FAILURES = []
CURRENT_YEAR = datetime.now().year


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


def _assert_raises(fn, exc_types=Exception, note=""):
    try:
        fn()
    except exc_types:
        return
    except Exception as e:  # wrong exception type — surface it, don't hide it
        raise AssertionError(
            f"{note} raised {type(e).__name__} (expected {exc_types}): {e}") from e
    raise AssertionError(note or f"expected {exc_types} to be raised, but nothing was")


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def _make_zip(path, members: dict):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


# ---------------------------------------------------------------------------
# _zip_url — SSRF guard: URLs built ONLY from fixed host + validated inputs
# ---------------------------------------------------------------------------

def test_zip_url_builds_exact_string_for_a_valid_year():
    assert nces._zip_url(2023, "Final") == (
        "https://nces.ed.gov/ipeds/tablefiles/zipfiles/IPEDS_2023-24_Final.zip")
    assert nces._zip_url(2023, "Provisional") == (
        "https://nces.ed.gov/ipeds/tablefiles/zipfiles/IPEDS_2023-24_Provisional.zip")


def test_zip_url_accepts_the_full_valid_boundary_range():
    # Lower bound (2004) and upper bound (current_year+1) must both be valid.
    assert "IPEDS_2004-05_Final.zip" in nces._zip_url(2004, "Final")
    assert f"IPEDS_{CURRENT_YEAR + 1}-{str(CURRENT_YEAR + 2)[-2:]}_Final.zip" in \
        nces._zip_url(CURRENT_YEAR + 1, "Final")


def test_zip_url_rejects_non_int_year():
    _assert_raises(lambda: nces._zip_url("2023", "Final"), (ValueError, AssertionError, TypeError))
    _assert_raises(lambda: nces._zip_url(2023.0, "Final"), (ValueError, AssertionError, TypeError))
    _assert_raises(lambda: nces._zip_url(None, "Final"), (ValueError, AssertionError, TypeError))


def test_zip_url_rejects_year_below_2004():
    _assert_raises(lambda: nces._zip_url(2003, "Final"), (ValueError, AssertionError))


def test_zip_url_rejects_year_beyond_current_plus_one():
    _assert_raises(lambda: nces._zip_url(CURRENT_YEAR + 2, "Final"), (ValueError, AssertionError))


def test_zip_url_rejects_unknown_release():
    _assert_raises(lambda: nces._zip_url(2023, "final"), (ValueError, AssertionError))
    _assert_raises(lambda: nces._zip_url(2023, "Draft"), (ValueError, AssertionError))
    _assert_raises(lambda: nces._zip_url(2023, ""), (ValueError, AssertionError))


# ---------------------------------------------------------------------------
# head_release — Final -> Provisional fallback, SSRF redirect guard
# ---------------------------------------------------------------------------

def test_head_release_final_available():
    def handler(request):
        assert request.method == "HEAD"
        return httpx.Response(200, headers={"content-length": "123456"})
    release, url, zip_bytes = nces.head_release(2023, client=_client(handler))
    assert release == "Final", release
    assert url == nces._zip_url(2023, "Final"), url
    assert zip_bytes == 123456, zip_bytes


def test_head_release_missing_content_length_gives_none_zip_bytes():
    def handler(request):
        return httpx.Response(200)  # no content-length header at all
    release, url, zip_bytes = nces.head_release(2023, client=_client(handler))
    assert release == "Final", release
    assert zip_bytes is None, zip_bytes


def test_head_release_falls_back_to_provisional():
    def handler(request):
        if "Final" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-length": "999"})
    release, url, zip_bytes = nces.head_release(2023, client=_client(handler))
    assert release == "Provisional", release
    assert url == nces._zip_url(2023, "Provisional"), url
    assert zip_bytes == 999, zip_bytes


def test_head_release_neither_available_returns_none_triple():
    def handler(request):
        return httpx.Response(404)
    release, url, zip_bytes = nces.head_release(2099, client=_client(handler))
    assert release is None, release
    assert url is None, url
    assert zip_bytes is None, zip_bytes


def test_head_release_rejects_redirect_off_nces_host():
    def handler(request):
        if request.url.host == "nces.ed.gov":
            return httpx.Response(302, headers={"location": "https://evil.example.com/x.zip"})
        return httpx.Response(200)
    _assert_raises(lambda: nces.head_release(2023, client=_client(handler)),
                   note="redirect to a non-nces host must raise")


# ---------------------------------------------------------------------------
# probe_catalog — one entry per start year + TTL cache. Probes run
# CONCURRENTLY (ThreadPoolExecutor), so fakes below guard their shared call
# counter with a lock — the assertions tolerate any completion order but the
# returned catalog list itself must always come back sorted ascending by
# start_year.
# ---------------------------------------------------------------------------

def test_probe_catalog_covers_full_range_and_shape():
    import threading
    nces._clear_catalog_cache()
    lock = threading.Lock()
    calls = {"n": 0}

    def fake_head(start_year, client=None):
        with lock:
            calls["n"] += 1
        if start_year == 2023:
            return "Provisional", nces._zip_url(start_year, "Provisional"), 42_000_000
        if start_year % 2 == 0:
            return "Final", nces._zip_url(start_year, "Final"), 10_000_000
        return None, None, None

    orig = nces.head_release
    nces.head_release = fake_head
    try:
        cat = nces.probe_catalog()
    finally:
        nces.head_release = orig
        nces._clear_catalog_cache()

    expected_years = list(range(2004, CURRENT_YEAR + 2))
    # Ascending order is required REGARDLESS of which thread finished first.
    assert [e["start_year"] for e in cat] == expected_years, \
        [e["start_year"] for e in cat]
    assert calls["n"] == len(expected_years), calls

    e2023 = next(e for e in cat if e["start_year"] == 2023)
    assert e2023["year_label"] == "2023-24", e2023
    assert e2023["year"] == 2024, e2023
    assert e2023["available"] is True, e2023
    assert e2023["release"] == "Provisional", e2023
    assert e2023["zip_bytes"] == 42_000_000, e2023

    e_odd_unavailable = next(e for e in cat if e["start_year"] % 2 != 0 and e["start_year"] != 2023)
    assert e_odd_unavailable["available"] is False, e_odd_unavailable
    assert e_odd_unavailable["release"] is None, e_odd_unavailable
    assert e_odd_unavailable["zip_bytes"] is None, e_odd_unavailable


def test_probe_catalog_runs_concurrently_by_default():
    # A fake head_release that BLOCKS until it's been entered by several
    # threads at once proves probe_catalog isn't looping start years one at a
    # time — if it were sequential, this would deadlock/timeout instead of
    # completing quickly. Bounded by the default concurrency (>= 2 in flight
    # at once is enough to prove it's not serial without hard-coding "5").
    import threading
    import time as time_mod
    nces._clear_catalog_cache()
    in_flight = {"n": 0, "max": 0}
    lock = threading.Lock()

    def fake_head(start_year, client=None):
        with lock:
            in_flight["n"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["n"])
        time_mod.sleep(0.01)
        with lock:
            in_flight["n"] -= 1
        return "Final", nces._zip_url(start_year, "Final"), 1

    orig = nces.head_release
    nces.head_release = fake_head
    try:
        nces.probe_catalog()
    finally:
        nces.head_release = orig
        nces._clear_catalog_cache()

    assert in_flight["max"] >= 2, \
        f"expected concurrent probing (>=2 in flight at once), got max={in_flight['max']}"


def test_probe_catalog_caches_until_refresh():
    import threading
    nces._clear_catalog_cache()
    lock = threading.Lock()
    calls = {"n": 0}

    def fake_head(start_year, client=None):
        with lock:
            calls["n"] += 1
        return "Final", nces._zip_url(start_year, "Final"), 1

    orig = nces.head_release
    nces.head_release = fake_head
    try:
        cat1 = nces.probe_catalog()
        first_count = calls["n"]
        assert first_count == len(cat1), \
            "probe_catalog must call head_release exactly once per year"

        cat2 = nces.probe_catalog()  # no refresh -> must be a pure cache hit
        assert calls["n"] == first_count, \
            "a second probe_catalog() call without refresh=True must not re-HEAD"
        assert cat2 == cat1, "cached result must be returned unchanged"

        nces.probe_catalog(refresh=True)
        assert calls["n"] > first_count, "refresh=True must bypass the cache"
    finally:
        nces.head_release = orig
        nces._clear_catalog_cache()


# ---------------------------------------------------------------------------
# download_zip — Content-Length pre-check + mid-stream running-byte-count cap
# ---------------------------------------------------------------------------

def test_download_zip_happy_path_writes_the_file():
    content = b"small-fake-zip-bytes"

    def handler(request):
        return httpx.Response(200, content=content)

    dest = Path(tempfile.mkdtemp()) / "out.zip"
    nces.download_zip("https://nces.ed.gov/x.zip", dest, max_bytes=1_000_000,
                      client=_client(handler))
    assert dest.exists(), "downloaded file must be written"
    assert dest.read_bytes() == content, dest.read_bytes()


def test_download_zip_rejects_oversized_content_length_header():
    def handler(request):
        return httpx.Response(200, headers={"content-length": "2000000"})

    dest = Path(tempfile.mkdtemp()) / "out.zip"
    _assert_raises(lambda: nces.download_zip(
        "https://nces.ed.gov/x.zip", dest, max_bytes=1_000_000, client=_client(handler)),
        note="an oversized declared Content-Length must be rejected before streaming")
    assert not dest.exists(), "no partial file should be left behind"


def test_download_zip_aborts_mid_stream_with_no_content_length_header():
    def gen():
        chunk = b"a" * 100_000
        for _ in range(20):  # 2,000,000 bytes total, no length declared up front
            yield chunk

    def handler(request):
        return httpx.Response(200, content=gen())  # -> chunked, no content-length

    dest = Path(tempfile.mkdtemp()) / "out.zip"
    _assert_raises(lambda: nces.download_zip(
        "https://nces.ed.gov/x.zip", dest, max_bytes=50_000, client=_client(handler)),
        note="an oversized stream with no Content-Length must still be aborted mid-stream")
    assert not dest.exists(), "partial file must be deleted after a mid-stream abort"


def test_download_zip_aborts_mid_stream_with_underreported_content_length():
    def gen():
        chunk = b"a" * 100_000
        for _ in range(20):
            yield chunk

    def handler(request):
        # Lies: claims 10 bytes but the stream actually produces 2,000,000.
        return httpx.Response(200, headers={"content-length": "10"}, content=gen())

    dest = Path(tempfile.mkdtemp()) / "out.zip"
    _assert_raises(lambda: nces.download_zip(
        "https://nces.ed.gov/x.zip", dest, max_bytes=50_000, client=_client(handler)),
        note="an underreported Content-Length must not let the stream bypass the cap")
    assert not dest.exists(), "partial file must be deleted after a mid-stream abort"


# ---------------------------------------------------------------------------
# download_zip — on_progress (cumulative bytes) + deadline_seconds abort
# ---------------------------------------------------------------------------

def test_download_zip_on_progress_reports_cumulative_bytes():
    chunks = [b"a" * 10, b"b" * 15, b"c" * 5]
    total_len = sum(len(c) for c in chunks)

    def gen():
        yield from chunks

    def handler(request):
        return httpx.Response(200, headers={"content-length": str(total_len)}, content=gen())

    calls = []
    dest = Path(tempfile.mkdtemp()) / "out.zip"
    nces.download_zip("https://nces.ed.gov/x.zip", dest, max_bytes=1_000_000,
                      client=_client(handler),
                      on_progress=lambda written, total: calls.append((written, total)))
    assert dest.exists()
    assert calls, "on_progress must fire at least once"
    written_vals = [c[0] for c in calls]
    assert written_vals == sorted(written_vals), \
        f"on_progress must report cumulative (non-decreasing) bytes, got {written_vals}"
    assert written_vals[-1] == total_len, \
        f"final on_progress call must report the full byte count, got {written_vals}"
    assert all(total == total_len for _, total in calls), \
        f"total must be the declared Content-Length on every call, got {calls}"


def test_download_zip_on_progress_total_is_none_without_content_length():
    def gen():
        yield b"x" * 10
        yield b"y" * 10

    def handler(request):
        return httpx.Response(200, content=gen())  # chunked, no content-length

    calls = []
    dest = Path(tempfile.mkdtemp()) / "out.zip"
    nces.download_zip("https://nces.ed.gov/x.zip", dest, max_bytes=1_000_000,
                      client=_client(handler),
                      on_progress=lambda written, total: calls.append((written, total)))
    assert calls, "on_progress must fire at least once"
    assert all(total is None for _, total in calls), \
        f"total must be None when no Content-Length was declared, got {calls}"


def test_download_zip_mid_stream_abort_still_fires_progress_and_cleans_up():
    def gen():
        chunk = b"a" * 100_000
        for _ in range(20):
            yield chunk

    def handler(request):
        return httpx.Response(200, content=gen())

    calls = []
    dest = Path(tempfile.mkdtemp()) / "out.zip"
    _assert_raises(lambda: nces.download_zip(
        "https://nces.ed.gov/x.zip", dest, max_bytes=50_000, client=_client(handler),
        on_progress=lambda written, total: calls.append((written, total))),
        note="a mid-stream cap abort must still raise with on_progress set")
    assert not dest.exists(), "partial file must be deleted after the abort"
    assert calls, "on_progress should have fired at least once before the abort"


def test_download_zip_deadline_aborts_mid_stream_and_leaves_no_partial():
    # A multi-chunk stream that would otherwise finish fine under max_bytes,
    # but a monkeypatched time.monotonic() jumps far past the deadline right
    # after the first reading — deterministic, no wall-clock sleep needed.
    def gen():
        chunk = b"a" * 1_000
        for _ in range(50):  # 50,000 bytes total — well under max_bytes
            yield chunk

    def handler(request):
        return httpx.Response(200, content=gen())

    monotonic_calls = {"n": 0}

    def fake_monotonic():
        monotonic_calls["n"] += 1
        # First call establishes the start reference at t=0; every call after
        # that reads as if 10,000 seconds have elapsed — far past any small
        # deadline, regardless of exactly how many checks the implementation
        # performs per chunk.
        return 0.0 if monotonic_calls["n"] == 1 else 10_000.0

    dest = Path(tempfile.mkdtemp()) / "out.zip"
    orig_monotonic = nces.time.monotonic
    nces.time.monotonic = fake_monotonic
    try:
        _assert_raises(lambda: nces.download_zip(
            "https://nces.ed.gov/x.zip", dest, max_bytes=10_000_000,
            client=_client(handler), deadline_seconds=5.0),
            note="a download running past deadline_seconds must abort mid-stream")
    finally:
        nces.time.monotonic = orig_monotonic
    assert not dest.exists(), "partial file must be deleted after a deadline abort"
    assert monotonic_calls["n"] >= 2, \
        "fake_monotonic was never consulted — deadline_seconds must be checked " \
        "against time.monotonic() during the stream"


def test_download_zip_no_deadline_by_default_lets_a_slow_stream_finish():
    # deadline_seconds defaults to None -> no deadline enforcement at all.
    # Uses the SAME multi-chunk shape as the deadline-abort test above but
    # without monkeypatching time, to prove the default is "no deadline".
    def gen():
        chunk = b"a" * 1_000
        for _ in range(5):
            yield chunk

    def handler(request):
        return httpx.Response(200, content=gen())

    dest = Path(tempfile.mkdtemp()) / "out.zip"
    nces.download_zip("https://nces.ed.gov/x.zip", dest, max_bytes=10_000_000,
                      client=_client(handler))
    assert dest.exists()
    assert dest.stat().st_size == 5_000, dest.stat().st_size


# ---------------------------------------------------------------------------
# extract_accdb — member selection, case normalization, zip-slip, zip-bomb
# ---------------------------------------------------------------------------

def test_extract_accdb_selects_the_accdb_member_and_ignores_others():
    d = Path(tempfile.mkdtemp())
    zpath = d / "IPEDS_2000-01_Final.zip"
    _make_zip(zpath, {
        "IPEDS200001.accdb": b"fake-accdb-bytes",
        "ReadMe.docx": b"docx-bytes",
        "Frequencies.xlsx": b"xlsx-bytes",
    })
    out_dir = Path(tempfile.mkdtemp())
    result = nces.extract_accdb(zpath, out_dir, expected_start_year=2000)
    assert result == out_dir / "IPEDS200001.accdb", result
    assert result.read_bytes() == b"fake-accdb-bytes"
    assert not (out_dir / "ReadMe.docx").exists()
    assert not (out_dir / "Frequencies.xlsx").exists()


def test_extract_accdb_normalizes_filename_case():
    d = Path(tempfile.mkdtemp())
    zpath = d / "z.zip"
    _make_zip(zpath, {"ipeds200001.accdb": b"lowercase-member-bytes"})
    out_dir = Path(tempfile.mkdtemp())
    result = nces.extract_accdb(zpath, out_dir, expected_start_year=2000)
    assert result.name == "IPEDS200001.accdb", result.name
    assert result.read_bytes() == b"lowercase-member-bytes"


def test_extract_accdb_rejects_zip_slip_member():
    d = Path(tempfile.mkdtemp())
    zpath = d / "z.zip"
    _make_zip(zpath, {
        "IPEDS200001.accdb": b"legit-bytes",
        "../evil.accdb": b"slip-attempt-bytes",
        "ReadMe.docx": b"docx-bytes",
    })
    out_dir = Path(tempfile.mkdtemp())
    _assert_raises(lambda: nces.extract_accdb(zpath, out_dir, expected_start_year=2000),
                   note="a zip containing a path-traversal member name must be rejected")
    assert not any(out_dir.iterdir()), "nothing should be extracted from a rejected zip"


def test_extract_accdb_rejects_zip_bomb_by_declared_size():
    d = Path(tempfile.mkdtemp())
    zpath = d / "z.zip"
    _make_zip(zpath, {"IPEDS200001.accdb": b"x" * 2000})
    out_dir = Path(tempfile.mkdtemp())
    _assert_raises(lambda: nces.extract_accdb(
        zpath, out_dir, expected_start_year=2000, max_extract_bytes=100),
        note="a member declaring more bytes than the cap must be rejected before extracting")
    assert not any(out_dir.iterdir()), "nothing should be extracted when the size guard trips"


def test_extract_accdb_no_accdb_member_raises():
    d = Path(tempfile.mkdtemp())
    zpath = d / "z.zip"
    _make_zip(zpath, {"ReadMe.docx": b"docx-bytes", "Frequencies.xlsx": b"xlsx-bytes"})
    out_dir = Path(tempfile.mkdtemp())
    _assert_raises(lambda: nces.extract_accdb(zpath, out_dir, expected_start_year=2000),
                   note="a zip with no accdb member must raise")


def test_extract_accdb_year_mismatch_raises():
    d = Path(tempfile.mkdtemp())
    zpath = d / "z.zip"
    _make_zip(zpath, {"IPEDS202526.accdb": b"wrong-year-bytes"})
    out_dir = Path(tempfile.mkdtemp())
    _assert_raises(lambda: nces.extract_accdb(zpath, out_dir, expected_start_year=2000),
                   note="a member whose embedded year doesn't match expected_start_year must raise")


# ---------------------------------------------------------------------------
# fetch_year — orchestrates head_release -> download_zip -> extract_accdb
# ---------------------------------------------------------------------------

def test_fetch_year_orchestrates_the_three_steps_in_order():
    calls = []

    def fake_head(start_year, client=None):
        calls.append(("head", start_year))
        return "Final", nces._zip_url(start_year, "Final"), 12_345

    def fake_download(url, dest, max_bytes, client=None, on_progress=None, deadline_seconds=None):
        calls.append(("download", url))
        if on_progress is not None:
            on_progress(14, 14)  # fetch_year must be able to pass this through
        Path(dest).write_bytes(b"fake-zip-bytes")
        return Path(dest)

    def fake_extract(zip_path, out_dir, expected_start_year, **kw):
        calls.append(("extract", str(zip_path), expected_start_year))
        out = Path(out_dir) / f"IPEDS{expected_start_year}{str(expected_start_year + 1)[-2:]}.accdb"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake-accdb-bytes")
        return out

    orig_head, orig_download, orig_extract = (
        nces.head_release, nces.download_zip, nces.extract_accdb)
    nces.head_release = fake_head
    nces.download_zip = fake_download
    nces.extract_accdb = fake_extract
    try:
        work_dir = Path(tempfile.mkdtemp())
        progress_calls = []
        result, release = nces.fetch_year(
            2023, work_dir, on_progress=lambda w, t: progress_calls.append((w, t)))
    finally:
        nces.head_release, nces.download_zip, nces.extract_accdb = (
            orig_head, orig_download, orig_extract)

    assert result == work_dir / "IPEDS202324.accdb", result
    assert result.exists() and result.read_bytes() == b"fake-accdb-bytes"
    assert release == "Final", release
    assert [c[0] for c in calls] == ["head", "download", "extract"], calls
    assert progress_calls == [(14, 14)], \
        "fetch_year must pass its on_progress kwarg through to download_zip"


def test_fetch_year_works_without_an_on_progress_callback():
    def fake_head(start_year, client=None):
        return "Final", nces._zip_url(start_year, "Final"), 1

    def fake_download(url, dest, max_bytes, client=None, on_progress=None, deadline_seconds=None):
        Path(dest).write_bytes(b"fake-zip-bytes")
        return Path(dest)

    def fake_extract(zip_path, out_dir, expected_start_year, **kw):
        out = Path(out_dir) / f"IPEDS{expected_start_year}{str(expected_start_year + 1)[-2:]}.accdb"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake-accdb-bytes")
        return out

    orig_head, orig_download, orig_extract = (
        nces.head_release, nces.download_zip, nces.extract_accdb)
    nces.head_release = fake_head
    nces.download_zip = fake_download
    nces.extract_accdb = fake_extract
    try:
        work_dir = Path(tempfile.mkdtemp())
        result, release = nces.fetch_year(2023, work_dir)
    finally:
        nces.head_release, nces.download_zip, nces.extract_accdb = (
            orig_head, orig_download, orig_extract)
    assert result == work_dir / "IPEDS202324.accdb", result
    assert release == "Final", release


def test_fetch_year_raises_when_year_unavailable():
    orig_head = nces.head_release
    nces.head_release = lambda start_year, client=None: (None, None, None)
    try:
        work_dir = Path(tempfile.mkdtemp())
        _assert_raises(lambda: nces.fetch_year(2099, work_dir),
                       note="fetch_year must raise when NCES has neither release for the year")
    finally:
        nces.head_release = orig_head


def run():
    print("nces contract:")
    check("_zip_url builds the exact URL string for a valid year",
          test_zip_url_builds_exact_string_for_a_valid_year)
    check("_zip_url accepts the full valid boundary range",
          test_zip_url_accepts_the_full_valid_boundary_range)
    check("_zip_url rejects a non-int year",
          test_zip_url_rejects_non_int_year)
    check("_zip_url rejects a year below 2004",
          test_zip_url_rejects_year_below_2004)
    check("_zip_url rejects a year beyond current_year+1",
          test_zip_url_rejects_year_beyond_current_plus_one)
    check("_zip_url rejects an unknown release string",
          test_zip_url_rejects_unknown_release)
    check("head_release returns Final + zip_bytes when it HEADs 200",
          test_head_release_final_available)
    check("head_release returns zip_bytes=None with no Content-Length header",
          test_head_release_missing_content_length_gives_none_zip_bytes)
    check("head_release falls back to Provisional when Final 404s",
          test_head_release_falls_back_to_provisional)
    check("head_release returns (None, None, None) when neither release exists",
          test_head_release_neither_available_returns_none_triple)
    check("head_release rejects a redirect that resolves off nces.ed.gov",
          test_head_release_rejects_redirect_off_nces_host)
    check("probe_catalog covers the full start-year range with the right shape + zip_bytes",
          test_probe_catalog_covers_full_range_and_shape)
    check("probe_catalog probes concurrently by default",
          test_probe_catalog_runs_concurrently_by_default)
    check("probe_catalog caches until refresh=True",
          test_probe_catalog_caches_until_refresh)
    check("download_zip writes the file on the happy path",
          test_download_zip_happy_path_writes_the_file)
    check("download_zip rejects an oversized declared Content-Length",
          test_download_zip_rejects_oversized_content_length_header)
    check("download_zip aborts mid-stream with no Content-Length header",
          test_download_zip_aborts_mid_stream_with_no_content_length_header)
    check("download_zip aborts mid-stream despite an underreported Content-Length",
          test_download_zip_aborts_mid_stream_with_underreported_content_length)
    check("download_zip on_progress reports cumulative bytes",
          test_download_zip_on_progress_reports_cumulative_bytes)
    check("download_zip on_progress total is None without Content-Length",
          test_download_zip_on_progress_total_is_none_without_content_length)
    check("download_zip mid-stream cap abort still fires progress + cleans up",
          test_download_zip_mid_stream_abort_still_fires_progress_and_cleans_up)
    check("download_zip deadline_seconds aborts mid-stream, no partial file",
          test_download_zip_deadline_aborts_mid_stream_and_leaves_no_partial)
    check("download_zip with no deadline_seconds lets a slow stream finish",
          test_download_zip_no_deadline_by_default_lets_a_slow_stream_finish)
    check("extract_accdb selects the accdb member and ignores docx/xlsx",
          test_extract_accdb_selects_the_accdb_member_and_ignores_others)
    check("extract_accdb normalizes the output filename's case",
          test_extract_accdb_normalizes_filename_case)
    check("extract_accdb rejects a zip-slip member name",
          test_extract_accdb_rejects_zip_slip_member)
    check("extract_accdb rejects a zip-bomb by declared (uncompressed) size",
          test_extract_accdb_rejects_zip_bomb_by_declared_size)
    check("extract_accdb raises when no member matches the accdb pattern",
          test_extract_accdb_no_accdb_member_raises)
    check("extract_accdb raises when the member's year doesn't match expected_start_year",
          test_extract_accdb_year_mismatch_raises)
    check("fetch_year orchestrates head_release -> download_zip -> extract_accdb, "
          "returns (path, release), and forwards on_progress",
          test_fetch_year_orchestrates_the_three_steps_in_order)
    check("fetch_year works fine with no on_progress callback",
          test_fetch_year_works_without_an_on_progress_callback)
    check("fetch_year raises when the year isn't available from NCES",
          test_fetch_year_raises_when_year_unavailable)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL NCES TESTS PASSED")


if __name__ == "__main__":
    run()
