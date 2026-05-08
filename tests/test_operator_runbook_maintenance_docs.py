from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = PROJECT_ROOT / "src/pollypm/defaults/docs/reference/operator-runbook.md"


def test_operator_runbook_documents_background_maintenance_surfaces() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")

    required_terms = [
        "Understand Background Maintenance",
        "log rotation",
        "socket reaper",
        "worker-marker reaper",
        "pollypm.plugins_builtin.core_recurring.maintenance::log_rotate_handler",
        "pollypm.log_rotation::make_rotating_file_handler",
        "pollypm.log_rotation::bootstrap_truncate_if_too_big",
        "RotatingFileHandler",
        "[logging]",
        "rotate_size_mb",
        "rotate_keep",
        "pollypm.cockpit_socket_reaper::reap_stale_cockpit_sockets",
        "cockpit_inputs",
        "socket.reaped",
        "pollypm.work.worker_marker_reaper::reap_orphan_worker_markers",
        "pollypm.work.worker_marker_reaper::sweep_worker_markers",
        "worker-markers",
        "worker.session_reaped",
        "There is no tuning knob and no supported disable switch.",
    ]

    missing = [term for term in required_terms if term not in text]
    assert missing == []
