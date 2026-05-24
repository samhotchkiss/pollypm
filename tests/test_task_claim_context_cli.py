from __future__ import annotations

from datetime import UTC, datetime

from typer.testing import CliRunner

from pollypm.claim_breadcrumbs import (
    CLAIM_ALREADY_CLAIMED_REASON,
    CLAIM_ATTEMPTED_BY_LOSER,
    CLAIM_WON_BY,
)
from pollypm.work import cli as work_cli
from pollypm.work.models import ContextEntry


class _ContextService:
    def __init__(self, entries: list[ContextEntry]) -> None:
        self._entries = entries

    def get_context(
        self,
        task_id: str,
        limit: int | None = None,
        entry_type: str | None = None,
    ) -> list[ContextEntry]:
        assert task_id == "demo/1"
        entries = [
            entry for entry in self._entries
            if entry_type is None or entry.entry_type == entry_type
        ]
        if limit is not None:
            return entries[:limit]
        return entries


def test_task_context_renders_claim_history_breadcrumbs(monkeypatch) -> None:
    entries = [
        ContextEntry(
            actor="loser",
            timestamp=datetime(2026, 5, 24, 12, 0, 1, tzinfo=UTC),
            text=(
                f"{CLAIM_ATTEMPTED_BY_LOSER} task_id=demo/1 actor=loser "
                f"session=loser reason={CLAIM_ALREADY_CLAIMED_REASON} "
                "assignee=worker winner_session=winner"
            ),
            entry_type=CLAIM_ATTEMPTED_BY_LOSER,
        ),
        ContextEntry(
            actor="winner",
            timestamp=datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC),
            text=(
                f"{CLAIM_WON_BY} task_id=demo/1 actor=winner "
                "session=winner assignee=worker"
            ),
            entry_type=CLAIM_WON_BY,
        ),
    ]
    monkeypatch.setattr(
        work_cli, "_svc", lambda **_kw: _ContextService(entries),
    )

    result = CliRunner().invoke(work_cli.task_app, ["context", "demo/1"])

    assert result.exit_code == 0, result.output
    assert "Claim history:" in result.output
    assert "Timestamp" in result.output
    assert "Actor" in result.output
    assert "Type" in result.output
    assert CLAIM_WON_BY in result.output
    assert CLAIM_ATTEMPTED_BY_LOSER in result.output
    assert result.output.index(CLAIM_WON_BY) < result.output.index(
        CLAIM_ATTEMPTED_BY_LOSER
    )
    assert "actor=winner" in result.output
    assert "actor=loser" in result.output
    assert f"reason={CLAIM_ALREADY_CLAIMED_REASON}" in result.output
    assert "winner_session=winner" in result.output
