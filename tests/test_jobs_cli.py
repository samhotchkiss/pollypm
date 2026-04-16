"""Unit tests for the ``pm jobs`` CLI.

All tests wire a fresh ``JobQueue`` on a tmp_path DB via
``set_queue_factory`` so the commands do not touch the user's config.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm.jobs import JobQueue, JobStatus, exponential_backoff
from pollypm.jobs.cli import jobs_app, set_queue_factory


runner = CliRunner()


@pytest.fixture
def queue(tmp_path: Path):
    """A fresh queue on a tmp DB, wired into the CLI via the factory hook."""
    db_path = tmp_path / "jobs.db"

    # Tight retry policy so queued jobs show up as queued without delay.
    def factory(_config_path: Path) -> JobQueue:
        return JobQueue(
            db_path=db_path,
            retry_policy=exponential_backoff(
                base_seconds=0.01, factor=1.0, max_seconds=0.01, jitter=0
            ),
        )

    set_queue_factory(factory)
    try:
        # One long-lived handle for test assertions.
        q = JobQueue(db_path=db_path)
        yield q
        q.close()
    finally:
        set_queue_factory(None)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_empty(queue: JobQueue) -> None:
    result = runner.invoke(jobs_app, ["list"])
    assert result.exit_code == 0, result.stderr
    assert "No jobs match" in result.stdout


def test_list_returns_jobs(queue: JobQueue) -> None:
    queue.enqueue("inbox.sweep", {"project": "alpha"})
    queue.enqueue("session.health_sweep", {})

    result = runner.invoke(jobs_app, ["list"])
    assert result.exit_code == 0, result.stderr
    assert "inbox.sweep" in result.stdout
    assert "session.health_sweep" in result.stdout


def test_list_filters_by_status(queue: JobQueue) -> None:
    jid1 = queue.enqueue("h.a")
    queue.enqueue("h.b")
    (claimed,) = queue.claim("w")
    queue.complete(claimed.id)

    result = runner.invoke(jobs_app, ["list", "--status", "done"])
    assert result.exit_code == 0, result.stderr
    assert "h.a" in result.stdout
    assert "h.b" not in result.stdout

    result = runner.invoke(jobs_app, ["list", "--status", "queued"])
    assert result.exit_code == 0, result.stderr
    assert "h.b" in result.stdout
    assert "h.a" not in result.stdout


def test_list_filters_by_handler(queue: JobQueue) -> None:
    queue.enqueue("inbox.sweep")
    queue.enqueue("inbox.sweep")
    queue.enqueue("other")

    result = runner.invoke(jobs_app, ["list", "--handler", "inbox.sweep"])
    assert result.exit_code == 0, result.stderr
    assert result.stdout.count("inbox.sweep") == 2
    assert "other" not in result.stdout


def test_list_respects_limit(queue: JobQueue) -> None:
    for i in range(5):
        queue.enqueue(f"h{i}")
    result = runner.invoke(jobs_app, ["list", "--limit", "2"])
    assert result.exit_code == 0, result.stderr
    lines = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
    assert len(lines) == 2


def test_list_rejects_bad_status(queue: JobQueue) -> None:
    result = runner.invoke(jobs_app, ["list", "--status", "weird"])
    assert result.exit_code != 0


def test_list_json(queue: JobQueue) -> None:
    jid = queue.enqueue("inbox.sweep", {"project": "alpha"})
    result = runner.invoke(jobs_app, ["list", "--json"])
    assert result.exit_code == 0, result.stderr
    data = json.loads(result.stdout)
    assert len(data) == 1
    assert data[0]["id"] == jid
    assert data[0]["handler"] == "inbox.sweep"
    assert data[0]["payload"] == {"project": "alpha"}


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_returns_details(queue: JobQueue) -> None:
    jid = queue.enqueue("capacity.probe", {"region": "local"}, dedupe_key="cap")
    result = runner.invoke(jobs_app, ["show", str(jid)])
    assert result.exit_code == 0, result.stderr
    assert "capacity.probe" in result.stdout
    assert "cap" in result.stdout  # dedupe_key


def test_show_missing_job(queue: JobQueue) -> None:
    result = runner.invoke(jobs_app, ["show", "99999"])
    assert result.exit_code == 1
    assert "not found" in result.stderr


def test_show_json_includes_last_error(queue: JobQueue) -> None:
    jid = queue.enqueue("boom")
    (job,) = queue.claim("w")
    queue.fail(job.id, "it broke", retry=False)

    result = runner.invoke(jobs_app, ["show", str(jid), "--json"])
    assert result.exit_code == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["status"] == "failed"
    assert data["last_error"] == "it broke"


# ---------------------------------------------------------------------------
# retry
# ---------------------------------------------------------------------------


def test_retry_moves_failed_back_to_queued_and_resets_attempt(queue: JobQueue) -> None:
    jid = queue.enqueue("boom")
    (job,) = queue.claim("w")
    queue.fail(job.id, "it broke", retry=False)
    assert queue.get(jid).status is JobStatus.FAILED  # type: ignore[union-attr]

    result = runner.invoke(jobs_app, ["retry", str(jid)])
    assert result.exit_code == 0, result.stderr

    after = queue.get(jid)
    assert after is not None
    assert after.status is JobStatus.QUEUED
    assert after.attempt == 0
    assert queue.get_last_error(jid) is None


def test_retry_refuses_non_failed(queue: JobQueue) -> None:
    jid = queue.enqueue("h")
    result = runner.invoke(jobs_app, ["retry", str(jid)])
    assert result.exit_code == 1
    assert "not failed" in result.stderr


def test_retry_missing_job(queue: JobQueue) -> None:
    result = runner.invoke(jobs_app, ["retry", "99999"])
    assert result.exit_code == 1
    assert "not found" in result.stderr


# ---------------------------------------------------------------------------
# purge
# ---------------------------------------------------------------------------


def test_purge_failed_deletes_only_failed(queue: JobQueue) -> None:
    ok_id = queue.enqueue("ok")
    bad_id = queue.enqueue("bad")
    (ok_job,) = queue.claim("w1")
    queue.complete(ok_job.id)
    (bad_job,) = queue.claim("w1")
    queue.fail(bad_job.id, "x", retry=False)

    # Confirm both terminal.
    assert queue.get(ok_id).status is JobStatus.DONE  # type: ignore[union-attr]
    assert queue.get(bad_id).status is JobStatus.FAILED  # type: ignore[union-attr]

    result = runner.invoke(jobs_app, ["purge", "--status", "failed"])
    assert result.exit_code == 0, result.stderr
    assert "Purged 1" in result.stdout

    assert queue.get(bad_id) is None
    assert queue.get(ok_id) is not None


def test_purge_rejects_queued(queue: JobQueue) -> None:
    result = runner.invoke(jobs_app, ["purge", "--status", "queued"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# drain
# ---------------------------------------------------------------------------


def test_drain_succeeds_when_empty(queue: JobQueue) -> None:
    result = runner.invoke(jobs_app, ["drain", "--timeout", "1"])
    assert result.exit_code == 0, result.stderr
    assert "drained" in result.stdout.lower()


def test_drain_times_out_when_pending(queue: JobQueue) -> None:
    queue.enqueue("slow")
    result = runner.invoke(
        jobs_app,
        ["drain", "--timeout", "0.1", "--poll-interval", "0.05"],
    )
    assert result.exit_code == 1
    assert "Timed out" in result.stderr


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def test_stats_reports_counts(queue: JobQueue) -> None:
    queue.enqueue("a")
    queue.enqueue("a")
    queue.enqueue("b")
    (job,) = queue.claim("w")
    queue.complete(job.id)

    result = runner.invoke(jobs_app, ["stats"])
    assert result.exit_code == 0, result.stderr
    assert "queued  = 2" in result.stdout
    assert "done    = 1" in result.stdout
    assert "a" in result.stdout


def test_stats_json(queue: JobQueue) -> None:
    queue.enqueue("a")
    queue.enqueue("b")
    result = runner.invoke(jobs_app, ["stats", "--json"])
    assert result.exit_code == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["queued"] == 2
    assert data["total"] == 2
    assert any(item["handler"] == "a" for item in data["top_handlers"])
