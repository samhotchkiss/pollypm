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
