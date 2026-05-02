"""Cycle 71: pluralisation guard for the ``pm`` maintenance CLI surface.

Three CLI commands packed bare-plural counts into their output:

- ``pm tokens-sync`` echoed ``Synced N transcript token sample(s).``
- ``pm tokens`` listed per-project rows ``({days_active} active day(s))``
- ``pm repair`` printed ``Found N problem(s):`` and ``Applied K fix(es):``

Each fix is one ternary; testing the three full commands keeps the
guards anchored to the user-visible CLI surface rather than to the
helper functions.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import typer
from typer.testing import CliRunner

from pollypm.cli_features.maintenance import register_maintenance_commands


def _build_app() -> typer.Typer:
    app = typer.Typer()
    register_maintenance_commands(app)
    return app


def test_tokens_sync_pluralises_sample_count(tmp_path: Path) -> None:
    app = _build_app()
    runner = CliRunner()

    class _FakeSvc:
        def __init__(self, n: int) -> None:
            self._n = n

        def sync_token_ledger(self, *, account: str | None) -> int:
            return self._n

    cfg = tmp_path / "pollypm.toml"
    cfg.write_text("")

    with patch("pollypm.cli_features.maintenance._service", lambda _p: _FakeSvc(1)):
        out = runner.invoke(app, ["tokens-sync", "--config", str(cfg)])
    assert out.exit_code == 0, out.output
    assert "Synced 1 transcript token sample." in out.output
    assert "sample(s)" not in out.output

    with patch("pollypm.cli_features.maintenance._service", lambda _p: _FakeSvc(7)):
        out = runner.invoke(app, ["tokens-sync", "--config", str(cfg)])
    assert out.exit_code == 0, out.output
    assert "Synced 7 transcript token samples." in out.output
    assert "sample(s)" not in out.output


def test_semver_sort_key_picks_v110_over_v19() -> None:
    """Cycle 92: ``pm upgrade``'s git ls-remote fallback was sorting tags
    lexicographically — at the v1.10 line it would pick ``1.9.0`` as
    "latest" instead of ``1.10.0``. The new ``_semver_sort_key`` orders
    by ``packaging.version.Version`` so the picked latest is the actual
    newest release.
    """
    from pollypm.cli_features.maintenance import _semver_sort_key

    tags = [
        "1.0.0", "1.1.0", "1.2.0", "1.9.0", "1.10.0", "1.11.0", "2.0.0rc1",
    ]
    latest = sorted(tags, key=_semver_sort_key)[-1]
    assert latest == "2.0.0rc1"

    # Without the rc, 1.10.0 should beat 1.9.0.
    tags_no_rc = ["1.0.0", "1.1.0", "1.9.0", "1.10.0"]
    assert sorted(tags_no_rc, key=_semver_sort_key)[-1] == "1.10.0"

    # Lexicographic sort would have picked 1.9.0 — confirm we aren't
    # accidentally falling back to that.
    assert sorted(tags_no_rc)[-1] == "1.9.0"


def test_semver_sort_key_demotes_unparseable_tags() -> None:
    """Tags that don't parse as PEP 440 versions sort before any parseable
    version — so a stray ``nightly`` tag never masquerades as latest."""
    from pollypm.cli_features.maintenance import _semver_sort_key

    tags = ["nightly", "1.0.0", "wip", "1.1.0"]
    assert sorted(tags, key=_semver_sort_key)[-1] == "1.1.0"


def test_costs_pluralises_lookback_window(tmp_path: Path) -> None:
    """Cycle 105 — ``pm costs --days 1`` printed
    ``Token usage (last 1 days):`` because the window header was
    hard-pluralised. Agree the noun with the count.
    """
    from types import SimpleNamespace
    from unittest.mock import patch

    app = _build_app()
    runner = CliRunner()

    cfg = tmp_path / "pollypm.toml"
    cfg.write_text("")

    class _FakeStore:
        def execute(self, _sql, _params):
            return SimpleNamespace(
                fetchall=lambda: [("demo", 1000, 100, 1)]
            )

        def close(self) -> None:
            return None

    fake_config = SimpleNamespace(
        project=SimpleNamespace(state_db=tmp_path / "state.db"),
    )

    with patch(
        "pollypm.cli_features.maintenance.load_config",
        lambda _p: fake_config,
    ), patch(
        "pollypm.cli_features.maintenance.StateStore",
        lambda _db: _FakeStore(),
    ):
        out = runner.invoke(app, ["costs", "--days", "1", "--config", str(cfg)])
        assert out.exit_code == 0, out.output
        assert "Token usage (last 1 day):" in out.output
        assert "(last 1 days)" not in out.output

        out = runner.invoke(app, ["costs", "--days", "7", "--config", str(cfg)])
        assert out.exit_code == 0, out.output
        assert "Token usage (last 7 days):" in out.output


def test_costs_collapses_case_variant_project_keys(tmp_path: Path) -> None:
    """Issue #1042 — ``pm costs`` was grouping on the raw ``project_key``
    column, so a writer that emitted ``"PollyPM"`` (display name leaked
    via ``_project_key_for_cwd`` fallback) and a writer that emitted
    ``"pollypm"`` (cwd-resolved slug) showed up as two separate rows for
    one logical project. Group on ``LOWER(project_key)`` so a single
    row absorbs both cases.
    """
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from pollypm.storage.state import StateStore, TokenUsageHourlyRecord

    app = _build_app()
    runner = CliRunner()

    cfg = tmp_path / "pollypm.toml"
    cfg.write_text("")

    db_path = tmp_path / "state.db"
    store = StateStore(db_path)
    now_iso = datetime.now(UTC).isoformat()
    bucket = datetime.now(UTC).replace(minute=0, second=0, microsecond=0).isoformat()
    store.replace_token_usage_hourly(
        [
            TokenUsageHourlyRecord(
                hour_bucket=bucket,
                account_name="claude_primary",
                provider="claude",
                model_name="claude-opus-4-7",
                project_key="PollyPM",
                tokens_used=1_000_000,
                updated_at=now_iso,
            ),
            TokenUsageHourlyRecord(
                hour_bucket=bucket,
                account_name="claude_primary",
                provider="claude",
                model_name="claude-haiku-4-5",
                project_key="pollypm",
                tokens_used=2_500_000,
                updated_at=now_iso,
            ),
        ]
    )
    store.close()

    fake_config = SimpleNamespace(
        project=SimpleNamespace(state_db=db_path),
    )

    with patch(
        "pollypm.cli_features.maintenance.load_config",
        lambda _p: fake_config,
    ):
        out = runner.invoke(app, ["costs", "--days", "7", "--config", str(cfg)])
    assert out.exit_code == 0, out.output

    # One canonical lowercase row — neither the capitalized form nor a
    # second duplicate row should surface.
    assert "  pollypm: 3,500,000 tokens" in out.output, out.output
    assert "PollyPM" not in out.output, out.output
    # And the trailing total reflects the union, not just one variant.
    assert "Total: 3,500,000 tokens" in out.output, out.output

    # ``--project`` filter should canonicalize too: passing the
    # capitalized form must still match the lowercased aggregate row.
    with patch(
        "pollypm.cli_features.maintenance.load_config",
        lambda _p: fake_config,
    ):
        filtered = runner.invoke(
            app,
            ["costs", "--days", "7", "--project", "PollyPM", "--config", str(cfg)],
        )
    assert filtered.exit_code == 0, filtered.output
    assert "  pollypm: 3,500,000 tokens" in filtered.output, filtered.output
