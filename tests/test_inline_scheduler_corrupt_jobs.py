"""Cycle 119: defend ``InlineSchedulerBackend._load_jobs`` against
corrupt job files.

The previous implementation called ``json.loads`` inside the file
lock and assumed the result was a list of dicts. A corrupt or hand-
edited ``scheduler/jobs.json`` (parse error / non-list / non-dict
entry) would crash the entire scheduler tick, potentially halting
every recurring background job until the bad file was repaired.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pollypm.schedulers.inline import InlineSchedulerBackend


def _supervisor(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(project=SimpleNamespace(base_dir=tmp_path))
    )


def test_load_jobs_returns_empty_on_malformed_json(tmp_path: Path) -> None:
    sched = InlineSchedulerBackend()
    sup = _supervisor(tmp_path)
    jobs_path = tmp_path / "scheduler" / "jobs.json"
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    jobs_path.write_text("{not valid json")
    # Without the fix the bare json.loads raised ValueError out of the
    # lock context, killing the scheduler tick.
    assert sched._load_jobs(sup) == []


def test_load_jobs_returns_empty_when_top_level_is_not_a_list(tmp_path: Path) -> None:
    sched = InlineSchedulerBackend()
    sup = _supervisor(tmp_path)
    jobs_path = tmp_path / "scheduler" / "jobs.json"
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    # Parses fine, but ``for item in raw`` would iterate dict keys.
    jobs_path.write_text('{"oops": "not a list"}')
    assert sched._load_jobs(sup) == []


def test_load_jobs_skips_bad_entries_and_returns_good_ones(tmp_path: Path) -> None:
    """A single malformed entry shouldn't poison the whole queue —
    other valid jobs still run."""
    sched = InlineSchedulerBackend()
    sup = _supervisor(tmp_path)
    jobs_path = tmp_path / "scheduler" / "jobs.json"
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    jobs_path.write_text(
        '['
        '"oops not a dict",'
        '{"job_id": "good", "kind": "demo", "run_at": "2026-04-26T00:00:00+00:00", "payload": {}},'
        '{"job_id": "missing-required-fields"}'
        ']'
    )
    jobs = sched._load_jobs(sup)
    assert [j.job_id for j in jobs] == ["good"]
    assert jobs[0].kind == "demo"
