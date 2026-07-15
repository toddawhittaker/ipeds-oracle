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
from datetime import datetime
from pathlib import Path, PurePosixPath

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


def _client(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True)


def head_release(
    start_year: int, client: httpx.Client | None = None,
) -> tuple[str | None, str | None]:
    """HEAD the Final release, falling back to Provisional. Returns
    (release, url) for whichever exists, or (None, None) if neither does (this
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
                return None, None
            resp = c.head(url, follow_redirects=True)
            resolved_host = resp.url.host
            if resolved_host != NCES_HOST:
                raise ValueError(
                    f"redirect for {url} resolved off {NCES_HOST} (to {resolved_host})")
            if resp.status_code == 200:
                return release, url
        return None, None
    finally:
        if own_client:
            c.close()


# --- probe_catalog: one entry per start year, with a short TTL cache -------
_CATALOG_TTL_SECONDS = 3600
_catalog_cache: dict[str, object] = {"at": None, "data": None}


def _clear_catalog_cache() -> None:
    _catalog_cache["at"] = None
    _catalog_cache["data"] = None


def probe_catalog(refresh: bool = False, client: httpx.Client | None = None) -> list[dict]:
    """One entry per NCES start year [EARLIEST_START_YEAR .. current_year+1],
    each shaped {start_year, year_label, year, available, release}. Cached
    in-process for ~1h; refresh=True bypasses the cache."""
    now = time.time()
    if not refresh and _catalog_cache["data"] is not None and \
            _catalog_cache["at"] is not None and \
            (now - _catalog_cache["at"]) < _CATALOG_TTL_SECONDS:
        return _catalog_cache["data"]

    current_year = datetime.now().year
    own_client = client is None
    c = client or _client(get_settings().nces_http_timeout_seconds)
    entries: list[dict] = []
    try:
        for start_year in range(EARLIEST_START_YEAR, current_year + 2):
            release, _url = head_release(start_year, client=c)
            entries.append({
                "start_year": start_year,
                "year_label": f"{start_year}-{str(start_year + 1)[-2:]}",
                "year": start_year + 1,
                "available": release is not None,
                "release": release,
            })
    finally:
        if own_client:
            c.close()

    _catalog_cache["data"] = entries
    _catalog_cache["at"] = now
    return entries


def download_zip(url: str, dest: Path, max_bytes: int, client: httpx.Client | None = None) -> Path:
    """Stream `url` to `dest`, enforcing `max_bytes` both from a declared
    Content-Length (rejected up front) AND from the actual running byte count
    mid-stream (a lying/missing header must not bypass the cap). Refuses to
    trust a redirect that resolves off `NCES_HOST` (mirrors head_release).
    Deletes any partial file on failure."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    own_client = client is None
    c = client or _client(get_settings().nces_http_timeout_seconds)
    try:
        with c.stream("GET", url) as resp:
            resp.raise_for_status()
            resolved_host = resp.url.host
            if resolved_host != NCES_HOST:
                raise ValueError(
                    f"redirect for {url} resolved off {NCES_HOST} (to {resolved_host})")
            declared = resp.headers.get("content-length")
            if declared is not None:
                try:
                    declared_n = int(declared)
                except ValueError:
                    declared_n = None
                if declared_n is not None and declared_n > max_bytes:
                    raise ValueError(
                        f"declared Content-Length {declared_n} exceeds the "
                        f"{max_bytes}-byte cap for {url}")
            written = 0
            with dest.open("wb") as f:
                for chunk in resp.iter_bytes():
                    written += len(chunk)
                    if written > max_bytes:
                        raise ValueError(
                            f"download of {url} exceeded the {max_bytes}-byte "
                            "cap mid-stream")
                    f.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    finally:
        if own_client:
            c.close()
    return dest


def _unsafe_member_name(name: str, out_dir: Path) -> bool:
    """Zip-slip guard: reject any member name with a path separator, a '..'
    component, or an absolute path, or one that would resolve outside
    out_dir. IPEDS zips are flat archives, so disallowing any nesting at all
    is not a functional loss."""
    if "/" in name or "\\" in name or ".." in name:
        return True
    if PurePosixPath(name).is_absolute():
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


def fetch_year(start_year: int, work_dir: Path) -> Path:
    """Orchestrate head_release -> download_zip -> extract_accdb for one
    start year. Returns the path to the extracted .accdb (deleting the zip
    afterward), or raises if NCES has neither release for the year."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    s = get_settings()

    release, url = head_release(start_year)
    if not release or not url:
        raise ValueError(f"NCES has no Final or Provisional release for start year {start_year}")

    zip_path = work_dir / f"nces_download_{start_year}.zip"
    try:
        download_zip(url, zip_path, max_bytes=s.nces_zip_max_mb * 1024 * 1024)
        return extract_accdb(zip_path, work_dir, expected_start_year=start_year,
                             max_extract_bytes=s.nces_accdb_max_mb * 1024 * 1024)
    finally:
        zip_path.unlink(missing_ok=True)
