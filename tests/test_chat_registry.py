"""Unit tests for the chat-API surface registry (P1 spec §1).

Synthesizes a :class:`PollyPMConfig` with all four surface types and a
mock work-service emitting :class:`WorkerSessionRecord` rows. Asserts
:func:`enumerate_chat_surfaces` returns one :class:`ChatSurface` per
expected surface, with the correct persona, project, tmux window, and
transcript-path resolution.

Also covers:

- Disabled sessions are filtered out
- Non-chat roles (heartbeat-supervisor, reviewer) are filtered out
- Persona fallback (Polly / Archie / Advisor) when project lacks a persona
- TmuxClient probing populates window.present + pane_id
- Worker session_name follows ``task-{project}-{N}`` convention
- ``ChatSurface.to_dict`` produces the spec §2.1 shape
- Stable surface ordering (operator → architect → advisor → worker)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pollypm.models import (
    AccountConfig,
    KnownProject,
    PollyPMConfig,
    PollyPMSettings,
    ProjectKind,
    ProjectSettings,
    ProviderKind,
    SessionConfig,
)
from pollypm.tmux.client import TmuxWindow
from pollypm.web_api.chat import (
    ChatSurface,
    SurfaceType,
    enumerate_chat_surfaces,
    enumerate_config_surfaces,
    enumerate_worker_surfaces,
)
from pollypm.web_api.chat.registry import (
    TmuxWindowState,
    _classify_session,
    _surface_sort_key,
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _build_config(
    tmp_path: Path,
    *,
    sessions: dict[str, SessionConfig] | None = None,
    projects: dict[str, KnownProject] | None = None,
) -> PollyPMConfig:
    project_root = tmp_path / "repo"
    project_root.mkdir(exist_ok=True)
    sessions = sessions or {}
    projects = projects or {
        "samblog": KnownProject(
            key="samblog",
            path=project_root,
            name="Sam Blog",
            persona_name="Archie",
            kind=ProjectKind.GIT,
        ),
    }
    return PollyPMConfig(
        project=ProjectSettings(
            name="pollypm",
            root_dir=project_root,
            base_dir=project_root / ".pollypm",
            logs_dir=project_root / ".pollypm/logs",
            snapshots_dir=project_root / ".pollypm/snapshots",
            state_db=project_root / ".pollypm/state.db",
            tmux_session="storage-closet",
        ),
        pollypm=PollyPMSettings(controller_account="claude_main"),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=project_root / ".pollypm/homes/claude_main",
            ),
        },
        sessions=sessions,
        projects=projects,
    )


def _session(name: str, role: str, **kwargs) -> SessionConfig:
    defaults = {
        "name": name,
        "role": role,
        "provider": ProviderKind.CLAUDE,
        "account": "claude_main",
        "cwd": Path("/tmp/work"),
    }
    defaults.update(kwargs)
    return SessionConfig(**defaults)


@dataclass
class _StubWorkerRecord:
    task_project: str
    task_number: int
    agent_name: str = "worker"
    pane_id: str | None = "%42"
    worktree_path: str | None = None
    branch_name: str | None = None
    started_at: str = "2026-05-21T20:00:00Z"
    ended_at: str | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    archive_path: str | None = None
    provider: str | None = "claude"
    provider_home: str | None = None


class _StubWorkService:
    """Minimal work-service stand-in returning canned WorkerSession rows."""

    def __init__(self, records: list[_StubWorkerRecord]) -> None:
        self._records = records
        self.calls: list[dict[str, Any]] = []

    def list_worker_sessions(
        self, *, active_only: bool = True,
    ) -> list[_StubWorkerRecord]:
        self.calls.append({"active_only": active_only})
        return list(self._records)


class _StubTmuxClient:
    """TmuxClient stand-in that returns canned ``list_windows`` results."""

    def __init__(self, windows: list[TmuxWindow]) -> None:
        self._windows = windows
        self.list_calls: list[str] = []

    def list_windows(self, target: str) -> list[TmuxWindow]:
        self.list_calls.append(target)
        return list(self._windows)


# ---------------------------------------------------------------------------
# All four surface types end-to-end
# ---------------------------------------------------------------------------


def test_enumerate_returns_all_four_surface_types(tmp_path: Path) -> None:
    config = _build_config(tmp_path, sessions={
        "operator": _session("operator", role="operator-pm", project=""),
        "architect_samblog": _session(
            "architect_samblog", role="architect", project="samblog",
            window_name="architect-samblog",
        ),
        "advisor_samblog": _session(
            "advisor_samblog", role="advisor", project="samblog",
            window_name="advisor-samblog",
        ),
    })
    worker_records = [
        _StubWorkerRecord(task_project="samblog", task_number=47),
    ]
    surfaces = enumerate_chat_surfaces(
        config, work_service=_StubWorkService(worker_records),
    )
    types = [s.surface_type for s in surfaces]
    assert SurfaceType.OPERATOR in types
    assert SurfaceType.ARCHITECT in types
    assert SurfaceType.ADVISOR in types
    assert SurfaceType.WORKER in types
    # Stable ordering: operator first, worker last.
    assert types[0] == SurfaceType.OPERATOR
    assert types[-1] == SurfaceType.WORKER


def test_operator_surface_has_polly_persona_and_no_project(tmp_path: Path) -> None:
    config = _build_config(tmp_path, sessions={
        "operator": _session("operator", role="operator-pm"),
    })
    surfaces = enumerate_chat_surfaces(config)
    assert len(surfaces) == 1
    assert surfaces[0].persona == "Polly"
    assert surfaces[0].project is None
    assert surfaces[0].surface_type == SurfaceType.OPERATOR


def test_architect_surface_carries_project_persona(tmp_path: Path) -> None:
    config = _build_config(tmp_path, sessions={
        "architect_samblog": _session(
            "architect_samblog", role="architect", project="samblog",
        ),
    })
    surfaces = enumerate_chat_surfaces(config)
    assert surfaces[0].project == "samblog"
    assert surfaces[0].persona == "Archie"
    assert surfaces[0].surface_type == SurfaceType.ARCHITECT


def test_architect_surface_falls_back_to_archie_when_no_persona(tmp_path: Path) -> None:
    config = _build_config(
        tmp_path,
        sessions={
            "architect_other": _session(
                "architect_other", role="architect", project="other",
            ),
        },
        projects={
            "other": KnownProject(
                key="other", path=tmp_path / "repo", persona_name=None,
            ),
        },
    )
    surfaces = enumerate_chat_surfaces(config)
    assert surfaces[0].persona == "Archie"


def test_advisor_surface_falls_back_to_advisor_when_no_persona(tmp_path: Path) -> None:
    config = _build_config(
        tmp_path,
        sessions={
            "advisor_other": _session(
                "advisor_other", role="advisor", project="other",
            ),
        },
        projects={
            "other": KnownProject(
                key="other", path=tmp_path / "repo", persona_name=None,
            ),
        },
    )
    surfaces = enumerate_chat_surfaces(config)
    assert surfaces[0].persona == "Advisor"


# ---------------------------------------------------------------------------
# Filtering: disabled / non-chat roles
# ---------------------------------------------------------------------------


def test_disabled_sessions_are_skipped(tmp_path: Path) -> None:
    config = _build_config(tmp_path, sessions={
        "operator": _session("operator", role="operator-pm"),
        "architect_off": _session(
            "architect_off", role="architect", project="samblog",
            enabled=False,
        ),
    })
    surfaces = enumerate_chat_surfaces(config)
    names = [s.session_name for s in surfaces]
    assert "operator" in names
    assert "architect_off" not in names


def test_non_chat_roles_are_skipped(tmp_path: Path) -> None:
    config = _build_config(tmp_path, sessions={
        "heartbeat": _session("heartbeat", role="heartbeat-supervisor"),
        "reviewer-samblog-1": _session(
            "reviewer-samblog-1", role="reviewer", project="samblog",
        ),
        "operator": _session("operator", role="operator-pm"),
    })
    surfaces = enumerate_chat_surfaces(config)
    names = [s.session_name for s in surfaces]
    assert names == ["operator"]


# ---------------------------------------------------------------------------
# Classification edge cases
# ---------------------------------------------------------------------------


def test_classify_recognizes_operator_by_name() -> None:
    # A session named ``operator`` with no role still classifies.
    session = SessionConfig(
        name="operator",
        role="",
        provider=ProviderKind.CLAUDE,
        account="claude_main",
        cwd=Path("."),
    )
    assert _classify_session(session) == SurfaceType.OPERATOR


def test_classify_recognizes_architect_by_name_prefix() -> None:
    session = SessionConfig(
        name="architect_samblog",
        role="",
        provider=ProviderKind.CLAUDE,
        account="claude_main",
        cwd=Path("."),
    )
    assert _classify_session(session) == SurfaceType.ARCHITECT


def test_classify_recognizes_advisor_by_name_prefix() -> None:
    session = SessionConfig(
        name="advisor_samblog",
        role="",
        provider=ProviderKind.CLAUDE,
        account="claude_main",
        cwd=Path("."),
    )
    assert _classify_session(session) == SurfaceType.ADVISOR


def test_classify_returns_none_for_unknown_role() -> None:
    session = SessionConfig(
        name="random",
        role="custom-role",
        provider=ProviderKind.CLAUDE,
        account="claude_main",
        cwd=Path("."),
    )
    assert _classify_session(session) is None


def test_classify_handles_case_insensitive_roles() -> None:
    session = SessionConfig(
        name="x",
        role="ARCHITECT",
        provider=ProviderKind.CLAUDE,
        account="claude_main",
        cwd=Path("."),
    )
    assert _classify_session(session) == SurfaceType.ARCHITECT


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------


def test_worker_session_name_uses_task_window_convention(tmp_path: Path) -> None:
    config = _build_config(tmp_path)
    work_service = _StubWorkService([
        _StubWorkerRecord(task_project="samblog", task_number=47),
        _StubWorkerRecord(task_project="other", task_number=3),
    ])
    surfaces = enumerate_worker_surfaces(config, work_service)
    names = sorted(s.session_name for s in surfaces)
    assert names == ["task-other-3", "task-samblog-47"]


def test_worker_surface_includes_task_id_and_project(tmp_path: Path) -> None:
    config = _build_config(tmp_path)
    work_service = _StubWorkService([
        _StubWorkerRecord(
            task_project="samblog",
            task_number=47,
            worktree_path="/Users/x/wt-47",
            provider="claude",
        ),
    ])
    surfaces = enumerate_worker_surfaces(config, work_service)
    assert len(surfaces) == 1
    surface = surfaces[0]
    assert surface.task_id == 47
    assert surface.project == "samblog"
    assert surface.worktree_path == Path("/Users/x/wt-47")
    assert surface.provider == "claude"
    assert surface.persona is None  # workers have no persona


def test_worker_surfaces_empty_when_work_service_has_no_records(tmp_path: Path) -> None:
    config = _build_config(tmp_path)
    work_service = _StubWorkService([])
    assert enumerate_worker_surfaces(config, work_service) == []


def test_enumerate_skips_workers_when_no_work_service(tmp_path: Path) -> None:
    config = _build_config(tmp_path, sessions={
        "operator": _session("operator", role="operator-pm"),
    })
    surfaces = enumerate_chat_surfaces(config, work_service=None)
    assert all(s.surface_type != SurfaceType.WORKER for s in surfaces)


def test_worker_enumeration_skips_records_with_missing_project_or_task(
    tmp_path: Path,
) -> None:
    config = _build_config(tmp_path)
    work_service = _StubWorkService([
        _StubWorkerRecord(task_project="", task_number=1),
        _StubWorkerRecord(task_project="samblog", task_number=0),
        _StubWorkerRecord(task_project="samblog", task_number=2),
    ])
    surfaces = enumerate_worker_surfaces(config, work_service)
    assert [s.session_name for s in surfaces] == ["task-samblog-2"]


def test_worker_enumeration_tolerates_service_exception(tmp_path: Path) -> None:
    config = _build_config(tmp_path)

    class _BrokenService:
        def list_worker_sessions(self, **_kw):
            raise RuntimeError("backend offline")

    assert enumerate_worker_surfaces(config, _BrokenService()) == []


def test_worker_enumeration_skips_when_protocol_unsupported(tmp_path: Path) -> None:
    config = _build_config(tmp_path)
    assert enumerate_worker_surfaces(config, object()) == []


# ---------------------------------------------------------------------------
# Tmux probing
# ---------------------------------------------------------------------------


def test_tmux_client_populates_window_present_and_pane_id(tmp_path: Path) -> None:
    config = _build_config(tmp_path, sessions={
        "operator": _session(
            "operator", role="operator-pm", window_name="pm-operator",
        ),
    })
    tmux = _StubTmuxClient([
        TmuxWindow(
            session="storage-closet",
            index=1,
            name="pm-operator",
            active=True,
            pane_id="%17",
            pane_current_command="claude",
            pane_current_path="/x",
            pane_dead=False,
        ),
    ])
    surfaces = enumerate_chat_surfaces(config, tmux_client=tmux)
    assert surfaces[0].window.present is True
    assert surfaces[0].window.pane_id == "%17"
    assert surfaces[0].window.tmux_session == "storage-closet"
    assert tmux.list_calls == ["storage-closet"]


def test_window_present_false_when_tmux_returns_no_window(tmp_path: Path) -> None:
    config = _build_config(tmp_path, sessions={
        "operator": _session(
            "operator", role="operator-pm", window_name="pm-operator",
        ),
    })
    tmux = _StubTmuxClient(windows=[])
    surfaces = enumerate_chat_surfaces(config, tmux_client=tmux)
    assert surfaces[0].window.present is False
    assert surfaces[0].window.window_name == "pm-operator"


def test_window_present_false_when_no_tmux_client(tmp_path: Path) -> None:
    config = _build_config(tmp_path, sessions={
        "operator": _session(
            "operator", role="operator-pm", window_name="pm-operator",
        ),
    })
    surfaces = enumerate_chat_surfaces(config)
    assert surfaces[0].window.present is False


def test_tmux_probe_failure_falls_back_to_unknown_state(tmp_path: Path) -> None:
    config = _build_config(tmp_path, sessions={
        "operator": _session("operator", role="operator-pm"),
    })

    class _BrokenTmux:
        def list_windows(self, _target):
            raise OSError("tmux exploded")

    surfaces = enumerate_chat_surfaces(config, tmux_client=_BrokenTmux())
    assert surfaces[0].window.present is False


def test_pane_dead_propagates_from_tmux(tmp_path: Path) -> None:
    config = _build_config(tmp_path, sessions={
        "operator": _session(
            "operator", role="operator-pm", window_name="pm-operator",
        ),
    })
    tmux = _StubTmuxClient([
        TmuxWindow(
            session="storage-closet",
            index=1,
            name="pm-operator",
            active=False,
            pane_id="%99",
            pane_current_command="dead",
            pane_current_path="/x",
            pane_dead=True,
        ),
    ])
    surfaces = enumerate_chat_surfaces(config, tmux_client=tmux)
    assert surfaces[0].window.pane_dead is True


# ---------------------------------------------------------------------------
# Transcript-path resolution wiring
# ---------------------------------------------------------------------------


def test_surface_transcript_path_resolves_from_events_jsonl(tmp_path: Path) -> None:
    config = _build_config(tmp_path, sessions={
        "operator": _session(
            "operator", role="operator-pm",
            cwd=tmp_path / "repo",
        ),
    })
    # Lay an events.jsonl in the project's transcripts dir.
    transcripts = config.project.root_dir / ".pollypm" / "transcripts" / "uuid-1"
    transcripts.mkdir(parents=True)
    event = {
        "timestamp": "2026-05-21T20:48:11Z",
        "event_type": "user_turn",
        "session_id": "uuid-1",
        "account_name": "claude_main",
        "provider": "claude",
        "project_key": "pollypm",
        "source_path": "/tmp/raw",
        "source_offset": 0,
        "cwd": str(tmp_path / "repo"),
        "payload": {"text": "x"},
    }
    (transcripts / "events.jsonl").write_text(json.dumps(event) + "\n")
    surfaces = enumerate_chat_surfaces(config)
    assert surfaces[0].transcript_path == transcripts / "events.jsonl"


def test_surface_transcript_path_none_when_archive_missing(tmp_path: Path) -> None:
    config = _build_config(tmp_path, sessions={
        "operator": _session("operator", role="operator-pm"),
    })
    surfaces = enumerate_chat_surfaces(config)
    assert surfaces[0].transcript_path is None


def test_two_surfaces_sharing_cwd_resolve_to_correct_transcripts(
    tmp_path: Path,
) -> None:
    """Codex review #2044 — blocker 1 regression test.

    Operator + architect both run with ``cwd = project_root``. Without
    the account-based fingerprint disambiguator, the prior freshest-mtime
    fallback would cross-attach: both surfaces would resolve to whichever
    transcript was written last. With the fingerprint lookup, each
    surface resolves to its OWN transcript exactly.
    """
    project_root = tmp_path / "repo"
    project_root.mkdir(exist_ok=True)
    accounts = {
        "claude_main": AccountConfig(
            name="claude_main",
            provider=ProviderKind.CLAUDE,
            home=project_root / ".pollypm/homes/claude_main",
        ),
        "claude_alt": AccountConfig(
            name="claude_alt",
            provider=ProviderKind.CLAUDE,
            home=project_root / ".pollypm/homes/claude_alt",
        ),
    }
    config = PollyPMConfig(
        project=ProjectSettings(
            name="pollypm",
            root_dir=project_root,
            base_dir=project_root / ".pollypm",
            logs_dir=project_root / ".pollypm/logs",
            snapshots_dir=project_root / ".pollypm/snapshots",
            state_db=project_root / ".pollypm/state.db",
            tmux_session="storage-closet",
        ),
        pollypm=PollyPMSettings(controller_account="claude_main"),
        accounts=accounts,
        sessions={
            "operator": _session(
                "operator", role="operator-pm",
                account="claude_main", cwd=project_root,
            ),
            "architect_pollypm": _session(
                "architect_pollypm", role="architect", project="pollypm",
                account="claude_alt", cwd=project_root,
            ),
        },
        projects={
            "pollypm": KnownProject(
                key="pollypm", path=project_root, persona_name="Archie",
                kind=ProjectKind.GIT,
            ),
        },
    )
    transcripts = project_root / ".pollypm" / "transcripts"
    # Plant operator transcript first (older mtime).
    op_dir = transcripts / "session-op-uuid"
    op_dir.mkdir(parents=True)
    op_event = {
        "timestamp": "2026-05-21T20:00:00Z",
        "event_type": "user_turn",
        "session_id": "session-op-uuid",
        "account_name": "claude_main",
        "provider": "claude",
        "project_key": "pollypm",
        "source_path": "/tmp/raw-op",
        "source_offset": 0,
        "cwd": str(project_root),
        "payload": {"text": "from operator"},
    }
    (op_dir / "events.jsonl").write_text(json.dumps(op_event) + "\n")
    import time as _time
    _time.sleep(0.05)
    # Architect's transcript is the freshest — if the old "freshest mtime"
    # fallback were still in place, the operator surface would
    # cross-attach to this one.
    arch_dir = transcripts / "session-arch-uuid"
    arch_dir.mkdir(parents=True)
    arch_event = {
        "timestamp": "2026-05-21T21:00:00Z",
        "event_type": "user_turn",
        "session_id": "session-arch-uuid",
        "account_name": "claude_alt",
        "provider": "claude",
        "project_key": "pollypm",
        "source_path": "/tmp/raw-arch",
        "source_offset": 0,
        "cwd": str(project_root),
        "payload": {"text": "from architect"},
    }
    (arch_dir / "events.jsonl").write_text(json.dumps(arch_event) + "\n")

    surfaces = enumerate_chat_surfaces(config)
    by_name = {s.session_name: s for s in surfaces}
    assert by_name["operator"].transcript_path == op_dir / "events.jsonl"
    assert by_name["architect_pollypm"].transcript_path == arch_dir / "events.jsonl"
    # Symmetric assertion: neither surface attached to the other's
    # transcript, even though both shared the same cwd.
    assert by_name["operator"].transcript_path != by_name["architect_pollypm"].transcript_path


# ---------------------------------------------------------------------------
# auth_token_present surfacing
# ---------------------------------------------------------------------------


def test_auth_token_present_reflects_session_field(tmp_path: Path) -> None:
    config = _build_config(tmp_path, sessions={
        "operator": _session(
            "operator", role="operator-pm", auth_token="0123abcd",
        ),
        "architect_samblog": _session(
            "architect_samblog", role="architect", project="samblog",
        ),
    })
    surfaces = enumerate_chat_surfaces(config)
    by_name = {s.session_name: s for s in surfaces}
    assert by_name["operator"].auth_token_present is True
    assert by_name["architect_samblog"].auth_token_present is False


# ---------------------------------------------------------------------------
# to_dict serialization
# ---------------------------------------------------------------------------


def test_chat_surface_to_dict_matches_spec_shape(tmp_path: Path) -> None:
    config = _build_config(tmp_path, sessions={
        "architect_samblog": _session(
            "architect_samblog", role="architect", project="samblog",
            window_name="architect-samblog",
        ),
    })
    surfaces = enumerate_chat_surfaces(config)
    payload = surfaces[0].to_dict()
    assert payload["session_name"] == "architect_samblog"
    assert payload["surface_type"] == "architect"
    assert payload["persona"] == "Archie"
    assert payload["project"] == "samblog"
    assert payload["task_id"] is None
    assert payload["window"]["window_name"] == "architect-samblog"
    assert payload["window"]["present"] is False
    assert payload["transcript"]["source"] is None
    assert payload["transcript"]["path"] is None
    assert payload["auth_token_present"] is False
    assert payload["provider"] == "claude"


def test_worker_surface_to_dict_includes_task_id_and_worktree(tmp_path: Path) -> None:
    config = _build_config(tmp_path)
    surfaces = enumerate_worker_surfaces(config, _StubWorkService([
        _StubWorkerRecord(
            task_project="samblog",
            task_number=99,
            worktree_path="/Users/x/wt",
            provider="claude",
        ),
    ]))
    payload = surfaces[0].to_dict()
    assert payload["task_id"] == 99
    assert payload["worktree_path"] == "/Users/x/wt"
    assert payload["surface_type"] == "worker"


# ---------------------------------------------------------------------------
# Sort key
# ---------------------------------------------------------------------------


def test_surface_sort_key_orders_by_type_then_project_then_task(
    tmp_path: Path,
) -> None:
    op = ChatSurface(
        session_name="operator",
        surface_type=SurfaceType.OPERATOR,
        persona="Polly",
        project=None,
        window=TmuxWindowState(tmux_session="x", window_name="y"),
    )
    arch = ChatSurface(
        session_name="architect_a",
        surface_type=SurfaceType.ARCHITECT,
        persona="A",
        project="a",
        window=TmuxWindowState(tmux_session="x", window_name="y"),
    )
    worker_1 = ChatSurface(
        session_name="task-a-1",
        surface_type=SurfaceType.WORKER,
        persona=None,
        project="a",
        task_id=1,
        window=TmuxWindowState(tmux_session="x", window_name="y"),
    )
    worker_2 = ChatSurface(
        session_name="task-a-2",
        surface_type=SurfaceType.WORKER,
        persona=None,
        project="a",
        task_id=2,
        window=TmuxWindowState(tmux_session="x", window_name="y"),
    )
    ordered = sorted([worker_2, worker_1, arch, op], key=_surface_sort_key)
    assert ordered == [op, arch, worker_1, worker_2]


def test_only_config_surfaces_helper_does_not_call_work_service(tmp_path: Path) -> None:
    config = _build_config(tmp_path, sessions={
        "operator": _session("operator", role="operator-pm"),
    })
    # No work_service arg at all — should still produce config surfaces.
    surfaces = enumerate_config_surfaces(config)
    assert len(surfaces) == 1
