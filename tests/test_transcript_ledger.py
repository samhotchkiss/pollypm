import gc
import json
import os
from pathlib import Path

from pollypm.config import write_config
from pollypm.models import AccountConfig, KnownProject, ProjectKind, ProjectSettings, PollyPMConfig, PollyPMSettings, ProviderKind
from pollypm.transcript_ledger import sync_token_ledger
from pollypm.storage.state import StateStore


def _config(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    config = PollyPMConfig(
        project=ProjectSettings(
            name="pollypm",
            root_dir=root,
            base_dir=root / ".pollypm",
            logs_dir=root / ".pollypm/logs",
            snapshots_dir=root / ".pollypm/snapshots",
            state_db=root / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_main"),
        accounts={
            "claude_main": AccountConfig(
                name="claude_main",
                provider=ProviderKind.CLAUDE,
                home=root / ".pollypm/homes/claude_main",
            ),
            "codex_main": AccountConfig(
                name="codex_main",
                provider=ProviderKind.CODEX,
                home=root / ".pollypm/homes/codex_main",
            ),
        },
        sessions={},
        projects={
            "demo": KnownProject(
                key="demo",
                path=root,
                name="Demo",
                kind=ProjectKind.GIT,
            )
        },
    )
    config_path = root / "pollypm.toml"
    write_config(config, config_path, force=True)
    return config_path


def test_sync_token_ledger_reads_claude_and_codex_jsonl(tmp_path: Path) -> None:
    config_path = _config(tmp_path)
    root = config_path.parent

    claude_home = root / ".pollypm/homes/claude_main"
    claude_file = claude_home / ".claude/projects/-Users-sam-dev-pollypm/session-a.jsonl"
    claude_file.parent.mkdir(parents=True, exist_ok=True)
    claude_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": "session-a",
                        "cwd": str(root),
                        "message": {
                            "role": "assistant",
                            "model": "claude-opus-4-6",
                            "usage": {
                                "input_tokens": 100,
                                "output_tokens": 20,
                                "cache_creation_input_tokens": 0,
                                "cache_read_input_tokens": 0,
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": "session-a",
                        "cwd": str(root),
                        "message": {
                            "role": "assistant",
                            "model": "claude-opus-4-6",
                            "usage": {
                                "input_tokens": 200,
                                "output_tokens": 50,
                                "cache_creation_input_tokens": 0,
                                "cache_read_input_tokens": 0,
                            },
                        },
                    }
                ),
            ]
        )
        + "\n"
    )

    codex_home = root / ".pollypm/homes/codex_main"
    codex_file = codex_home / ".codex/sessions/2026/04/08/rollout-test.jsonl"
    codex_file.parent.mkdir(parents=True, exist_ok=True)
    codex_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-04-08T10:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "session-b",
                            "cwd": str(root),
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-04-08T10:00:01Z",
                        "type": "turn_context",
                        "payload": {
                            "turn_id": "turn-b",
                            "cwd": str(root),
                            "model": "gpt-5.4",
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-04-08T10:00:02Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 300,
                                    "cached_input_tokens": 20,
                                    "output_tokens": 30,
                                    "reasoning_output_tokens": 5,
                                    "total_tokens": 355,
                                },
                                "last_token_usage": {
                                    "input_tokens": 300,
                                    "cached_input_tokens": 20,
                                    "output_tokens": 30,
                                    "reasoning_output_tokens": 5,
                                    "total_tokens": 355,
                                },
                            },
                            "rate_limits": {
                                "limit_id": "codex",
                                "primary": {"used_percent": 0.0, "window_minutes": 300, "resets_at": 0},
                                "secondary": {"used_percent": 0.0, "window_minutes": 10080, "resets_at": 0},
                                "credits": None,
                                "plan_type": "pro",
                            },
                        },
                    }
                ),
            ]
        )
        + "\n"
    )

    samples = sync_token_ledger(config_path)
    store = StateStore(root / ".pollypm/state.db")
    usage = store.recent_token_usage(limit=10)

    assert len(samples) == 2
    assert {sample.account_name for sample in samples} == {"claude_main", "codex_main"}
    assert {sample.project_key for sample in samples} == {"demo"}
    assert len(usage) == 2
    usage_by_account = {row.account_name: row.tokens_used for row in usage}
    assert usage_by_account["claude_main"] == 370
    assert usage_by_account["codex_main"] == 355


def _count_open_fds() -> int | None:
    """Return open-fd count or ``None`` if the platform doesn't expose
    a readable /proc-style fd directory (Windows, exotic sandboxes)."""
    for candidate in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(candidate))
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            continue
    return None


def test_sync_token_ledger_does_not_leak_file_descriptors(tmp_path: Path) -> None:
    """Regression for #1019.

    Before the fix, ``_scan_claude_transcript`` and ``_scan_codex_transcript``
    used a bare ``for line in path.open():`` iterator that left the file
    handle dangling until garbage collection. With many transcripts scanned
    every heartbeat tick this exhausted ``RLIMIT_NOFILE`` within hours and
    surfaced as ``Errno 24 Too many open files`` against the next caller of
    ``open()`` (the transcript-ingestion lock).
    """
    fd_listing = _count_open_fds()
    if fd_listing is None:
        import pytest

        pytest.skip("platform exposes neither /proc/self/fd nor /dev/fd")

    config_path = _config(tmp_path)
    root = config_path.parent

    # Lay down a handful of transcripts per provider so a leak compounds
    # quickly under a small tick count.
    claude_home = root / ".pollypm/homes/claude_main"
    codex_home = root / ".pollypm/homes/codex_main"
    claude_root = claude_home / ".claude/projects/-Users-sam-dev-pollypm"
    codex_root = codex_home / ".codex/sessions/2026/04/08"
    claude_root.mkdir(parents=True, exist_ok=True)
    codex_root.mkdir(parents=True, exist_ok=True)

    for index in range(8):
        (claude_root / f"session-{index}.jsonl").write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "sessionId": f"session-{index}",
                    "cwd": str(root),
                    "message": {
                        "role": "assistant",
                        "model": "claude-opus-4-6",
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 5,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                        },
                    },
                }
            )
            + "\n"
        )
        (codex_root / f"rollout-{index}.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "timestamp": "2026-04-08T10:00:00Z",
                            "type": "session_meta",
                            "payload": {"id": f"codex-{index}", "cwd": str(root)},
                        }
                    ),
                    json.dumps(
                        {
                            "timestamp": "2026-04-08T10:00:01Z",
                            "type": "turn_context",
                            "payload": {"cwd": str(root), "model": "gpt-5.4"},
                        }
                    ),
                    json.dumps(
                        {
                            "timestamp": "2026-04-08T10:00:02Z",
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "total_token_usage": {"total_tokens": 10},
                                    "last_token_usage": {"total_tokens": 10},
                                },
                            },
                        }
                    ),
                ]
            )
            + "\n"
        )

    # Prime caches / module imports so the baseline reflects steady state.
    sync_token_ledger(config_path)

    # Disable cyclic GC so a reference cycle in StateStore (or any other
    # opener) can't mask the leak. Pure refcounting still reaps deterministic
    # local references — what we want to catch is anything that needs
    # an explicit ``close()``.
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        baseline = _count_open_fds()
        assert baseline is not None

        # 20 heartbeat-equivalent ticks. Pre-fix the unclosed StateStore
        # leaked ~2 fds per call (the SQLite db handle plus its WAL/SHM
        # companions when WAL mode is active). Post-fix the steady-state
        # delta should stay well under a small bound. The 30-fd budget
        # absorbs prime-time variance from lazy WAL checkpointing.
        for _ in range(20):
            sync_token_ledger(config_path)

        final = _count_open_fds()
        assert final is not None
        delta = final - baseline
        assert delta < 30, (
            f"file-descriptor count grew unbounded across 20 ticks "
            f"(baseline={baseline}, final={final}, delta={delta}). "
            "Suspect a missing close() / context manager — see #1019."
        )
    finally:
        if gc_was_enabled:
            gc.enable()


def test_project_key_for_cwd_falls_back_to_slug_not_display_name(tmp_path: Path) -> None:
    """Issue #1042 — when the cwd doesn't resolve to a registered project
    (or is empty), ``_project_key_for_cwd`` used to fall back to
    ``config.project.name``, which is the *display name* (``"PollyPM"``).
    That capitalised slug landed in ``token_usage_hourly`` alongside the
    cwd-resolved lowercase key, splitting one project across two rows
    in ``pm costs``. The fallback now slugifies the display name so all
    writers agree on case.
    """
    from types import SimpleNamespace

    from pollypm.transcript_ledger import _project_key_for_cwd

    fake_config = SimpleNamespace(
        project=SimpleNamespace(
            name="PollyPM",  # display-name form, capitalized
            root_dir=tmp_path / "no-such-root",
        ),
        projects={},  # no registered projects to match cwd against
    )

    # Empty cwd hits the first fallback.
    assert _project_key_for_cwd(fake_config, None) == "pollypm"
    assert _project_key_for_cwd(fake_config, "") == "pollypm"

    # cwd that doesn't match any project + isn't under root_dir hits the
    # final fallback.
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    assert _project_key_for_cwd(fake_config, str(unrelated)) == "pollypm"
