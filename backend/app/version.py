"""Running version + a read-only "is a newer release available?" check.

The browser can't call GitHub (CSP `connect-src 'self'`), so this runs
server-side and is exposed via `GET /api/version`. Everything here **fails open**
— offline, rate-limited, or no-releases-yet must never break a request — and the
GitHub result is cached process-wide so N users/workers don't hammer the
unauthenticated 60/hr limit. The outbound URL is built from a fixed repo slug
(`config.GITHUB_REPO`), never user input.
"""
from __future__ import annotations

import logging
import time

import httpx

from app.config import GITHUB_REPO, get_settings

log = logging.getLogger("ipeds.version")

_CACHE_TTL = 6 * 3600  # seconds; the newest release rarely changes
_HTTP_TIMEOUT = 3.0
# Process-wide cache: the last-fetched latest tag + when it was fetched. A miss
# ("" / None never fetched) and a stale entry both trigger a refresh; a fetch
# error keeps whatever was last known (or None) so we never regress to raising.
_cache: dict = {"latest": None, "at": 0.0}


def parse_semver(s: str | None) -> tuple[int, int, int] | None:
    """"X.Y.Z" (optionally with a leading 'v' or trailing '-suffix') → a tuple,
    or None when it isn't a plain numeric semver (e.g. "dev")."""
    if not s:
        return None
    core = str(s).strip().lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)  # type: ignore[return-value]


def is_newer(latest: str | None, current: str | None) -> bool:
    """True only when BOTH parse and latest > current. Unknown/unparseable
    (current == "dev", a network miss) is treated as "no update to offer"."""
    lv, cv = parse_semver(latest), parse_semver(current)
    return bool(lv and cv and lv > cv)


def latest_release() -> str | None:
    """The newest published release tag (leading 'v' stripped), cached ~6h, or
    None when the check is disabled or hasn't succeeded. Never raises."""
    if not get_settings().update_check_enabled:
        return None
    now = time.time()
    if _cache["latest"] is not None and (now - _cache["at"]) < _CACHE_TTL:
        return _cache["latest"]
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    try:
        r = httpx.get(url, timeout=_HTTP_TIMEOUT,
                      headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        tag = str(r.json().get("tag_name") or "").strip().lstrip("vV")
        if tag:
            _cache["latest"], _cache["at"] = tag, now
        return _cache["latest"]
    except Exception as e:  # noqa: BLE001 — a version check must never 500 a request
        log.info("release check skipped (%s)", e)
        return _cache["latest"]  # last known (or None) — never regress to raising


def version_info() -> dict:
    """`{current, latest, update_available}` for the About dialog / Admin banner."""
    current = get_settings().app_version
    latest = latest_release()
    return {"current": current, "latest": latest,
            "update_available": is_newer(latest, current)}
