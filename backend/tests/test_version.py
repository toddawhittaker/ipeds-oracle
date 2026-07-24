"""app/version.py: semver compare, the cached fails-open GitHub release check,
and version_info. No network — httpx.get is monkeypatched and the disabled path
makes no call at all; get_settings is stubbed so no .env is needed."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("APP_DB_PATH", "/tmp/version-test-app.db")
os.environ["LLM_API_KEY"] = ""

from app import version  # noqa: E402

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


class _Stub:
    def __init__(self, enabled=True, current="0.1.0"):
        self.update_check_enabled = enabled
        self.app_version = current


def test_parse_semver():
    assert version.parse_semver("v0.2.0") == (0, 2, 0)
    assert version.parse_semver("0.1.0") == (0, 1, 0)
    assert version.parse_semver("1.2.3-rc1") == (1, 2, 3)  # pre-release suffix dropped
    for bad in (None, "", "dev", "1.2", "1.2.x", "latest"):
        assert version.parse_semver(bad) is None, bad


def test_is_newer():
    assert version.is_newer("0.2.0", "0.1.0") is True
    assert version.is_newer("v0.2.0", "0.1.0") is True
    assert version.is_newer("0.1.0", "0.1.0") is False
    assert version.is_newer("0.1.0", "0.2.0") is False
    assert version.is_newer("0.1.0", "dev") is False   # current unparseable
    assert version.is_newer(None, "0.1.0") is False    # no known latest


def test_latest_release_disabled():
    version._cache = {"latest": None, "at": 0.0}
    version.get_settings = lambda: _Stub(enabled=False)
    assert version.latest_release() is None


def test_latest_release_fetch_and_cache():
    version._cache = {"latest": None, "at": 0.0}
    version.get_settings = lambda: _Stub(enabled=True)
    calls = {"n": 0}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"tag_name": "v0.3.0"}

    def _fake_get(url, **kw):
        calls["n"] += 1
        assert "api.github.com/repos/toddawhittaker/ipeds-oracle/releases/latest" in url
        return _Resp()

    version.httpx.get = _fake_get
    assert version.latest_release() == "0.3.0"   # leading v stripped
    assert version.latest_release() == "0.3.0"   # served from cache
    assert calls["n"] == 1, "second call must hit the cache, not the network"


def test_latest_release_fails_open():
    version._cache = {"latest": None, "at": 0.0}
    version.get_settings = lambda: _Stub(enabled=True)

    def _boom(url, **kw):
        raise RuntimeError("network down")

    version.httpx.get = _boom
    assert version.latest_release() is None  # error → None, never raises


def test_version_info():
    version.get_settings = lambda: _Stub(enabled=True, current="0.1.0")
    version.latest_release = lambda: "0.2.0"
    assert version.version_info() == {
        "current": "0.1.0", "latest": "0.2.0", "update_available": True}


def run():
    check("parse_semver: valid tags parse, non-semver → None", test_parse_semver)
    check("is_newer: strict >, unknown/unparseable → False", test_is_newer)
    check("latest_release disabled → None (no network)", test_latest_release_disabled)
    check("latest_release fetches once then caches", test_latest_release_fetch_and_cache)
    check("latest_release fails open on any error", test_latest_release_fails_open)
    check("version_info assembles the three keys", test_version_info)
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED")
        sys.exit(1)
    print("\nALL VERSION TESTS PASSED")


if __name__ == "__main__":
    run()
