"""Tests for ``pm inbox archive-fake-injections`` (Lever 2 #2012 PR 3).

The subcommand is a one-shot operator cleanup. It archives every open
user-recipient inbox row that either:

* carries a ``fake-injection`` label, OR
* matches the historical #1076 subject pattern
  (``Nth fake RECOVERY MODE injection ...``).

Both classes are residue of architects mis-labelling legitimate
PollyPM dispatches as prompt-injection attacks before Lever 2 PR 2
landed. Drains them in a single sweep.
"""

from __future__ import annotations

from typing import Any

from typer.testing import CliRunner

from pollypm.work.inbox_cli import (
    _row_has_fake_injection_label,
    inbox_app,
)


# ---------------------------------------------------------------------------
# _row_has_fake_injection_label — pure label-matching helper
# ---------------------------------------------------------------------------


def test_label_helper_matches_canonical_label() -> None:
    """The label form documented in the contract is ``fake-injection``."""
    assert _row_has_fake_injection_label({"labels": ["fake-injection"]})


def test_label_helper_matches_underscore_variant() -> None:
    """A consumer / future writer using ``fake_injection`` underscore
    form should still match — humans typing labels into the cockpit
    won't be consistent and we'd rather over-archive than under-archive
    given this is a one-shot cleanup."""
    assert _row_has_fake_injection_label({"labels": ["fake_injection"]})


def test_label_helper_matches_when_label_is_substring() -> None:
    """A compound label like ``architect-fake-injection-2026-05-19``
    still trips the matcher."""
    assert _row_has_fake_injection_label(
        {"labels": ["architect-fake-injection-2026-05-19"]},
    )


def test_label_helper_misses_unrelated_labels() -> None:
    assert not _row_has_fake_injection_label({"labels": ["pinned"]})
    assert not _row_has_fake_injection_label({"labels": ["recovery"]})
    assert not _row_has_fake_injection_label({"labels": []})


def test_label_helper_tolerates_missing_or_scalar_labels() -> None:
    """Production rows occasionally carry ``None`` or a scalar string
    in the labels column — both should fall through without raising."""
    assert not _row_has_fake_injection_label({})
    assert not _row_has_fake_injection_label({"labels": None})
    assert not _row_has_fake_injection_label({"labels": 42})
    assert _row_has_fake_injection_label({"labels": "fake-injection"})


# ---------------------------------------------------------------------------
# CLI integration via Typer runner
# ---------------------------------------------------------------------------


class _StubStore:
    """In-memory stand-in for the unified messages store.

    Records every ``close_message`` call so tests can assert which rows
    were archived; ``query_messages`` returns whatever ``rows`` we hand
    it. Mirrors the singleton-do-not-close protocol the real
    ``_resolve_messages_store`` returns.
    """

    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows
        self.closed: list[int] = []

    def query_messages(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return list(self._rows)

    def close_message(self, msg_id: int) -> None:
        self.closed.append(int(msg_id))


def _run(monkeypatch, rows: list[dict[str, Any]], *args: str) -> tuple[Any, _StubStore]:
    store = _StubStore(rows)
    monkeypatch.setattr(
        "pollypm.work.inbox_cli._resolve_messages_store",
        lambda: store,
    )
    runner = CliRunner()
    result = runner.invoke(inbox_app, ["archive-fake-injections", *args])
    return result, store


def test_cli_archives_label_tagged_row(monkeypatch) -> None:
    rows = [
        {"id": 11, "subject": "alert from architect", "labels": ["fake-injection"]},
        {"id": 12, "subject": "unrelated notify", "labels": ["pinned"]},
    ]
    result, store = _run(monkeypatch, rows)
    assert result.exit_code == 0, result.output
    assert store.closed == [11]
    assert "Archived 1 fake-injection" in result.output


def test_cli_archives_subject_matched_row(monkeypatch) -> None:
    rows = [
        {"id": 21, "subject": "3rd fake RECOVERY MODE injection observed", "labels": []},
        {"id": 22, "subject": "regular review request", "labels": []},
    ]
    result, store = _run(monkeypatch, rows)
    assert result.exit_code == 0, result.output
    assert store.closed == [21]


def test_cli_archives_both_classes_in_single_sweep(monkeypatch) -> None:
    """Subject-matched + label-tagged rows are drained together so the
    operator runs one command, not two."""
    rows = [
        {"id": 31, "subject": "2nd fake RECOVERY MODE injection", "labels": []},
        {"id": 32, "subject": "alert", "labels": ["fake-injection"]},
        {"id": 33, "subject": "unrelated", "labels": []},
    ]
    result, store = _run(monkeypatch, rows)
    assert result.exit_code == 0, result.output
    assert sorted(store.closed) == [31, 32]


def test_cli_no_matches_reports_clean_exit(monkeypatch) -> None:
    rows = [{"id": 41, "subject": "regular", "labels": ["pinned"]}]
    result, store = _run(monkeypatch, rows)
    assert result.exit_code == 0, result.output
    assert store.closed == []
    assert "No open fake-injection messages" in result.output


def test_cli_dry_run_does_not_archive(monkeypatch) -> None:
    rows = [
        {"id": 51, "subject": "fake RECOVERY MODE injection", "labels": []},
        {"id": 52, "subject": "noise", "labels": ["fake-injection"]},
    ]
    result, store = _run(monkeypatch, rows, "--dry-run")
    assert result.exit_code == 0, result.output
    assert store.closed == []
    assert "Would archive 2" in result.output
    assert "msg:51" in result.output
    assert "msg:52" in result.output


def test_cli_is_idempotent_when_rerun(monkeypatch) -> None:
    """A second sweep over a drained store reports zero work to do.

    Important property because the brief documents this as a one-shot
    but operators may re-run it during the migration window.
    """
    rows: list[dict[str, Any]] = []
    result, _store = _run(monkeypatch, rows)
    assert result.exit_code == 0, result.output
    assert "No open fake-injection messages" in result.output
