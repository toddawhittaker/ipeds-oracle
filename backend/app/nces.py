"""Fetch IPEDS `.accdb` releases straight from NCES (nces.ed.gov) — the network
seam behind the Admin -> Imports year catalog.

Security posture (SSRF choke point): every URL this module ever requests is
built ONLY from a fixed host + a fixed URL template (`_zip_url`), combined
with a validated integer year and a release string drawn from a closed set
({"Final", "Provisional"}). No caller-supplied string ever reaches a URL.
`head_release`/`download_zip` additionally refuse to trust a redirect that
resolves off `NCES_HOST`.

Everything here is a bare module-level function (not a class) so tests can
monkeypatch each independently (`nces.head_release = fake_head`, etc.) —
mirrors the existing `app/importer.py` monkeypatch convention.
"""
from __future__ import annotations

import re
import shutil
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin

import httpx

from app.config import get_settings

# --- Fixed SSRF-safe constants (NOT config/env/admin-overridable) -----------
NCES_BASE = "https://nces.ed.gov/ipeds/tablefiles/zipfiles"
NCES_HOST = "nces.ed.gov"
EARLIEST_START_YEAR = 2004
_RELEASES = ("Final", "Provisional")

# Matches the one .accdb member inside an NCES zip, e.g. "IPEDS202324.accdb"
# (case-insensitive — some zips ship a lowercase member name).
ACCDB_NAME_RE = re.compile(r"IPEDS(\d{4})(\d{2})\.accdb$", re.IGNORECASE)


def _zip_url(start_year: int, release: str) -> str:
    """Build the exact NCES zip URL for one start year + release. Raises on
    anything that isn't a validated int year in range / a known release —
    this is the SSRF guard."""
    if type(start_year) is not int:  # noqa: E721 — deliberately exclude bool/float/str
        raise ValueError(f"start_year must be an int, got {start_year!r}")
    current_year = datetime.now().year
    if not (EARLIEST_START_YEAR <= start_year <= current_year + 1):
        raise ValueError(
            f"start_year {start_year} is out of range "
            f"[{EARLIEST_START_YEAR}, {current_year + 1}]")
    if release not in _RELEASES:
        raise ValueError(f"unknown release {release!r} (must be one of {_RELEASES})")
    end_yy = str(start_year + 1)[-2:]
    return f"{NCES_BASE}/IPEDS_{start_year}-{end_yy}_{release}.zip"


# Manual redirect handling (SEC-6): httpx's own follow_redirects would ISSUE the
# request to an intermediate redirect target BEFORE we could inspect its host, so
# a hop off nces.ed.gov is an SSRF the final-host check can't prevent. Instead we
# follow redirects by hand with follow_redirects=False and validate EACH hop's
# host+scheme before issuing the next request. Bounded to defuse a redirect loop.
_MAX_REDIRECTS = 5


def _client(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=False)


def _validated_redirect_target(current_url: str, resp: httpx.Response) -> str:
    """Given a 3xx response to `current_url`, return the absolute URL of the next
    hop — or raise if it has no Location or resolves off https://NCES_HOST. The
    caller validates BEFORE issuing the next request, so a request is only ever
    sent to nces.ed.gov."""
    location = resp.headers.get("location")
    if not location:
        raise ValueError(f"redirect for {current_url} carried no Location header")
    nxt = httpx.URL(urljoin(current_url, location))
    if nxt.scheme != "https" or nxt.host != NCES_HOST:
        raise ValueError(
            f"redirect for {current_url} points off https://{NCES_HOST} (to {nxt})")
    return str(nxt)


def _head_following_redirects(client: httpx.Client, url: str) -> httpx.Response:
    """HEAD `url`, manually following up to _MAX_REDIRECTS validated same-host
    hops. Returns the first non-redirect response; raises on an off-host hop or a
    redirect loop that exceeds the cap."""
    for _ in range(_MAX_REDIRECTS + 1):
        resp = client.head(url, follow_redirects=False)
        if not resp.is_redirect:
            return resp
        url = _validated_redirect_target(url, resp)
    raise ValueError(f"too many redirects fetching {url}")


def head_release(
    start_year: int, client: httpx.Client | None = None,
) -> tuple[str | None, str | None, int | None]:
    """HEAD the Final release, falling back to Provisional. Returns
    (release, url, zip_bytes) for whichever exists — zip_bytes is the HEAD
    response's declared Content-Length as an int, or None if the server
    didn't send one — or (None, None, None) if neither release exists (this
    includes a start_year outside _zip_url's valid range — simply nothing to
    find, not an error). Refuses to trust a redirect that resolves off
    NCES_HOST (that DOES raise — it's a security-relevant condition, not a
    "not found")."""
    own_client = client is None
    c = client or _client(get_settings().nces_http_timeout_seconds)
    try:
        for release in _RELEASES:
            try:
                url = _zip_url(start_year, release)
            except ValueError:
                return None, None, None
            resp = _head_following_redirects(c, url)
            resolved_host = resp.url.host
            if resolved_host != NCES_HOST:  # defense-in-depth; hops already validated
                raise ValueError(
                    f"redirect for {url} resolved off {NCES_HOST} (to {resolved_host})")
            if resp.status_code == 200:
                declared = resp.headers.get("content-length")
                zip_bytes = int(declared) if declared is not None and declared.isdigit() else None
                return release, url, zip_bytes
        return None, None, None
    finally:
        if own_client:
            c.close()


# --- Failure diagnosis -------------------------------------------------------
# One integrate run at Franklin failed with "RemoteProtocolError: Server
# disconnected without sending a response" and the job report said the year
# "may have been moved or withdrawn". It hadn't: an egress proxy was cutting the
# download off after the connection opened. A fetch can fail for very different
# reasons -- the year really is gone (HTTP 404), or the network between this
# server and nces.ed.gov is broken (DNS, refused, timeout, TLS, or a proxy that
# closes the connection with no HTTP response). Only the first is NCES's doing;
# the rest are the operator's, and the job report must say which so the right
# team gets the ticket.

def describe_fetch_error(e: BaseException) -> tuple[str, str]:
    """Classify an exception from head_release/download_zip into
    (kind, plain-English detail). `kind` is "not_found" (NCES answered 404),
    "http" (any other HTTP status), "network" (never got a usable HTTP
    answer -- DNS, connect, timeout, TLS, or a connection closed without a
    response), or "other"."""
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code == 404:
            return "not_found", "NCES answered 404 Not Found"
        return "http", f"NCES answered HTTP {code}"
    if isinstance(e, httpx.RemoteProtocolError):
        return "network", ("the connection to nces.ed.gov opened but was closed "
                           "without an HTTP response (a proxy or firewall "
                           "cutting the transfer off looks exactly like this)")
    if isinstance(e, httpx.ConnectTimeout):
        return "network", "connecting to nces.ed.gov timed out"
    if isinstance(e, httpx.TimeoutException):
        return "network", "nces.ed.gov stopped sending data and the transfer timed out"
    if isinstance(e, httpx.ConnectError):
        msg = str(e)
        low = msg.lower()
        if "getaddrinfo" in low or "name or service" in low or "nodename" in low \
                or "name resolution" in low:
            return "network", "the name nces.ed.gov could not be resolved (DNS)"
        if "ssl" in low or "certificate" in low or "tls" in low:
            return "network", f"the TLS handshake with nces.ed.gov failed ({msg})"
        if "refused" in low:
            return "network", "the connection to nces.ed.gov was refused"
        return "network", f"could not connect to nces.ed.gov ({msg})"
    if isinstance(e, (httpx.NetworkError, httpx.ProtocolError)):
        return "network", f"network error talking to nces.ed.gov ({type(e).__name__}: {e})"
    return "other", f"{type(e).__name__}: {e}"


# The URL the reachability probe fetches one byte of: the earliest year's Final
# release, which has been at the same address for two decades. Fixed on purpose
# (SSRF posture above) -- never a caller-chosen year.
_PROBE_START_YEAR = EARLIEST_START_YEAR


def probe_reachability(client: httpx.Client | None = None) -> dict:
    """Can this server actually DOWNLOAD from NCES? head_release's HEAD probes
    can succeed while a real download is cut off (a proxy that lets small
    requests through and kills bodies), which is how the catalog listed a year
    as available and the fetch of it then died. So this issues one ranged GET
    for the first byte of a fixed, long-lived zip and reads the body. Never
    raises; returns {"ok": bool, "detail": str | None} where detail is the
    plain-English cause from describe_fetch_error. Deliberately NOT cached,
    unlike probe_catalog: an operator retrying after a network fix must see the
    live answer, not an hour-old one."""
    own_client = client is None
    c = client or _client(get_settings().nces_http_timeout_seconds)
    url = _zip_url(_PROBE_START_YEAR, "Final")
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            with c.stream("GET", url, headers={"Range": "bytes=0-0"},
                          follow_redirects=False) as resp:
                if resp.is_redirect:
                    url = _validated_redirect_target(url, resp)
                    continue
                resp.raise_for_status()
                if resp.url.host != NCES_HOST:  # defense-in-depth; hops validated
                    raise ValueError(
                        f"redirect for {url} resolved off {NCES_HOST} (to {resp.url.host})")
                for _chunk in resp.iter_bytes():
                    break  # one chunk proves the body flows; stop there
                return {"ok": True, "detail": None}
        raise ValueError(f"too many redirects fetching {url}")
    except Exception as e:  # noqa: BLE001 -- a probe reports, it never raises
        _kind, detail = describe_fetch_error(e)
        return {"ok": False, "detail": detail}
    finally:
        if own_client:
            c.close()


# --- probe_catalog: one entry per start year, with a short TTL cache -------
_CATALOG_TTL_SECONDS = 3600
_catalog_cache: dict[str, object] = {"at": None, "data": None}


def _clear_catalog_cache() -> None:
    _catalog_cache["at"] = None
    _catalog_cache["data"] = None


def _probe_one(start_year: int, client: httpx.Client) -> dict:
    release, _url, zip_bytes = head_release(start_year, client=client)
    return {
        "start_year": start_year,
        "year_label": f"{start_year}-{str(start_year + 1)[-2:]}",
        "year": start_year + 1,
        "available": release is not None,
        "release": release,
        "zip_bytes": zip_bytes,
    }


def probe_catalog(refresh: bool = False, client: httpx.Client | None = None) -> list[dict]:
    """One entry per NCES start year [EARLIEST_START_YEAR .. current_year+1],
    each shaped {start_year, year_label, year, available, release, zip_bytes}.
    Probes run CONCURRENTLY (a ThreadPoolExecutor, width
    settings.nces_probe_concurrency) but the returned list is always re-sorted
    ascending by start_year, regardless of completion order. Cached in-process
    for ~1h; refresh=True bypasses the cache."""
    now = time.time()
    if not refresh and _catalog_cache["data"] is not None and \
            _catalog_cache["at"] is not None and \
            (now - _catalog_cache["at"]) < _CATALOG_TTL_SECONDS:
        return _catalog_cache["data"]

    current_year = datetime.now().year
    years = list(range(EARLIEST_START_YEAR, current_year + 2))
    own_client = client is None
    c = client or _client(get_settings().nces_http_timeout_seconds)
    try:
        with ThreadPoolExecutor(max_workers=get_settings().nces_probe_concurrency) as ex:
            futures = {sy: ex.submit(_probe_one, sy, c) for sy in years}
            entries = [futures[sy].result() for sy in years]
    finally:
        if own_client:
            c.close()

    _catalog_cache["data"] = entries
    _catalog_cache["at"] = now
    return entries


def download_zip(url: str, dest: Path, max_bytes: int, client: httpx.Client | None = None, *,
                 on_progress=None, deadline_seconds: float | None = None,
                 known_total: int | None = None) -> Path:
    """Stream `url` to `dest`, enforcing `max_bytes` both from a declared
    Content-Length (rejected up front) AND from the actual running byte count
    mid-stream (a lying/missing header must not bypass the cap). Refuses to
    trust a redirect that resolves off `NCES_HOST` (mirrors head_release).
    Deletes any partial file on failure.

    `on_progress(written, total)`, if given, is called with CUMULATIVE bytes
    after every chunk (`total` is the declared Content-Length, or None if the
    server didn't send one). `deadline_seconds`, if given, enforces a
    per-transfer wall-clock cap — checked against `time.monotonic()` (a
    module-attribute reference, `nces.time.monotonic`, so tests can
    monkeypatch it deterministically) — and aborts the stream (raising) just
    like the byte cap does. `deadline_seconds=None` (the default) means no
    deadline at all; `fetch_year` is the caller that passes the
    `settings.nces_download_deadline_seconds` default through."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    own_client = client is None
    c = client or _client(get_settings().nces_http_timeout_seconds)
    try:
        start = time.monotonic() if deadline_seconds is not None else None
        # Manually follow redirects, validating each hop's host BEFORE the next
        # request (SEC-6); the stream body is only ever read from a validated
        # same-host, non-redirect response.
        for _ in range(_MAX_REDIRECTS + 1):
            with c.stream("GET", url, follow_redirects=False) as resp:
                if resp.is_redirect:
                    url = _validated_redirect_target(url, resp)
                    continue
                resp.raise_for_status()
                resolved_host = resp.url.host
                if resolved_host != NCES_HOST:  # defense-in-depth; hops validated
                    raise ValueError(
                        f"redirect for {url} resolved off {NCES_HOST} (to {resolved_host})")
                declared = resp.headers.get("content-length")
                total: int | None
                if declared is not None:
                    try:
                        declared_n = int(declared)
                    except ValueError:
                        declared_n = None
                    if declared_n is not None and declared_n > max_bytes:
                        raise ValueError(
                            f"declared Content-Length {declared_n} exceeds the "
                            f"{max_bytes}-byte cap for {url}")
                    total = declared_n
                else:
                    total = None
                # For the progress %, fall back to the size learned from the HEAD
                # probe when the streamed GET omits Content-Length (chunked /
                # compressed transfer) — otherwise pct is stuck at 0 the whole
                # download. Display only; the byte caps above/below are unaffected.
                if total is None:
                    total = known_total
                written = 0
                with dest.open("wb") as f:
                    for chunk in resp.iter_bytes():
                        if deadline_seconds is not None and \
                                (time.monotonic() - start) > deadline_seconds:
                            raise TimeoutError(
                                f"download of {url} exceeded the {deadline_seconds}s deadline")
                        written += len(chunk)
                        if on_progress is not None:
                            on_progress(written, total)
                        if written > max_bytes:
                            raise ValueError(
                                f"download of {url} exceeded the {max_bytes}-byte "
                                "cap mid-stream")
                        f.write(chunk)
                return dest
        raise ValueError(f"too many redirects fetching {url}")
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    finally:
        if own_client:
            c.close()


def _unsafe_member_name(name: str, out_dir: Path) -> bool:
    """Zip-slip guard: reject a member that is an absolute path, uses a
    backslash separator (non-conformant — refuse rather than guess), contains a
    '..' traversal component, or resolves outside out_dir. A normal
    subdirectory is ALLOWED: some NCES zips nest the .accdb under a top-level
    folder (e.g. 'IPEDS_2020-21_Final/IPEDS202021.accdb'), and we extract by
    basename into out_dir regardless (see extract_accdb), so nesting is safe."""
    if "\\" in name:
        return True
    if name.startswith("/") or PurePosixPath(name).is_absolute():
        return True
    if ".." in PurePosixPath(name).parts:
        return True
    resolved = (out_dir / name).resolve()
    try:
        resolved.relative_to(out_dir.resolve())
    except ValueError:
        return True
    return False


def extract_accdb(zip_path: Path, out_dir: Path, expected_start_year: int,
                  max_extract_bytes: int | None = None) -> Path:
    """Extract the single .accdb member from `zip_path` into `out_dir`,
    normalizing the output filename's case, guarding against zip-slip member
    names, enforcing a size cap on the declared (uncompressed) member size
    BEFORE extracting (zip-bomb guard), and rejecting a member whose embedded
    year doesn't match `expected_start_year`. Ignores non-.accdb members
    (docx/xlsx docs bundled in the same zip)."""
    if max_extract_bytes is None:
        max_extract_bytes = get_settings().nces_accdb_max_mb * 1024 * 1024
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        for zi in infos:
            if _unsafe_member_name(zi.filename, out_dir):
                raise ValueError(f"unsafe member path in zip: {zi.filename}")

        accdb_members = [zi for zi in infos if ACCDB_NAME_RE.search(zi.filename)]
        if not accdb_members:
            raise ValueError(f"{zip_path} contains no .accdb member")
        if len(accdb_members) > 1:
            raise ValueError(
                f"{zip_path} contains more than one .accdb member: "
                f"{[zi.filename for zi in accdb_members]}")
        member = accdb_members[0]

        m = ACCDB_NAME_RE.search(member.filename)
        member_start_year = int(m.group(1))
        if member_start_year != int(expected_start_year):
            raise ValueError(
                f"member {member.filename} is for start year {member_start_year}, "
                f"expected {expected_start_year}")

        if member.file_size > max_extract_bytes:
            raise ValueError(
                f"member {member.filename} declares {member.file_size} bytes, "
                f"exceeding the {max_extract_bytes}-byte cap")

        out_name = f"IPEDS{expected_start_year}{str(int(expected_start_year) + 1)[-2:]}.accdb"
        out_path = out_dir / out_name
        with zf.open(member) as src, out_path.open("wb") as dst:
            shutil.copyfileobj(src, dst)

    return out_path


def fetch_year(start_year: int, work_dir: Path, *, on_progress=None) -> tuple[Path, str]:
    """Orchestrate head_release -> download_zip -> extract_accdb for one
    start year. Returns (accdb_path, release) (deleting the zip afterward),
    or raises if NCES has neither release for the year. `on_progress`, if
    given, is threaded straight through to download_zip."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    s = get_settings()

    release, url, head_bytes = head_release(start_year)
    if not release or not url:
        raise ValueError(f"NCES has no Final or Provisional release for start year {start_year}")

    zip_path = work_dir / f"nces_download_{start_year}.zip"
    try:
        download_zip(url, zip_path, max_bytes=s.nces_zip_max_mb * 1024 * 1024,
                     on_progress=on_progress,
                     deadline_seconds=s.nces_download_deadline_seconds,
                     known_total=head_bytes)
        accdb_path = extract_accdb(zip_path, work_dir, expected_start_year=start_year,
                                   max_extract_bytes=s.nces_accdb_max_mb * 1024 * 1024)
        return accdb_path, release
    finally:
        zip_path.unlink(missing_ok=True)
