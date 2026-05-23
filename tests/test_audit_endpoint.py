"""Integration tests for ``GET /api/v1/audit/{grep,stats}``.

Phase 2 surface #6 (audit query). These tests drive the FastAPI app
end-to-end against a synthesized audit-log tree so the rotation-aware
file walker, ``since`` shortcut parsing, and filter chaining are all
exercised through the HTTP surface.

The harness mirrors ``tests/web_api/conftest.py`` but stays
``--noconftest``-friendly so the spec's pytest invocation
(``pytest --noconftest tests/test_audit_endpoint.py``) runs in
isolation without pulling shared fixtures. Each fixture is declared
locally; the cost is a bit of duplication, the win is that one
unrelated failing fixture upstream can't mask regressions in this
endpoint.
"""

from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pollypm.config import (
    AccountConfig,
    MemorySettings,
    PollyPMConfig,
    PollyPMSettings,
    ProjectSettings,
)
from pollypm.models import KnownProject, ProjectKind, ProviderKind, RuntimeKind
from pollypm.web_api import create_app, ensure_token


# ---------------------------------------------------------------------------
# Fixtures (self-contained — no shared conftest dependency)
# ---------------------------------------------------------------------------


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "myproj"
    root.mkdir()
    (root / ".pollypm").mkdir()
    return root


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".pollypm").mkdir()
    return root


@pytest.fixture
def audit_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``POLLYPM_AUDIT_HOME`` so test logs don't bleed into ``~``."""
    audit = tmp_path / "audit"
    audit.mkdir()
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit))
    return audit


@pytest.fixture
def api_config(
    tmp_path: Path, project_root: Path, workspace_root: Path
) -> PollyPMConfig:
    base_dir = workspace_root / ".pollypm"
    state_db = base_dir / "state.db"
    return PollyPMConfig(
        project=ProjectSettings(
            name="PollyPM",
            root_dir=workspace_root,
            tmux_session="pollypm-test",
            workspace_root=workspace_root,
            base_dir=base_dir,
            logs_dir=base_dir / "logs",
            snapshots_dir=base_dir / "snapshots",
            state_db=state_db,
        ),
        pollypm=PollyPMSettings(
            controller_account="codex_primary",
            open_permissions_by_default=False,
            failover_enabled=False,
            failover_accounts=[],
            heartbeat_backend="local",
            scheduler_backend="inline",
            lease_timeout_minutes=30,
        ),
        accounts={
            "codex_primary": AccountConfig(
                name="codex_primary",
                provider=ProviderKind.CODEX,
                email="codex@example.com",
                runtime=RuntimeKind.LOCAL,
                home=base_dir / "homes" / "codex_primary",
            ),
        },
        sessions={},
        projects={
            "myproj": KnownProject(
                key="myproj",
                path=project_root,
                name="My Project",
                tracked=True,
                kind=ProjectKind.GIT,
            ),
        },
        memory=MemorySettings(backend="file"),
    )


@pytest.fixture
def token_path(tmp_path: Path) -> Path:
    return tmp_path / "api-token"


@pytest.fixture
def token(token_path: Path) -> str:
    value, _generated = ensure_token(token_path)
    return value


@pytest.fixture
def app(api_config, token_path, token):  # noqa: ARG001 — token fixture must run
    return create_app(config=api_config, token_path=token_path)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Log-writing helpers — drive the rotation-aware walker without going
# through ``audit.log.emit`` (we want deterministic timestamps + the
# ability to pre-place ``.gz`` archives).
# ---------------------------------------------------------------------------


def _make_event(
    *,
    project: str = "myproj",
    event: str = "task.created",
    subject: str = "",
    ts: str | None = None,
    status: str = "ok",
    actor: str = "polly",
    metadata: dict | None = None,
) -> dict:
    return {
        "schema": 1,
        "ts": ts or "2026-05-21T00:00:00+00:00",
        "project": project,
        "event": event,
        "subject": subject,
        "actor": actor,
        "status": status,
        "metadata": metadata or {},
    }


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, separators=(",", ":")))
            fh.write("\n")


def _write_jsonl_gz(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, separators=(",", ":")))
            fh.write("\n")


def _per_project_log(project_root: Path) -> Path:
    return project_root / ".pollypm" / "audit.jsonl"


def _central_log(audit_home: Path, project: str) -> Path:
    return audit_home / f"{project}.jsonl"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_grep_requires_auth(client: TestClient, audit_home: Path) -> None:
    """No bearer → ``401 unauthorized`` (spec §6 / Phase 1 §3)."""
    response = client.get("/api/v1/audit/grep")
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] in {"unauthorized", "invalid_token"}


def test_stats_requires_auth(client: TestClient, audit_home: Path) -> None:
    response = client.get("/api/v1/audit/stats")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Grep happy paths
# ---------------------------------------------------------------------------


def test_grep_happy_path_returns_matching_events(
    client: TestClient,
    auth_headers: dict[str, str],
    project_root: Path,
    audit_home: Path,
) -> None:
    """Filter chain (project + event_type + pattern) returns hits."""
    _write_jsonl(
        _per_project_log(project_root),
        [
            _make_event(event="task.created", subject="myproj/1"),
            _make_event(event="task.created", subject="myproj/2"),
            _make_event(event="task.status_changed", subject="myproj/1"),
        ],
    )
    response = client.get(
        "/api/v1/audit/grep",
        params={"project": "myproj", "event_type": "task.created", "pattern": "myproj/"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["next_cursor"] is None
    subjects = [e["subject"] for e in body["events"]]
    assert subjects == ["myproj/1", "myproj/2"]
    # Sanity: every event carries the canonical Event shape.
    assert all("event" in e and "actor" in e for e in body["events"])


def test_grep_regex_pattern_filters_lines(
    client: TestClient,
    auth_headers: dict[str, str],
    project_root: Path,
    audit_home: Path,
) -> None:
    """Pattern is a Python regex (``re.search``) when ``safe_regex=true``."""
    _write_jsonl(
        _per_project_log(project_root),
        [
            _make_event(subject="myproj/1"),
            _make_event(subject="myproj/12"),
            _make_event(subject="myproj/abc"),
        ],
    )
    response = client.get(
        "/api/v1/audit/grep",
        params={
            "project": "myproj",
            "pattern": r"myproj/\d+",
            "safe_regex": "true",
            # Round-4: regex mode requires ``since`` to bound the scan.
            "since": "30d",
        },
        headers=auth_headers,
    )
    assert response.status_code == 200
    subjects = [e["subject"] for e in response.json()["events"]]
    # ``myproj/abc`` doesn't match the digit-only suffix regex.
    assert subjects == ["myproj/1", "myproj/12"]


def test_grep_default_pattern_is_literal_substring(
    client: TestClient,
    auth_headers: dict[str, str],
    project_root: Path,
    audit_home: Path,
) -> None:
    """Without ``safe_regex=true``, ``pattern`` is matched as a substring."""
    _write_jsonl(
        _per_project_log(project_root),
        [
            _make_event(subject="myproj/1"),
            _make_event(subject="myproj/12"),
            _make_event(subject="myproj/abc"),
        ],
    )
    # ``\d+`` is treated as the literal six characters, not a regex —
    # nothing matches.
    response = client.get(
        "/api/v1/audit/grep",
        params={"project": "myproj", "pattern": r"\d+"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json()["events"] == []


def test_grep_since_shortcut_drops_old_events(
    client: TestClient,
    auth_headers: dict[str, str],
    project_root: Path,
    audit_home: Path,
) -> None:
    """``since=1h`` shortcut keeps only the recent event."""
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(hours=3)).isoformat()
    fresh_ts = (now - timedelta(minutes=10)).isoformat()
    _write_jsonl(
        _per_project_log(project_root),
        [
            _make_event(subject="myproj/old", ts=old_ts),
            _make_event(subject="myproj/fresh", ts=fresh_ts),
        ],
    )
    response = client.get(
        "/api/v1/audit/grep",
        params={"project": "myproj", "since": "1h"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    subjects = [e["subject"] for e in response.json()["events"]]
    assert subjects == ["myproj/fresh"]


def test_grep_walks_rotated_gz_archives(
    client: TestClient,
    auth_headers: dict[str, str],
    project_root: Path,
    audit_home: Path,
) -> None:
    """Rotation-awareness: events in a ``.gz`` sibling surface in results."""
    live = _per_project_log(project_root)
    archive = live.with_name(live.name + ".1700000000.gz")
    _write_jsonl(live, [_make_event(subject="myproj/live")])
    _write_jsonl_gz(archive, [_make_event(subject="myproj/archived")])
    # Pin mtime so the walker's newest-first sort is deterministic.
    os.utime(archive, (1_700_000_000, 1_700_000_000))

    response = client.get(
        "/api/v1/audit/grep",
        params={"project": "myproj", "pattern": "myproj/"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    subjects = [e["subject"] for e in response.json()["events"]]
    # Live first, then the rotated archive (matches the CLI ordering).
    assert subjects == ["myproj/live", "myproj/archived"]


def test_grep_skips_truncated_gz_archive_and_counts_it(
    client: TestClient,
    auth_headers: dict[str, str],
    project_root: Path,
    audit_home: Path,
) -> None:
    """Codex round-7 (PR #2062): a truncated ``.gz`` archive must not 500.

    Before the fix the walker only caught ``OSError`` around
    ``gzip.open(...)``/iteration; truncated archives raise
    :class:`EOFError` mid-iteration, which escaped past
    :func:`iter_matching_events` and bubbled to the route as a 500 —
    one bad rotated file would take out the whole grep. The fix
    catches ``EOFError`` / ``gzip.BadGzipFile`` / ``zlib.error``,
    bumps ``stats["corrupt_archives_skipped"]``, and the route
    surfaces it as ``_corrupt_archives_skipped`` on the response.
    """
    live = _per_project_log(project_root)
    _write_jsonl(live, [_make_event(subject="myproj/live")])

    # Build a real gzip stream, then truncate it mid-deflate so
    # iteration raises EOFError (not OSError) — exact failure mode
    # Codex flagged.
    valid_archive = live.with_name(live.name + ".1700000000.gz")
    _write_jsonl_gz(valid_archive, [_make_event(subject="myproj/valid_arch")])
    valid_bytes = valid_archive.read_bytes()
    truncated_archive = live.with_name(live.name + ".1699000000.gz")
    truncated_archive.write_bytes(valid_bytes[: len(valid_bytes) // 2])
    os.utime(valid_archive, (1_700_000_000, 1_700_000_000))
    os.utime(truncated_archive, (1_699_000_000, 1_699_000_000))

    response = client.get(
        "/api/v1/audit/grep",
        params={"project": "myproj", "pattern": "myproj/"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    subjects = [e["subject"] for e in body["events"]]
    # Live + valid archive survive; truncated archive is skipped.
    assert "myproj/live" in subjects
    assert "myproj/valid_arch" in subjects
    assert body.get("_corrupt_archives_skipped", 0) >= 1, body


def test_grep_skips_bad_gzip_header_archive(
    client: TestClient,
    auth_headers: dict[str, str],
    project_root: Path,
    audit_home: Path,
) -> None:
    """Bogus header (``gzip.BadGzipFile``) is also best-effort skipped."""
    live = _per_project_log(project_root)
    _write_jsonl(live, [_make_event(subject="myproj/live")])
    bogus = live.with_name(live.name + ".1698000000.gz")
    bogus.write_bytes(b"this is not a gzip file at all")
    os.utime(bogus, (1_698_000_000, 1_698_000_000))

    response = client.get(
        "/api/v1/audit/grep",
        params={"project": "myproj", "pattern": "myproj/"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert any(e["subject"] == "myproj/live" for e in body["events"])
    assert body.get("_corrupt_archives_skipped", 0) >= 1, body


def test_grep_limit_caps_result_count(
    client: TestClient,
    auth_headers: dict[str, str],
    project_root: Path,
    audit_home: Path,
) -> None:
    """``limit`` short-circuits the iteration."""
    _write_jsonl(
        _per_project_log(project_root),
        [_make_event(subject=f"myproj/{i}") for i in range(50)],
    )
    response = client.get(
        "/api/v1/audit/grep",
        params={"project": "myproj", "limit": 5},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert len(response.json()["events"]) == 5


def test_grep_default_limit_is_100(
    client: TestClient,
    auth_headers: dict[str, str],
    project_root: Path,
    audit_home: Path,
) -> None:
    """No explicit ``limit`` → default 100 (spec §8.1)."""
    _write_jsonl(
        _per_project_log(project_root),
        [_make_event(subject=f"myproj/{i}") for i in range(250)],
    )
    response = client.get(
        "/api/v1/audit/grep",
        params={"project": "myproj"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert len(response.json()["events"]) == 100


# ---------------------------------------------------------------------------
# Grep error paths
# ---------------------------------------------------------------------------


def test_grep_invalid_since_returns_400_invalid_request(
    client: TestClient,
    auth_headers: dict[str, str],
    audit_home: Path,
) -> None:
    """Free-text 'yesterday' is not ISO-8601 → ``400 invalid_request``."""
    response = client.get(
        "/api/v1/audit/grep",
        params={"since": "yesterday"},
        headers=auth_headers,
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert "since" in body["error"]["message"]


def test_grep_invalid_regex_returns_400_invalid_request(
    client: TestClient,
    auth_headers: dict[str, str],
    audit_home: Path,
) -> None:
    """Unbalanced group with ``safe_regex=true`` → ``400 invalid_request``."""
    response = client.get(
        "/api/v1/audit/grep",
        params={"pattern": "(unbalanced", "safe_regex": "true"},
        headers=auth_headers,
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert "pattern" in body["error"]["message"]


def test_grep_limit_above_max_rejected_by_validation(
    client: TestClient,
    auth_headers: dict[str, str],
    audit_home: Path,
) -> None:
    """``limit=5000`` exceeds the 1000 cap (spec §8.3)."""
    response = client.get(
        "/api/v1/audit/grep",
        params={"limit": 5000},
        headers=auth_headers,
    )
    # Pydantic-driven query validation returns ``422 validation_error``
    # via the spec's reshape handler.
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def test_stats_counts_by_event_and_severity(
    client: TestClient,
    auth_headers: dict[str, str],
    project_root: Path,
    audit_home: Path,
) -> None:
    """Stats aggregates across event names + severity (status)."""
    _write_jsonl(
        _per_project_log(project_root),
        [
            _make_event(event="task.created", status="ok"),
            _make_event(event="task.created", status="ok"),
            _make_event(event="task.status_changed", status="warn"),
            _make_event(event="watchdog.escalation_dispatched", status="error"),
        ],
    )
    response = client.get(
        "/api/v1/audit/stats",
        params={"project": "myproj", "since": "30d"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 4
    assert body["by_event"] == {
        "task.created": 2,
        "task.status_changed": 1,
        "watchdog.escalation_dispatched": 1,
    }
    assert body["by_severity"] == {"ok": 2, "warn": 1, "error": 1}


def test_stats_with_project_filter_isolates_one_project(
    client: TestClient,
    auth_headers: dict[str, str],
    project_root: Path,
    audit_home: Path,
) -> None:
    """``project=myproj`` ignores events sitting in another project's tail."""
    # myproj's per-project log carries one event.
    _write_jsonl(
        _per_project_log(project_root),
        [_make_event(project="myproj", event="task.created")],
    )
    # Central tail for a different project — must NOT show up under
    # ``project=myproj``. The CLI helper's ``_resolve_target_files``
    # branch with a project filter scopes to that project's tail only.
    _write_jsonl(
        _central_log(audit_home, "otherproj"),
        [
            _make_event(project="otherproj", event="task.created"),
            _make_event(project="otherproj", event="task.created"),
        ],
    )
    response = client.get(
        "/api/v1/audit/stats",
        params={"project": "myproj", "since": "30d"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["by_event"] == {"task.created": 1}


def test_stats_with_since_excludes_old_events(
    client: TestClient,
    auth_headers: dict[str, str],
    project_root: Path,
    audit_home: Path,
) -> None:
    """``since=1h`` cuts off events older than the cutoff."""
    now = datetime.now(timezone.utc)
    _write_jsonl(
        _per_project_log(project_root),
        [
            _make_event(event="a", ts=(now - timedelta(hours=3)).isoformat()),
            _make_event(event="b", ts=(now - timedelta(minutes=5)).isoformat()),
            _make_event(event="c", ts=(now - timedelta(minutes=1)).isoformat()),
        ],
    )
    response = client.get(
        "/api/v1/audit/stats",
        params={"project": "myproj", "since": "1h"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["by_event"] == {"b": 1, "c": 1}
    # ``since`` echoes back the parsed cutoff so the client can
    # confirm the server's interpretation.
    assert body["since"] is not None


def test_stats_empty_log_returns_zero_total(
    client: TestClient,
    auth_headers: dict[str, str],
    audit_home: Path,
) -> None:
    """No files at all → ``total=0`` (not a 500)."""
    response = client.get(
        "/api/v1/audit/stats",
        params={"project": "myproj", "since": "24h"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0
    assert body["by_event"] == {}
    assert body["by_severity"] == {}


# ---------------------------------------------------------------------------
# Round-2 guardrails (Codex review on PR #2062): ReDoS, malformed rows,
# unbounded stats. See module docstring in
# ``src/pollypm/web_api/routes/audit.py``.
# ---------------------------------------------------------------------------


def test_audit_grep_pattern_length_capped(
    client: TestClient,
    auth_headers: dict[str, str],
    audit_home: Path,
) -> None:
    """Patterns longer than 200 chars → ``400 invalid_request``."""
    long_pattern = "a" * 500
    response = client.get(
        "/api/v1/audit/grep",
        params={"pattern": long_pattern},
        headers=auth_headers,
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert "pattern" in body["error"]["message"]


def test_audit_grep_pathological_pattern_returns_safely(
    client: TestClient,
    auth_headers: dict[str, str],
    project_root: Path,
    audit_home: Path,
) -> None:
    """ReDoS pattern returns within budget when default literal mode is on.

    ``(a+)+b`` over ``aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa!`` is the canonical
    catastrophic-backtracking demo. The HTTP surface treats ``pattern``
    as a literal substring by default, so this query must NOT hang —
    it should complete in well under a second and just return no hits.
    """
    import time

    _write_jsonl(
        _per_project_log(project_root),
        [
            _make_event(subject=f"myproj/{'a' * 30}!"),
            _make_event(subject="myproj/normal"),
        ],
    )
    start = time.monotonic()
    response = client.get(
        "/api/v1/audit/grep",
        params={"project": "myproj", "pattern": "(a+)+b"},
        headers=auth_headers,
    )
    elapsed = time.monotonic() - start
    assert response.status_code == 200, response.text
    # Literal substring search over a handful of small lines must be
    # near-instant; a 1 s budget is generous and unambiguously below
    # what a catastrophic regex would take on the same input.
    assert elapsed < 1.0, f"literal-mode grep took {elapsed:.3f}s (ReDoS regression?)"
    # The literal string "(a+)+b" is never present in any line.
    assert response.json()["events"] == []


def test_audit_grep_malformed_row_skipped_not_500(
    client: TestClient,
    auth_headers: dict[str, str],
    project_root: Path,
    audit_home: Path,
) -> None:
    """A row with a non-ISO ``ts`` must be skipped, not 500 the response."""
    log_path = _per_project_log(project_root)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Mix one valid event with one malformed one (bad ``ts``).
    good = _make_event(subject="myproj/good", ts="2026-05-21T00:00:00+00:00")
    bad = _make_event(subject="myproj/bad", ts="not-a-date")
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(good) + "\n")
        fh.write(json.dumps(bad) + "\n")

    response = client.get(
        "/api/v1/audit/grep",
        params={"project": "myproj"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    subjects = [e["subject"] for e in body["events"]]
    assert subjects == ["myproj/good"]
    assert body["_malformed_rows_skipped"] >= 1


def test_audit_stats_requires_time_window(
    client: TestClient,
    auth_headers: dict[str, str],
    audit_home: Path,
) -> None:
    """``/audit/stats`` without ``since`` → ``400 invalid_request`` with hint."""
    response = client.get(
        "/api/v1/audit/stats",
        params={"project": "myproj"},
        headers=auth_headers,
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert "since" in body["error"]["message"]
    # Hint should mention the CLI escape hatch for unbounded scans.
    hint = (body["error"].get("hint") or "").lower()
    assert "since" in hint or "cli" in hint or "pm audit" in hint


def test_audit_grep_short_pathological_pattern_does_not_hang(
    client: TestClient,
    auth_headers: dict[str, str],
    project_root: Path,
    audit_home: Path,
) -> None:
    """ReDoS-style pattern ``(a+)+b`` against backtracking-heavy input.

    Round 1's 200-char pattern cap doesn't stop this — the pattern is
    only six characters but each line of ``a``s triggers catastrophic
    backtracking in the stdlib ``re`` engine, which holds the GIL
    throughout (so daemon threads can't time it out from Python). Round
    2 runs each ``pattern.search`` inside a ``multiprocessing`` worker
    so the OS can ``terminate()`` the child when its wall-clock budget
    expires; the line is counted in ``_pattern_timeouts`` and the
    response still returns.

    Without the guardrail, ``(a+)+b`` against 40 ``a``s + ``!`` runs
    for tens of seconds per line on Python 3.14 (190 s at n=30 in our
    local benchmark, exponential in n). The test asserts a generous
    8 s ceiling — well below the un-guarded backtrack cost but loose
    enough to absorb the cost of respawning the worker process after
    each timeout (a forkserver respawn is ~50 ms steady state but can
    push into the second-range under TestClient + pytest scheduling).
    """
    import time

    # 40 ``a``s followed by ``!`` — the ``!`` blocks any final ``b``
    # match so the engine exhausts every grouping permutation before
    # admitting defeat. Three rows so the timeout has to fire more
    # than once (single-line timeout could be a happy accident).
    pathological_subject = "a" * 40 + "!"
    _write_jsonl(
        _per_project_log(project_root),
        [_make_event(subject=pathological_subject) for _ in range(3)],
    )

    start = time.monotonic()
    response = client.get(
        "/api/v1/audit/grep",
        params={
            "project": "myproj",
            "pattern": "(a+)+b",
            "safe_regex": "true",
            # Round-4: regex mode requires ``since``; pick a window
            # generous enough to keep all three rows in scope.
            "since": "30d",
            # Round-4: deadline well above the 3-row worst case so we're
            # still measuring the per-line bound, not the request bound.
            "deadline_seconds": "30.0",
        },
        headers=auth_headers,
    )
    elapsed = time.monotonic() - start

    # 8 s ceiling: per-line timeout is 100 ms but each fresh worker
    # spawn after a kill adds ~50-1000 ms depending on host load. Three
    # pathological lines is ~3 s worst case; we double that for slack.
    # Without the guardrail this query would burn many minutes.
    assert elapsed < 8.0, f"pattern.search hung; elapsed={elapsed:.3f}s"
    assert response.status_code == 200, response.text
    body = response.json()
    # Every line timed out → no events matched.
    assert body["events"] == []
    # At least one line should have tripped the timeout — typically all
    # three do, but we only assert >= 1 to keep the test robust on a
    # faster future machine where some lines happen to complete in time.
    assert body.get("_pattern_timeouts", 0) >= 1, body


# ---------------------------------------------------------------------------
# Round-3 guardrails (Codex review on PR #2062): parsed-timestamp ``since``
# comparison + malformed-row gating for both grep AND stats.
# See ``src/pollypm/audit/query.py::iter_matching_events`` docstring step 4.
# ---------------------------------------------------------------------------


def test_audit_grep_since_handles_tz_offsets(
    client: TestClient,
    auth_headers: dict[str, str],
    project_root: Path,
    audit_home: Path,
) -> None:
    """``since`` must compare parsed datetimes, not raw ISO strings.

    Round-2 code did a lexicographic ``line < since_iso`` compare which
    let timezone-offset rows leak through: ``2026-05-21T01:00:00+02:00``
    is lexicographically AFTER ``2026-05-21T00:00:00+00:00`` but is
    actually ``2026-05-20T23:00:00Z`` — one hour BEFORE the cutoff, so
    it must be EXCLUDED. Round-3 fixes the compare by parsing both sides.
    """
    # 01:00 local with +02:00 offset = 23:00 UTC the previous day.
    older_tz_offset_ts = "2026-05-21T01:00:00+02:00"
    # Anything clearly fresher than the cutoff so we can confirm the
    # filter still admits valid rows in the same query.
    fresh_ts = "2026-05-21T05:00:00+00:00"
    _write_jsonl(
        _per_project_log(project_root),
        [
            _make_event(subject="myproj/should-be-excluded", ts=older_tz_offset_ts),
            _make_event(subject="myproj/should-be-kept", ts=fresh_ts),
        ],
    )
    response = client.get(
        "/api/v1/audit/grep",
        params={
            "project": "myproj",
            "since": "2026-05-21T00:00:00Z",
        },
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    subjects = [e["subject"] for e in response.json()["events"]]
    # The +02:00 row is older than the cutoff in real time; it MUST be
    # dropped. Round-2 (raw string compare) erroneously included it.
    assert "myproj/should-be-excluded" not in subjects
    assert subjects == ["myproj/should-be-kept"]


def test_audit_grep_malformed_ts_skipped(
    client: TestClient,
    auth_headers: dict[str, str],
    project_root: Path,
    audit_home: Path,
) -> None:
    """Rows whose ``ts`` doesn't parse must be excluded + counted.

    Independent of ``since`` — even with no time-window filter, a
    record with ``ts="not-a-date"`` is unsafe to keep (the response
    model needs a datetime, and downstream consumers expect ordered
    chronology). The walker drops it and increments the diagnostic
    counter so the operator sees that history was silently elided.
    """
    log_path = _per_project_log(project_root)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    good = _make_event(subject="myproj/good", ts="2026-05-21T00:00:00+00:00")
    bad = _make_event(subject="myproj/bad", ts="not-a-date")
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(good) + "\n")
        fh.write(json.dumps(bad) + "\n")

    response = client.get(
        "/api/v1/audit/grep",
        params={"project": "myproj"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    subjects = [e["subject"] for e in body["events"]]
    assert subjects == ["myproj/good"]
    # The malformed row was caught by the walker (round-3 parse gate)
    # rather than the older Pydantic-level coerce — either way the
    # counter on the response envelope must reflect it.
    assert body["_malformed_rows_skipped"] >= 1


def test_audit_stats_skips_malformed_rows(
    client: TestClient,
    auth_headers: dict[str, str],
    project_root: Path,
    audit_home: Path,
) -> None:
    """``/audit/stats`` total must NOT count malformed-ts rows.

    Round-2 stats iterated raw rows without validating ``ts`` — a
    corrupt archive row inflated ``total``. Round-3 routes stats
    through the same parse-gated walker, so totals stay symmetric
    with what ``/audit/grep`` would return.
    """
    log_path = _per_project_log(project_root)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    valid = [
        _make_event(event="task.created", ts="2026-05-21T00:00:00+00:00"),
        _make_event(event="task.created", ts="2026-05-21T00:01:00+00:00"),
    ]
    invalid = [
        _make_event(event="task.created", ts="not-a-date"),
        _make_event(event="task.created", ts="2026-13-45T99:99:99Z"),
    ]
    with open(log_path, "w", encoding="utf-8") as fh:
        for record in valid + invalid:
            fh.write(json.dumps(record) + "\n")

    response = client.get(
        "/api/v1/audit/stats",
        params={"project": "myproj", "since": "30d"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # Only the two valid rows count — the malformed pair is gated out
    # before reaching the by-event counter.
    assert body["total"] == 2
    assert body["by_event"] == {"task.created": 2}


# ---------------------------------------------------------------------------
# Round-4 guardrails (Codex review on PR #2062): request-level bound for the
# HTTP regex path. ``limit`` caps matches, not scanned lines, so a no-match
# pathological regex over many rows can still hold an API worker for roughly
# ``per_line_timeout * timed_out_lines``. The deadline + safe_regex-requires-
# since invariants bound that work explicitly and surface a diagnostic so
# callers can tell complete-but-empty from bounded-and-truncated.
# ---------------------------------------------------------------------------


def test_audit_grep_request_deadline_truncates_pathological_scan(
    client: TestClient,
    auth_headers: dict[str, str],
    project_root: Path,
    audit_home: Path,
) -> None:
    """Request-level deadline halts a no-match pathological regex scan.

    Round-3 left ``limit`` capping matches only; a no-match regex still
    walks every line. With many pathological rows the per-line timeout
    (100 ms) stacks: 30 rows = ~3 s, 100 = ~10 s, etc. ``deadline_s=1.0``
    must cap total wall-clock and surface ``_truncated_by_deadline=true``
    so the caller knows results are bounded, not complete. Verifies the
    regression FAILED on round-3 (no truncation field surfaced) and now
    PASSES with the request-level bound wired through.
    """
    import time

    # 60 pathological rows: each timed-out line costs ~100 ms timeout +
    # worker respawn (~500 ms on macOS) → un-bounded baseline is roughly
    # 60 × 0.6 s ≈ 36 s. The deadline must cut this off well before then.
    pathological_subject = "a" * 40 + "!"
    total_rows = 60
    _write_jsonl(
        _per_project_log(project_root),
        [_make_event(subject=pathological_subject) for _ in range(total_rows)],
    )

    start = time.monotonic()
    response = client.get(
        "/api/v1/audit/grep",
        params={
            "project": "myproj",
            "pattern": "(a+)+b",
            "safe_regex": "true",
            "since": "30d",
            "deadline_seconds": "1.0",
        },
        headers=auth_headers,
    )
    elapsed = time.monotonic() - start

    # The deadline check fires BEFORE each line is searched, but a line
    # already in flight when the deadline elapses still runs to per-line
    # completion (~100 ms timeout + ~500 ms respawn). 15 s ceiling sits
    # comfortably below the ~36 s un-bounded baseline (60 rows × ~600 ms)
    # while absorbing forkserver warmup variability on a loaded CI host.
    assert elapsed < 15.0, (
        f"deadline did not fire; elapsed={elapsed:.3f}s "
        f"(un-bounded would be ~{total_rows * 0.6:.0f}s)"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # Bounded-truncation diagnostic must be set so the caller can tell
    # this from a complete-but-empty scan.
    assert body.get("_truncated_by_deadline") is True, body
    # And the walker must have stopped strictly before the full row set
    # (otherwise the deadline didn't actually save work).
    assert body.get("_lines_scanned", 0) < total_rows, body
    assert body.get("_lines_scanned", 0) >= 1, body
    # No matches expected — pathological pattern never hits ``b``.
    assert body["events"] == []


def test_audit_grep_regex_mode_requires_since(
    client: TestClient,
    auth_headers: dict[str, str],
    project_root: Path,
    audit_home: Path,
) -> None:
    """``safe_regex=true`` without ``since`` → ``400 invalid_request``.

    Mirrors the round-2 ``/audit/stats`` invariant: regex mode walks
    every line in the window, so the window must be bounded. Literal-
    substring mode (default) is immune to ReDoS and stays unbounded.
    """
    _write_jsonl(
        _per_project_log(project_root),
        [_make_event(subject="myproj/whatever")],
    )
    response = client.get(
        "/api/v1/audit/grep",
        params={
            "project": "myproj",
            "pattern": "foo",
            "safe_regex": "true",
            # NB: no ``since`` — must 400.
        },
        headers=auth_headers,
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert "since" in body["error"]["message"].lower()
    hint = (body["error"].get("hint") or "").lower()
    assert "since" in hint or "literal" in hint

    # Sanity check: the same request WITH ``since`` must succeed (so the
    # 400 is specifically about the missing window, not the pattern).
    ok_response = client.get(
        "/api/v1/audit/grep",
        params={
            "project": "myproj",
            "pattern": "foo",
            "safe_regex": "true",
            "since": "30d",
        },
        headers=auth_headers,
    )
    assert ok_response.status_code == 200, ok_response.text


# ---------------------------------------------------------------------------
# Round-5 guardrail (Codex review on PR #2062): ``deadline_seconds`` must
# bound the FIRST regex line too. Round-4 wired a request-level deadline
# but ``_BoundedRegexSession.search`` still waited ``_STARTUP_GRACE_S = 5.0``
# on the first call, so a caller requesting ``deadline_seconds=0.5`` could
# still spend ~5 s on a pathological first line. The fix clamps both the
# steady-state per-line poll AND the first-line startup grace to the
# remaining request budget.
# ---------------------------------------------------------------------------
def test_audit_grep_deadline_bounds_first_pathological_line(
    client: TestClient,
    auth_headers: dict[str, str],
    project_root: Path,
    audit_home: Path,
) -> None:
    """A small ``deadline_seconds`` must cap the FIRST regex line too.

    Round-4 regression: even one pathological row blocks for
    ``_STARTUP_GRACE_S`` (5 s) before the walker can truncate, because
    the first call to the regex worker pays for forkserver warmup. A
    caller asking for ``deadline_seconds=0.5`` should NOT spend ~5 s.

    Verified to FAIL on round-4 (elapsed ≈ 5.0 s, well above 2 s
    ceiling) and PASS with the round-5 ``max_wall_clock_s`` plumbing.
    """
    import time

    pathological_subject = "a" * 40 + "!"
    # Just a handful of rows — the bug is on the FIRST line; even one
    # is enough to reproduce.
    _write_jsonl(
        _per_project_log(project_root),
        [_make_event(subject=pathological_subject) for _ in range(3)],
    )

    start = time.monotonic()
    response = client.get(
        "/api/v1/audit/grep",
        params={
            "project": "myproj",
            "pattern": "(a+)+b",
            "safe_regex": "true",
            "since": "30d",
            "deadline_seconds": "0.5",
        },
        headers=auth_headers,
    )
    elapsed = time.monotonic() - start

    # 2 s ceiling: deadline=0.5 s + one in-flight line clamped to ≤0.5 s
    # + worker respawn (~500 ms on macOS) + FastAPI/TestClient overhead.
    # Round-4 elapsed ≈ 5.0 s (the un-clamped startup grace), so any
    # ceiling between 2 s and 5 s would catch the bug; 2 s gives clear
    # signal without flaking on a loaded CI host.
    assert elapsed < 2.0, (
        f"deadline_seconds=0.5 did not bound first pathological line; "
        f"elapsed={elapsed:.3f}s (round-4 bug elapses ~5.0s due to "
        f"_STARTUP_GRACE_S)"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # The request was bounded by the deadline, so truncation must be
    # surfaced even though only one or two lines were scanned.
    assert body.get("_truncated_by_deadline") is True, body
    assert body["events"] == []


# ---------------------------------------------------------------------------
# Round-6 guardrail (Codex review on PR #2062): ``/audit/stats`` needs the
# same request-level wall-clock bound + diagnostic as ``/audit/grep``.
# Requiring ``since`` is only a semantic bound — the walker still reads
# every target file/line until it parses ``ts`` and filters, so a large
# archived history with a small ``since`` window can still monopolize the
# API worker on a synchronous GET.
# ---------------------------------------------------------------------------
def test_audit_stats_request_deadline_truncates_large_scan(
    client: TestClient,
    auth_headers: dict[str, str],
    project_root: Path,
    audit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``deadline_seconds`` must bound ``/audit/stats`` scan + diagnose it.

    Round-5 left stats calling ``iter_matching_events(..., deadline_s=None)``
    without scanned-line/byte caps or any ``truncated`` response field. A
    large old history can still force a full synchronous walk just to
    discard rows older than ``since``.

    Deterministic timing: monkeypatch the walker's ``parse_event_ts`` to
    add ~3 ms per row so 200 archived rows would unbounded-scan for
    ~600 ms. With ``deadline_seconds=0.5`` the walker MUST stop early,
    surface ``_truncated_by_deadline=true``, and the whole request must
    return in well under the un-bounded baseline. Monkeypatching the ts
    parser (rather than relying on raw row volume) keeps the test fast
    and immune to CI host speed variation.
    """
    import time

    from pollypm.audit import query as audit_query

    # Sentinel old timestamp — every row falls outside the ``since=24h``
    # window, so the walker exercises the parse → since-filter path on
    # every row but yields nothing (so the body's ``total`` would be 0
    # if the scan completed). Mirrors the realistic shape Codex
    # described: large old history + small since window.
    old_ts = "2020-01-01T00:00:00+00:00"
    total_rows = 200
    archive = _per_project_log(project_root).with_name(
        "audit.jsonl.1700000000.gz"
    )
    _write_jsonl_gz(
        archive,
        [
            _make_event(event="task.created", subject="old", ts=old_ts)
            for _ in range(total_rows)
        ],
    )
    os.utime(archive, (1_700_000_000, 1_700_000_000))

    # Slow ``parse_event_ts`` so the unbounded baseline is well above the
    # deadline. Slow ~3 ms per row → 200 rows ≈ 600 ms unbounded.
    real_parse_event_ts = audit_query.parse_event_ts

    def slow_parse_event_ts(value: object) -> object:
        time.sleep(0.003)
        return real_parse_event_ts(value)  # type: ignore[arg-type]

    monkeypatch.setattr(audit_query, "parse_event_ts", slow_parse_event_ts)

    start = time.monotonic()
    response = client.get(
        "/api/v1/audit/stats",
        params={
            "project": "myproj",
            "since": "24h",
            "deadline_seconds": "0.5",
        },
        headers=auth_headers,
    )
    elapsed = time.monotonic() - start

    # 2 s ceiling: deadline=0.5 s + one in-flight ts-parse (~3 ms) +
    # FastAPI/TestClient overhead. Un-bounded baseline is ~600 ms over
    # 200 rows; if the deadline didn't fire, the test would still finish
    # quickly on a fast host — but the truncation flag below pins the
    # actual behaviour the contract guarantees.
    assert elapsed < 2.0, (
        f"deadline_seconds=0.5 did not bound stats scan; "
        f"elapsed={elapsed:.3f}s "
        f"(un-bounded baseline ≈ {total_rows * 0.003:.2f}s)"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # Bounded-truncation diagnostic must be set so the caller can tell
    # this from a complete empty scan.
    assert body.get("_truncated_by_deadline") is True, body
    # The walker must have stopped strictly before the full row set
    # (otherwise the deadline didn't actually save work).
    assert 1 <= body.get("_lines_scanned", 0) < total_rows, body
    # Every row was outside ``since=24h`` so no aggregation is reported.
    assert body["total"] == 0
