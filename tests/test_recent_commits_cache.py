"""Cycle 130 — perf review fix: cache _recent_commits subprocess calls.

The polly-dashboard refresh tick used to spawn one ``git log``
subprocess per project per refresh. At 10s tick × 9 projects that
was 9 forks every tick and a 45s worst-case hang on a single slow
repo. The fix caches per-project rows for 60s and tightens the
per-project timeout to 2s so a slow repo can't poison a refresh.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pollypm.dashboard_data import (
    _COMMIT_CACHE,
    _CachedCommitRow,
    _git_log_rows_cached,
    _recent_commits,
)


def _config_with_projects(*paths: Path) -> SimpleNamespace:
    projects = {
        f"p{idx}": SimpleNamespace(path=p) for idx, p in enumerate(paths)
    }
    return SimpleNamespace(projects=projects)


def setup_function(_func) -> None:
    _COMMIT_CACHE.clear()


def test_git_log_cache_hit_skips_subprocess(tmp_path: Path) -> None:
    """Two reads inside the TTL → only one subprocess.run call."""
    project = tmp_path / "demo"
    (project / ".git").mkdir(parents=True)

    seed_rows = [
        _CachedCommitRow(
            hash7="abc1234",
            message="seed commit",
            author="alice",
            date_iso="2026-04-26T05:00:00+00:00",
        ),
    ]
    call_count = {"n": 0}

    def fake_run(*_args, **_kwargs):
        call_count["n"] += 1
        return SimpleNamespace(returncode=0, stdout="abc1234\tseed commit\talice\t2026-04-26T05:00:00+00:00\n")

    with patch("pollypm.dashboard_data.subprocess.run", side_effect=fake_run):
        first = _git_log_rows_cached(project, hours=24)
        second = _git_log_rows_cached(project, hours=24)

    assert call_count["n"] == 1, "second call within TTL must hit the cache"
    assert [r.hash7 for r in first] == ["abc1234"]
    assert second == first


def test_git_log_cache_caches_empty_result_too(tmp_path: Path) -> None:
    """A timeout / failure caches an empty result so the next tick
    doesn't re-spawn git on a known-slow repo."""
    project = tmp_path / "slow"
    (project / ".git").mkdir(parents=True)
    call_count = {"n": 0}

    import subprocess as _sp

    def fake_run(*_args, **_kwargs):
        call_count["n"] += 1
        raise _sp.TimeoutExpired(cmd="git", timeout=2.0)

    with patch("pollypm.dashboard_data.subprocess.run", side_effect=fake_run):
        first = _git_log_rows_cached(project, hours=24)
        second = _git_log_rows_cached(project, hours=24)

    assert call_count["n"] == 1, "timeout result must be cached so the next tick is free"
    assert first == [] and second == []


def test_recent_commits_uses_cache_for_repeated_calls(tmp_path: Path) -> None:
    """The full ``_recent_commits`` flow shares the cache across
    refreshes — two ticks of three projects = three subprocess calls
    total, not six."""
    paths = [tmp_path / f"p{i}" for i in range(3)]
    for p in paths:
        (p / ".git").mkdir(parents=True)
    config = _config_with_projects(*paths)

    call_count = {"n": 0}

    def fake_run(*_args, **kwargs):
        call_count["n"] += 1
        cwd = str(kwargs.get("cwd", ""))
        # Distinct hash per project so dedup works.
        h = f"hash{cwd[-2:]}"
        return SimpleNamespace(
            returncode=0,
            stdout=f"{h}1234567\tmsg\talice\t2026-04-26T05:00:00+00:00\n",
        )

    with patch("pollypm.dashboard_data.subprocess.run", side_effect=fake_run):
        first = _recent_commits(config, hours=24)
        second = _recent_commits(config, hours=24)

    assert call_count["n"] == 3, "cache must collapse the second sweep to zero subprocess calls"
    assert len(first) == 3
    assert len(second) == 3
