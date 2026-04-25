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
