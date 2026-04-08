from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from promptmaster.schedulers.base import ScheduledJob, SchedulerBackend


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
                job.status = "failed"
                job.last_error = str(exc)
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
        if job.kind == "heartbeat":
            supervisor.run_heartbeat()
            return
        if job.kind == "send_input":
            supervisor.send_input(
                str(job.payload["session_name"]),
                str(job.payload["text"]),
                owner=str(job.payload.get("owner", "pm-bot")),
            )
            return
        if job.kind == "release_lease":
            supervisor.release_lease(
                str(job.payload["session_name"]),
                str(job.payload.get("owner", "human")),
            )
            return
        raise RuntimeError(f"Unsupported scheduled job kind: {job.kind}")

    def _jobs_path(self, supervisor) -> Path:
        path = supervisor.config.project.base_dir / "scheduler" / "jobs.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _load_jobs(self, supervisor) -> list[ScheduledJob]:
        path = self._jobs_path(supervisor)
        if not path.exists():
            return []
        raw = json.loads(path.read_text())
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
        raw: list[dict[str, object]] = []
        for job in jobs:
            item = asdict(job)
            item["run_at"] = job.run_at.isoformat()
            raw.append(item)
        path.write_text(json.dumps(raw, indent=2) + "\n")
