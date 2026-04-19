"""Canonical PollyPM GitHub label mappings.

The GitHub issue tracker exposes a six-state board model. Work-service
statuses that do not have their own GitHub label today intentionally
collapse onto the nearest board state so every integration shares the
same repo-level label contract.
"""

from __future__ import annotations

from pollypm.work.models import WorkStatus


TRACKER_STATE_TO_GITHUB_LABEL: dict[str, str] = {
    "00-not-ready": "polly:not-ready",
    "01-ready": "polly:ready",
    "02-in-progress": "polly:in-progress",
    "03-needs-review": "polly:needs-review",
    "04-in-review": "polly:in-review",
    "05-completed": "polly:completed",
}

GITHUB_LABEL_TO_TRACKER_STATE: dict[str, str] = {
    label: state for state, label in TRACKER_STATE_TO_GITHUB_LABEL.items()
}

ALL_POLLY_GITHUB_LABELS: tuple[str, ...] = tuple(
    TRACKER_STATE_TO_GITHUB_LABEL.values()
)

WORK_STATUS_TO_GITHUB_LABEL: dict[str, str] = {
    WorkStatus.DRAFT.value: TRACKER_STATE_TO_GITHUB_LABEL["00-not-ready"],
    WorkStatus.QUEUED.value: TRACKER_STATE_TO_GITHUB_LABEL["01-ready"],
    WorkStatus.IN_PROGRESS.value: TRACKER_STATE_TO_GITHUB_LABEL["02-in-progress"],
    WorkStatus.REVIEW.value: TRACKER_STATE_TO_GITHUB_LABEL["03-needs-review"],
    WorkStatus.DONE.value: TRACKER_STATE_TO_GITHUB_LABEL["05-completed"],
    WorkStatus.CANCELLED.value: TRACKER_STATE_TO_GITHUB_LABEL["05-completed"],
    WorkStatus.BLOCKED.value: TRACKER_STATE_TO_GITHUB_LABEL["02-in-progress"],
    WorkStatus.ON_HOLD.value: TRACKER_STATE_TO_GITHUB_LABEL["00-not-ready"],
}
