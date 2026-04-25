"""Tests for ``pm inbox show msg:N`` (#760 adopter).

``pm inbox --json`` emits IDs in two forms: ``project/number`` for
work-service task rows and ``msg:N`` for unified-store notify/alert
rows. Before this fix, ``pm inbox show`` rejected the ``msg:N`` form,
so a user who copy-pasted the ID from ``--json`` hit a cryptic error.
These tests lock in the two-way parity: every ID from ``--json`` is a
valid ``show`` argument.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

from typer.testing import CliRunner

from pollypm.store import SQLAlchemyStore
from pollypm.work.inbox_cli import inbox_app


runner = CliRunner()


def _seed_message(db_path: Path, **overrides) -> int:
    """Write a single message and return its ID."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        return store.enqueue_message(
            type=overrides.get("type", "notify"),
            tier=overrides.get("tier", "immediate"),
            recipient=overrides.get("recipient", "user"),
            sender=overrides.get("sender", "polly"),
            subject=overrides.get("subject", "A notify from Polly"),
            body=overrides.get("body", "Plan ready for review.\nSee task demo/1."),
            scope=overrides.get("scope", "demo"),
        )
    finally:
        store.close()


def test_format_inbox_title_strips_contract_bracket_tag() -> None:
    """``pm inbox`` lists every immediate-tier notify subject as
    ``[Action] <subject>`` because the title contract tagged it at
    write time. The CLI list view already shows ``Type · notify``
    in its own column, so leading every row with ``[Action]`` just
    eats the column width set aside for the actual subject.

    Strip the title-contract tag for the display copy; preserve
    custom bracketed prefixes the caller chose.
    """
    from pollypm.work.inbox_cli import _format_inbox_title

    assert (
        _format_inbox_title("[Action] N-RC1 review (polly_remote/12)")
        == "N-RC1 review (polly_remote/12)"
    )
    assert _format_inbox_title("[FYI] CI green") == "CI green"
    assert _format_inbox_title("[Alert] stuck") == "stuck"
    assert _format_inbox_title("[Task] do the thing") == "do the thing"
    assert _format_inbox_title("[Audit] ledger") == "ledger"
    assert _format_inbox_title("[Note] generic") == "generic"
    # Custom bracketed content the caller wrote → leave it alone.
    assert (
        _format_inbox_title("[Done] milestone 02")
        == "[Done] milestone 02"
    )
    # No tag at all → leave it alone.
    assert _format_inbox_title("No tag here") == "No tag here"
    # Truncation still applies after stripping.
    long = "[Action] " + "x" * 60
    formatted = _format_inbox_title(long)
    assert formatted.endswith("…")
    assert len(formatted) <= 38


def test_inbox_show_accepts_msg_id_form(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    msg_id = _seed_message(db_path, subject="Plan ready for review")

    result = runner.invoke(inbox_app, ["show", f"msg:{msg_id}", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    # The key fields a user wants at a glance.
    assert f"msg:{msg_id}" in result.output
    assert "Plan ready for review" in result.output
    assert "sender:" in result.output
    assert "body:" in result.output


def test_inbox_show_msg_json_roundtrip(tmp_path: Path) -> None:
    """JSON output should be parseable and contain the same fields."""
    db_path = tmp_path / "state.db"
    msg_id = _seed_message(db_path, subject="JSON subject")

    result = runner.invoke(
        inbox_app, ["show", f"msg:{msg_id}", "--db", str(db_path), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = _json.loads(result.output)
    assert payload["id"] == msg_id
    # enqueue_message decorates subjects with a ``[Action]`` / ``[Info]``
    # prefix depending on type+tier; keep the assertion tolerant rather
    # than re-encoding that policy here.
    assert "JSON subject" in payload["subject"]


def test_inbox_show_msg_missing_id_exits_nonzero(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    # Create a DB with at least one row so the store isn't empty.
    _seed_message(db_path)

    result = runner.invoke(
        inbox_app, ["show", "msg:9999999", "--db", str(db_path)],
    )
    assert result.exit_code != 0
    assert "no message" in (result.output + (result.stderr or "")).lower()


def test_inbox_show_msg_invalid_form_exits_nonzero(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _seed_message(db_path)

    result = runner.invoke(
        inbox_app, ["show", "msg:not-a-number", "--db", str(db_path)],
    )
    assert result.exit_code != 0
    assert "invalid message id" in (result.output + (result.stderr or "")).lower()


def test_inbox_show_msg_surfaces_user_prompt_block(tmp_path: Path) -> None:
    """Architect / Polly notifications carrying a structured
    ``user_prompt`` payload must surface the plain-English summary,
    steps, and decision question through ``pm inbox show msg:N``,
    not just the raw body. Without this, an operator running the
    CLI to inspect an inbox row sees worker jargon and never the
    structured copy the architect authored."""
    db_path = tmp_path / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLAlchemyStore(f"sqlite:///{db_path}")
    try:
        msg_id = store.enqueue_message(
            type="notify",
            tier="immediate",
            recipient="user",
            sender="architect",
            subject="Plan ready for review",
            body=(
                "Internal worker notes — file paths, commit refs, "
                "stack traces — should not be the user-facing copy."
            ),
            scope="demo",
            payload={
                "actor": "architect",
                "user_prompt": {
                    "summary": "A full project plan is ready for your review.",
                    "steps": [
                        "Open the plan review surface.",
                        "Approve or send back with feedback.",
                    ],
                    "question": (
                        "Approve the plan or discuss changes with the PM?"
                    ),
                },
            },
        )
    finally:
        store.close()

    result = runner.invoke(
        inbox_app, ["show", f"msg:{msg_id}", "--db", str(db_path)],
    )
    assert result.exit_code == 0, result.output
    # The structured block leads with the plain-English summary,
    # numbered steps, and the decision label.
    assert "user_prompt:" in result.output
    assert "A full project plan is ready" in result.output
    assert "Open the plan review surface" in result.output
    assert "Approve or send back with feedback" in result.output
    assert "decision:" in result.output
    assert "Approve the plan or discuss changes" in result.output


def test_inbox_show_msg_omits_user_prompt_block_when_absent(
    tmp_path: Path,
) -> None:
    """Legacy notifications without a ``user_prompt`` payload must
    keep their existing body-only render — no empty
    ``user_prompt:`` heading on rows that have nothing to put under
    it."""
    db_path = tmp_path / "state.db"
    msg_id = _seed_message(db_path, subject="Legacy notification")

    result = runner.invoke(
        inbox_app, ["show", f"msg:{msg_id}", "--db", str(db_path)],
    )
    assert result.exit_code == 0, result.output
    assert "user_prompt:" not in result.output
