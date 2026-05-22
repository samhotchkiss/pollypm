import threading
from pathlib import Path

import typer

from pollypm.config import load_config, write_config
from pollypm.models import (
    AccountConfig,
    ProjectKind,
    ProjectSettings,
    ModelAssignment,
    PollyPMConfig,
    PollyPMSettings,
    ProviderKind,
    RuntimeKind,
    SessionConfig,
    KnownProject,
)
from pollypm.storage.state import StateStore
from pollypm.plugins_builtin.core_agent_profiles.profiles import heartbeat_prompt
from pollypm.plugins_builtin.core_agent_profiles.profiles import polly_prompt as operator_prompt
from pollypm.plugins_builtin.core_agent_profiles.profiles import triage_prompt
from pollypm.plugins_builtin.core_agent_profiles.profiles import reviewer_prompt
from pollypm.workers import auto_select_worker_account, create_worker_session, suggest_worker_prompt
from pollypm.plugins_builtin.core_agent_profiles.profiles import worker_prompt


def _config(tmp_path: Path) -> tuple[PollyPMConfig, Path]:
    config = PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(
            controller_account="claude_controller",
            failover_enabled=True,
            failover_accounts=["codex_backup"],
        ),
        accounts={
            "claude_controller": AccountConfig(
                name="claude_controller",
                provider=ProviderKind.CLAUDE,
                email="claude@example.com",
                runtime=RuntimeKind.LOCAL,
                home=tmp_path / ".pollypm/homes/claude_controller",
            ),
            "codex_backup": AccountConfig(
                name="codex_backup",
                provider=ProviderKind.CODEX,
                email="codex@example.com",
                runtime=RuntimeKind.LOCAL,
                home=tmp_path / ".pollypm/homes/codex_backup",
            ),
            "claude_worker": AccountConfig(
                name="claude_worker",
                provider=ProviderKind.CLAUDE,
                email="worker@example.com",
                runtime=RuntimeKind.LOCAL,
                home=tmp_path / ".pollypm/homes/claude_worker",
            ),
        },
        sessions={
            "heartbeat": SessionConfig(
                name="heartbeat",
                role="heartbeat-supervisor",
                provider=ProviderKind.CLAUDE,
                account="claude_controller",
                cwd=tmp_path,
                project="pollypm",
                window_name="pm-heartbeat",
            ),
            "operator": SessionConfig(
                name="operator",
                role="operator-pm",
                provider=ProviderKind.CLAUDE,
                account="claude_controller",
                cwd=tmp_path,
                project="pollypm",
                window_name="pm-operator",
            ),
        },
        projects={
            "pollypm": KnownProject(
                key="pollypm",
                path=tmp_path,
                name="PollyPM",
                kind=ProjectKind.FOLDER,
            )
        },
    )
    for account in config.accounts.values():
        if account.home is not None:
            account.home.mkdir(parents=True, exist_ok=True)
            if account.provider is ProviderKind.CLAUDE:
                (account.home / ".claude").mkdir(parents=True, exist_ok=True)
                (account.home / ".claude" / ".credentials.json").write_text("{}")
            else:
                (account.home / ".codex").mkdir(parents=True, exist_ok=True)
                (account.home / ".codex" / "auth.json").write_text("{}")
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path)
    return config, config_path


def test_auto_select_worker_avoids_effective_live_controller(tmp_path: Path, monkeypatch) -> None:
    config, config_path = _config(tmp_path)
    store = StateStore(config.project.state_db)
    store.upsert_session_runtime(
        session_name="operator",
        status="healthy",
        effective_account="codex_backup",
        effective_provider=ProviderKind.CODEX.value,
    )

    monkeypatch.setattr("pollypm.workers.detect_logged_in", lambda account: True)

    selected = auto_select_worker_account(config_path)

    assert selected == "claude_worker"


def test_auto_select_worker_skips_runtime_unhealthy_account(tmp_path: Path, monkeypatch) -> None:
    config, config_path = _config(tmp_path)
    store = StateStore(config.project.state_db)
    store.upsert_account_runtime(
        account_name="codex_backup",
        provider=ProviderKind.CODEX.value,
        status="auth-broken",
        reason="failed auth",
    )

    monkeypatch.setattr("pollypm.workers.detect_logged_in", lambda account: True)

    selected = auto_select_worker_account(config_path)

    assert selected == "claude_worker"


def test_auto_select_worker_skips_runtime_unhealthy_account_underscore_form(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression for #1437: writers in heartbeats/api.py and supervisor.py
    persist ``status="auth_broken"`` (underscore). The account-runtime
    reader must accept the canonical underscore form so per-task workers
    skip the wedged account during selection. Before the fix, the reader
    only matched the hyphenated form and silently let the wedged account
    through, producing the savethenovel/15 wedge."""
    config, config_path = _config(tmp_path)
    store = StateStore(config.project.state_db)
    store.upsert_account_runtime(
        account_name="codex_backup",
        provider=ProviderKind.CODEX.value,
        status="auth_broken",
        reason="live session reported authentication failure",
    )

    monkeypatch.setattr("pollypm.workers.detect_logged_in", lambda account: True)

    selected = auto_select_worker_account(config_path)

    assert selected == "claude_worker"


def test_auto_select_worker_uses_control_plane_account_before_controller_last_resort(
    tmp_path: Path, monkeypatch
) -> None:
    config, config_path = _config(tmp_path)
    store = StateStore(config.project.state_db)
    store.upsert_session_runtime(
        session_name="operator",
        status="healthy",
        effective_account="codex_backup",
        effective_provider=ProviderKind.CODEX.value,
    )
    del config.accounts["claude_worker"]
    write_config(config, config_path, force=True)

    monkeypatch.setattr("pollypm.workers.detect_logged_in", lambda account: True)

    selected = auto_select_worker_account(config_path)

    assert selected == "codex_backup"


def test_auto_select_worker_closes_state_store_reads(tmp_path: Path, monkeypatch) -> None:
    _config_data, config_path = _config(tmp_path)
    created: list["FakeStore"] = []

    class FakeStore:
        def __init__(self, _db_path: Path) -> None:
            self.closed = False
            created.append(self)

        def __enter__(self) -> "FakeStore":
            return self

        def __exit__(self, *args) -> None:
            self.close()

        def close(self) -> None:
            self.closed = True

        def get_session_runtime(self, session_name: str):
            del session_name
            return None

        def get_account_runtime(self, account_name: str):
            del account_name
            return None

    monkeypatch.setattr("pollypm.workers.StateStore", FakeStore)
    monkeypatch.setattr("pollypm.workers.detect_logged_in", lambda account: True)

    selected = auto_select_worker_account(config_path)

    assert selected == "codex_backup"
    assert created
    assert all(store.closed for store in created)


def test_suggest_worker_prompt_returns_empty(tmp_path: Path) -> None:
    _config_data, config_path = _config(tmp_path)

    prompt = suggest_worker_prompt(config_path, project_key="pollypm")

    assert prompt == ""


def test_create_worker_session_routes_architect_model_assignment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config, config_path = _config(tmp_path)
    config.projects["pollypm"].role_assignments["architect"] = ModelAssignment(
        alias="sonnet-4.6"
    )
    write_config(config, config_path, force=True)
    monkeypatch.setattr("pollypm.workers.detect_logged_in", lambda account: True)
    monkeypatch.setattr(
        "pollypm.workers.ensure_worktree",
        lambda *args, **kwargs: type(
            "Worktree",
            (),
            {"path": str(tmp_path / "architect-worktree")},
        )(),
    )

    session = create_worker_session(
        config_path,
        project_key="pollypm",
        prompt=None,
        role="architect",
        agent_profile="architect",
    )

    assert session.provider is ProviderKind.CLAUDE
    assert session.args == [
        "--dangerously-skip-permissions",
        "--model",
        "claude-sonnet-4-6",
    ]


def test_create_worker_session_keeps_legacy_selection_when_fallback_provider_has_no_account(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config, config_path = _config(tmp_path)
    config.accounts = {
        "claude_controller": config.accounts["claude_controller"],
    }
    config.pollypm.failover_enabled = False
    config.pollypm.failover_accounts = []
    write_config(config, config_path, force=True)
    monkeypatch.setattr("pollypm.workers.detect_logged_in", lambda account: True)
    monkeypatch.setattr(
        "pollypm.workers.ensure_worktree",
        lambda *args, **kwargs: type(
            "Worktree",
            (),
            {"path": str(tmp_path / "worker-worktree")},
        )(),
    )

    session = create_worker_session(
        config_path,
        project_key="pollypm",
        prompt=None,
        role="worker",
    )

    assert session.provider is ProviderKind.CLAUDE
    assert session.args == ["--dangerously-skip-permissions"]


def test_concurrent_create_worker_session_for_same_role_no_orphans(
    tmp_path: Path, monkeypatch
) -> None:
    """Two concurrent ``pm worker-start`` calls for the same project/role
    must NOT both succeed with the same session_key (round-11 race; #2063).

    Reproduction modelled on Codex's: a ``threading.Barrier(2)`` is wired
    into a patched ``ensure_worktree`` so on the buggy code path (where
    the duplicate check happens BEFORE the lock) both threads reach the
    commit window simultaneously after passing the check, race the
    write, and the second writer clobbers the first — yielding two
    "successes" with the same session_key but only one session in the
    persisted config (the other worktree orphaned).

    With the fix, the uniqueness check happens INSIDE the lock, so the
    loser sees the winner's session and raises ``typer.BadParameter``
    before calling ``ensure_worktree``. Only the winner's thread ever
    enters the patched ``ensure_worktree``; the barrier therefore
    short-circuits via timeout, the winner proceeds, and the loser
    surfaces a clear duplicate error.

    Accept either outcome that preserves the invariant:
      (a) one success + one clear duplicate error; or
      (b) two distinct session keys (no overwrite).
    Reject: two successes with the same key, or any orphan.
    """
    _config_data, config_path = _config(tmp_path)
    monkeypatch.setattr("pollypm.workers.detect_logged_in", lambda account: True)

    barrier = threading.Barrier(2, timeout=5.0)
    worktree_counter = {"n": 0}
    worktree_lock = threading.Lock()

    def fake_ensure_worktree(*args, **kwargs):
        # Best-effort barrier: on the buggy code (check outside lock)
        # both threads reach here and race the commit. On the fixed
        # code only the winner enters this function; the loser is
        # already raising under the lock. A timeout-on-broken-barrier
        # lets the single thread keep going.
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        with worktree_lock:
            worktree_counter["n"] += 1
            idx = worktree_counter["n"]
        session = kwargs.get("session_name") or args[0] if args else "wt"
        path = tmp_path / f"worktree-{session}-{idx}"
        path.mkdir(parents=True, exist_ok=True)
        return type("Worktree", (), {"path": str(path)})()

    monkeypatch.setattr("pollypm.workers.ensure_worktree", fake_ensure_worktree)

    results: list[str] = []
    errors: list[BaseException] = []
    results_lock = threading.Lock()

    def run() -> None:
        try:
            session = create_worker_session(
                config_path,
                project_key="pollypm",
                prompt=None,
                role="worker",
            )
            with results_lock:
                results.append(session.name)
        except typer.BadParameter as exc:
            with results_lock:
                errors.append(exc)

    t1 = threading.Thread(target=run)
    t2 = threading.Thread(target=run)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)
    assert not t1.is_alive() and not t2.is_alive(), "worker-start threads deadlocked"

    # Together the two threads must produce exactly two outcomes.
    assert len(results) + len(errors) == 2, (
        f"Expected exactly 2 outcomes, got results={results} errors={errors}"
    )

    # No-orphan invariant: either two distinct keys, or one success + one
    # clear duplicate error. NEVER two successes with the same key.
    if len(results) == 2:
        assert results[0] != results[1], (
            f"Both threads returned the same session_key {results[0]!r} — "
            "round-11 race regression (orphan worktree)."
        )
    else:
        assert len(results) == 1 and len(errors) == 1, (
            f"Expected 1 success + 1 duplicate error, got results={results} "
            f"errors={[str(e) for e in errors]}"
        )

    # Every successful session_key MUST be present in the persisted
    # config (no orphan: a worker that the caller was told succeeded but
    # that the next ``load_config`` doesn't know about).
    final = load_config(config_path)
    final_sessions = set(final.sessions)
    for key in results:
        assert key in final_sessions, (
            f"session_key {key!r} returned successfully but missing from "
            f"persisted config sessions {sorted(final_sessions)} — orphan."
        )


def test_create_worker_session_revalidates_account_under_lock(
    tmp_path: Path, monkeypatch
) -> None:
    """Account removed between pre-lock validation and locked commit must
    fail safely without persisting an invalid session (#2063 round 12).

    Codex round-12 reproduction: the pre-lock path resolved the worker
    ``account`` from the pre-lock config snapshot and committed it
    inside the lock without re-validating against ``fresh``. An account
    edit landing between pre-lock and commit produced a persisted
    session whose ``account`` was missing from ``fresh.accounts`` —
    ``load_config`` then raised ``ValueError: Session ... references
    unknown account ...`` on the next read.

    Repro shape: patch ``suggest_worker_prompt`` (the legacy pre-lock
    helper) to remove the auto-selected worker account from disk before
    returning, then call ``create_worker_session``. Pre-fix the call
    succeeds and ``load_config`` raises on the next read. Post-fix the
    locked block re-validates the account against ``fresh`` and raises
    ``typer.BadParameter`` BEFORE writing, so the persisted config
    stays valid.
    """
    config, config_path = _config(tmp_path)
    monkeypatch.setattr("pollypm.workers.detect_logged_in", lambda account: True)
    monkeypatch.setattr(
        "pollypm.workers.ensure_worktree",
        lambda *args, **kwargs: type(
            "Worktree",
            (),
            {"path": str(tmp_path / "revalidate-worktree")},
        )(),
    )

    real_suggest = suggest_worker_prompt
    removed = {"done": False}

    def removing_suggest(config_path_arg, *, project_key):
        # Drop the explicitly-requested worker account between pre-lock
        # validation and the locked commit (Codex's exact reproduction
        # shape). The pre-lock path historically resolved the account
        # against the pre-lock snapshot and committed inside the lock;
        # once we drop the account from disk, the locked block's
        # ``fresh = load_config(...)`` MUST observe the absence and
        # refuse to persist an invalid session.
        if not removed["done"]:
            current = load_config(config_path_arg)
            current.accounts.pop("claude_worker", None)
            write_config(current, config_path_arg, force=True)
            removed["done"] = True
        return real_suggest(config_path_arg, project_key=project_key)

    monkeypatch.setattr("pollypm.workers.suggest_worker_prompt", removing_suggest)

    raised: BaseException | None = None
    try:
        create_worker_session(
            config_path,
            project_key="pollypm",
            prompt=None,
            account_name="claude_worker",
            role="worker",
        )
    except typer.BadParameter as exc:
        raised = exc

    assert raised is not None, (
        "create_worker_session should have raised typer.BadParameter when "
        "the requested account was removed between pre-lock validation "
        "and the locked commit — #2063 round-12 regression."
    )
    assert "claude_worker" in str(raised) or "Unknown account" in str(raised), (
        f"BadParameter should name the missing account, got: {raised!r}"
    )

    # Critical: load_config MUST succeed (no invalid session persisted).
    # Pre-fix this raised ``ValueError: Session 'worker_pollypm' references
    # unknown account 'claude_worker'``.
    final = load_config(config_path)
    for session in final.sessions.values():
        assert session.account in final.accounts, (
            f"persisted session {session.name!r} references unknown account "
            f"{session.account!r} — #2063 round-12 regression (the locked "
            f"commit wrote a SessionConfig whose account was missing from "
            f"the locked snapshot)."
        )


def test_worker_prompt_requires_core_identity() -> None:
    prompt = worker_prompt()

    assert "<identity>" in prompt
    assert "worker" in prompt.lower()
    assert "<principles>" in prompt
    assert "--output" in prompt
    assert " -o " not in prompt
    assert "commit" in prompt
    assert "file_change" in prompt
    assert "operations with side effects" in prompt
    assert "decision, blocker, or observation" in prompt
    assert "review_handoff" in prompt


def test_operator_prompt_requires_delegation_instructions() -> None:
    prompt = operator_prompt()

    assert "<identity>" in prompt
    assert "delegate" in prompt.lower()
    assert "pm" in prompt  # references pm commands
    assert "pm inbox" in prompt
    assert "pm mail" not in prompt
    assert "<principles>" in prompt
    assert "<authority>" in prompt
    assert "CAN, without asking" in prompt
    assert "MUST ESCALATE to Sam" in prompt
    assert "scope changes" in prompt.lower()
    assert "background probe" in prompt.lower()
    assert "non-blocking" in prompt.lower()
    assert "not a task for you" in prompt.lower()
    assert "<current_state_contract>" in prompt
    assert "<worker_management>" not in prompt
    assert "polly-operator-guide.md" in prompt
    assert "quote the exact name from the canonical artifact" in prompt
    # #936 added a <delegation> section instructing Polly to claim her
    # queued worker tasks. Cap held at <50 newlines so the prompt stays
    # tight while leaving room for that block.
    assert prompt.count("\n") < 50

def test_heartbeat_prompt_describes_recovery_protocol() -> None:
    prompt = heartbeat_prompt()

    assert "<protocol>" in prompt
    assert "idle" in prompt
    assert "stuck" in prompt
    assert "looping" in prompt
    assert "exited" in prompt
    assert "auth_broken" in prompt
    assert "resume ping" in prompt.lower()
    assert "pm task next" in prompt


def test_triage_prompt_points_at_inbox_cli() -> None:
    prompt = triage_prompt()

    assert "`pm inbox`" in prompt
    assert "`pm mail`" not in prompt


def test_reviewer_prompt_states_code_review_gate_semantics() -> None:
    prompt = reviewer_prompt()

    assert "`code_review`" in prompt
    assert "`done`" in prompt
    assert "`implement`" in prompt
    assert "stays parked at `code_review`" in prompt
