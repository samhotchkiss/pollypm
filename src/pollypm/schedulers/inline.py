from __future__ import annotations

import fcntl
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from pollypm.atomic_io import atomic_write_json
from pollypm.job_runner import get_executor, submit_job
from pollypm.schedulers.base import ScheduledJob, SchedulerBackend


class InlineSchedulerBackend(SchedulerBackend):
    name = "inline"

    def schedule(
        self,
        supervisor,
        *,
        kind: str,
        run_at: datetime,
        payload: dict[str, object] | None = None,
        interval_seconds: int | None = None,
    ) -> ScheduledJob:
        job = ScheduledJob(
            job_id=uuid4().hex,
            kind=kind,
            run_at=run_at.astimezone(UTC),
            payload=dict(payload or {}),
            interval_seconds=interval_seconds,
        )
        jobs = self._load_jobs(supervisor)
        jobs.append(job)
        self._save_jobs(supervisor, jobs)
        supervisor.store.record_event(
            "scheduler",
            "scheduled",
            f"Scheduled job {job.kind} at {job.run_at.isoformat()}",
        )
        return job

    def list_jobs(self, supervisor) -> list[ScheduledJob]:
        return self._load_jobs(supervisor)

    def run_due(self, supervisor, *, now: datetime | None = None) -> list[ScheduledJob]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        jobs = self._load_jobs(supervisor)
        executed: list[ScheduledJob] = []
        dirty = False
        for job in jobs:
            if job.status != "pending" or job.run_at > current:
                continue
            dirty = True
            try:
                self._execute(supervisor, job)
            except Exception as exc:  # noqa: BLE001
                job.last_error = str(exc)
                if job.interval_seconds:
                    # Recurring jobs reschedule even after failure so they
                    # retry on the next cycle instead of being stuck forever.
                    job.run_at = current + timedelta(seconds=job.interval_seconds)
                    job.status = "pending"
                else:
                    job.status = "failed"
                supervisor.store.record_event(
                    "scheduler",
                    "failed",
                    f"Scheduled job {job.kind} failed: {exc}",
                )
            else:
                executed.append(job)
                if job.interval_seconds:
                    job.run_at = current + timedelta(seconds=job.interval_seconds)
                    job.status = "pending"
                    job.last_error = None
                else:
                    job.status = "done"
                supervisor.store.record_event(
                    "scheduler",
                    "ran",
                    f"Ran scheduled job {job.kind}",
                )
        if dirty:
            self._save_jobs(supervisor, jobs)
        return executed

    def _execute(self, supervisor, job: ScheduledJob) -> None:
        executor_fn = get_executor(job.kind)
        if executor_fn is None:
            raise RuntimeError(f"Unsupported scheduled job kind: {job.kind}")
        executor_fn(supervisor, job.payload)

    def _jobs_path(self, supervisor) -> Path:
        path = supervisor.config.project.base_dir / "scheduler" / "jobs.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _lock_path(self, supervisor) -> Path:
        return self._jobs_path(supervisor).with_suffix(".lock")

    def _load_jobs(self, supervisor) -> list[ScheduledJob]:
        path = self._jobs_path(supervisor)
        if not path.exists():
            return []
        lock = self._lock_path(supervisor)
        lock.touch(exist_ok=True)
        with lock.open("r") as lf:
            fcntl.flock(lf, fcntl.LOCK_SH)
            try:
                raw = json.loads(path.read_text())
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
        return [
            ScheduledJob(
                job_id=str(item["job_id"]),
                kind=str(item["kind"]),
                run_at=datetime.fromisoformat(str(item["run_at"])),
                payload=dict(item.get("payload", {})),
                status=str(item.get("status", "pending")),
                interval_seconds=item.get("interval_seconds"),
                last_error=item.get("last_error"),
            )
            for item in raw
        ]

    def _save_jobs(self, supervisor, jobs: list[ScheduledJob]) -> None:
        path = self._jobs_path(supervisor)
        lock = self._lock_path(supervisor)
        lock.touch(exist_ok=True)
        raw: list[dict[str, object]] = []
        for job in jobs:
            item = asdict(job)
            item["run_at"] = job.run_at.isoformat()
            raw.append(item)
        with lock.open("r") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                atomic_write_json(path, raw)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
