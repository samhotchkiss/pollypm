"""Tests for the release-channel update check (#714).

Covers the channel filter (stable drops prereleases; beta keeps them),
the 24h cache (hit on repeat, miss on TTL expiry, invalidate on channel
switch), offline behavior (returns None, never raises), and the banner
string shape.

The HTTP path is never exercised — every test uses the ``network_fetch``
seam on ``check_latest`` to feed synthetic release payloads.
"""

from __future__ import annotations

import json
import time

from pollypm import release_check


def _release(tag: str, *, prerelease: bool = False, draft: bool = False) -> dict:
    return {"tag_name": tag, "prerelease": prerelease, "draft": draft}


def _fixed_fetch(releases: list[dict]):
    def _call() -> list[dict]:
        return releases
    return _call


def test_stable_channel_filters_prereleases(tmp_path, monkeypatch):
    monkeypatch.setattr(release_check.pollypm, "__version__", "0.1.0")
    result = release_check.check_latest(
        "stable",
        cache_path=tmp_path / "cache.json",
        network_fetch=_fixed_fetch([
            _release("v0.3.0-beta.1", prerelease=True),
            _release("v0.2.0"),
            _release("v0.1.5"),
        ]),
    )
    assert result is not None
    assert result.latest == "0.2.0"
    assert result.channel == "stable"
    assert result.upgrade_available is True


def test_beta_channel_includes_prereleases(tmp_path, monkeypatch):
    monkeypatch.setattr(release_check.pollypm, "__version__", "0.1.0")
    result = release_check.check_latest(
        "beta",
        cache_path=tmp_path / "cache.json",
        network_fetch=_fixed_fetch([
            _release("v0.3.0-beta.1", prerelease=True),
            _release("v0.2.0"),
        ]),
    )
    assert result is not None
    assert result.latest == "0.3.0-beta.1"
    assert result.channel == "beta"


def test_no_upgrade_when_current_is_latest(tmp_path, monkeypatch):
    monkeypatch.setattr(release_check.pollypm, "__version__", "0.2.0")
    result = release_check.check_latest(
        "stable",
        cache_path=tmp_path / "cache.json",
        network_fetch=_fixed_fetch([_release("v0.2.0")]),
    )
    assert result is not None
    assert result.upgrade_available is False


def test_offline_returns_none_without_raising(tmp_path, monkeypatch):
    def _empty():
        return []
    monkeypatch.setattr(release_check.pollypm, "__version__", "0.1.0")
    result = release_check.check_latest(
        "stable",
        cache_path=tmp_path / "cache.json",
        network_fetch=_empty,
    )
    assert result is None


def test_drafts_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(release_check.pollypm, "__version__", "0.1.0")
    result = release_check.check_latest(
        "stable",
        cache_path=tmp_path / "cache.json",
        network_fetch=_fixed_fetch([
            _release("v0.9.9", draft=True),
            _release("v0.2.0"),
        ]),
    )
    assert result is not None
    assert result.latest == "0.2.0"


def test_unknown_channel_falls_back_to_stable(tmp_path, monkeypatch):
    monkeypatch.setattr(release_check.pollypm, "__version__", "0.1.0")
    result = release_check.check_latest(
        "nightly",
        cache_path=tmp_path / "cache.json",
        network_fetch=_fixed_fetch([
            _release("v0.3.0-beta.1", prerelease=True),
            _release("v0.2.0"),
        ]),
    )
    assert result is not None
    # Unknown channel maps to stable, which filters prereleases.
    assert result.latest == "0.2.0"
    assert result.channel == "stable"


def test_cache_hit_avoids_second_network_call(tmp_path, monkeypatch):
    monkeypatch.setattr(release_check.pollypm, "__version__", "0.1.0")
    calls = {"n": 0}

    def _counting():
        calls["n"] += 1
        return [_release("v0.2.0")]

    cache_path = tmp_path / "cache.json"
    first = release_check.check_latest(
        "stable", cache_path=cache_path, network_fetch=_counting,
    )
    second = release_check.check_latest(
        "stable", cache_path=cache_path, network_fetch=_counting,
    )
    assert first is not None and second is not None
    assert first.latest == second.latest
    assert calls["n"] == 1  # second call served from cache


def test_cache_expires_after_ttl(tmp_path, monkeypatch):
    monkeypatch.setattr(release_check.pollypm, "__version__", "0.1.0")
    cache_path = tmp_path / "cache.json"
    release_check.check_latest(
        "stable", cache_path=cache_path,
        network_fetch=_fixed_fetch([_release("v0.2.0")]),
    )
    # Rewrite the cached_at to the distant past.
    data = json.loads(cache_path.read_text())
    data["cached_at"] = time.time() - 10_000_000
    cache_path.write_text(json.dumps(data))

    calls = {"n": 0}

    def _counting():
        calls["n"] += 1
        return [_release("v0.3.0")]

    result = release_check.check_latest(
        "stable", cache_path=cache_path, network_fetch=_counting,
    )
    assert calls["n"] == 1
    assert result is not None and result.latest == "0.3.0"


def test_channel_switch_invalidates_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(release_check.pollypm, "__version__", "0.1.0")
    cache_path = tmp_path / "cache.json"
    release_check.check_latest(
        "stable", cache_path=cache_path,
        network_fetch=_fixed_fetch([_release("v0.2.0")]),
    )
    # Switching to beta must re-query — previous cache was stable-scoped.
    calls = {"n": 0}

    def _counting():
        calls["n"] += 1
        return [
            _release("v0.3.0-beta.1", prerelease=True),
            _release("v0.2.0"),
        ]

    result = release_check.check_latest(
        "beta", cache_path=cache_path, network_fetch=_counting,
    )
    assert calls["n"] == 1
    assert result is not None and result.channel == "beta"


def test_invalidate_cache_drops_file(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("{}")
    release_check.invalidate_cache(cache_path)
    assert not cache_path.exists()
    # Idempotent — second call doesn't raise.
    release_check.invalidate_cache(cache_path)


def test_banner_text_stable_channel(tmp_path, monkeypatch):
    monkeypatch.setattr(release_check.pollypm, "__version__", "0.1.0")
    check = release_check.check_latest(
        "stable", cache_path=tmp_path / "cache.json",
        network_fetch=_fixed_fetch([_release("v0.2.0")]),
    )
    assert check is not None
    text = release_check.banner_text(check)
    assert "0.1.0" in text
    assert "0.2.0" in text
    assert "Open Settings to upgrade" in text
    # Stable channel omits the channel label to keep the banner terse.
    assert "channel" not in text


def test_banner_text_beta_channel_labels(tmp_path, monkeypatch):
    monkeypatch.setattr(release_check.pollypm, "__version__", "0.1.0")
    check = release_check.check_latest(
        "beta", cache_path=tmp_path / "cache.json",
        network_fetch=_fixed_fetch([
            _release("v0.3.0-beta.1", prerelease=True),
        ]),
    )
    assert check is not None
    text = release_check.banner_text(check)
    assert "beta channel" in text


def test_update_banner_line_empty_when_no_upgrade(tmp_path, monkeypatch):
    monkeypatch.setattr(release_check.pollypm, "__version__", "0.2.0")
    monkeypatch.setattr(
        release_check, "_cache_path", lambda: tmp_path / "cache.json",
    )
    line = release_check.update_banner_line(
        channel="stable",
        network_fetch=_fixed_fetch([_release("v0.2.0")]),
    )
    assert line == ""


def test_update_banner_line_shows_when_upgrade(tmp_path, monkeypatch):
    monkeypatch.setattr(release_check.pollypm, "__version__", "0.1.0")
    monkeypatch.setattr(
        release_check, "_cache_path", lambda: tmp_path / "cache.json",
    )
    line = release_check.update_banner_line(
        channel="stable",
        network_fetch=_fixed_fetch([_release("v0.2.0")]),
    )
    assert "Open Settings to upgrade" in line
    assert "0.2.0" in line


def test_malformed_cache_is_discarded(tmp_path, monkeypatch):
    monkeypatch.setattr(release_check.pollypm, "__version__", "0.1.0")
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("not json")
    result = release_check.check_latest(
        "stable", cache_path=cache_path,
        network_fetch=_fixed_fetch([_release("v0.2.0")]),
    )
    assert result is not None
    assert result.latest == "0.2.0"


def test_invalid_version_tag_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(release_check.pollypm, "__version__", "0.1.0")
    result = release_check.check_latest(
        "stable", cache_path=tmp_path / "cache.json",
        network_fetch=_fixed_fetch([
            {"tag_name": "release-candidate", "prerelease": False, "draft": False},
            _release("v0.2.0"),
        ]),
    )
    assert result is not None
    assert result.latest == "0.2.0"
