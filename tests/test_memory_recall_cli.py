"""CLI smoke tests for the Slice D pg-recall + backfill commands.

Goes through the Typer CliRunner with ``pollypm.storage.memory_recall.recall``
patched to a stub. Real recall against pg is covered in
``tests/test_pg_memory_recall.py``; this module only verifies
argument parsing, JSON output shape, and error handling.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm.memory_cli import memory_app


runner = CliRunner()


# --------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------- #


@pytest.fixture()
def config_path(tmp_path: Path) -> Path:
    """Minimal pollypm.toml that loads cleanly."""
    cfg = tmp_path / "pollypm.toml"
    cfg.write_text(
        """
[project]
name = "test"
provider = "claude"
root_dir = "."
kind = "folder"

[pollypm]
home = ".pollypm"

[accounts.default]
provider = "claude"
home = ".claude"
""".strip()
    )
    # Match the resolve_config_path heuristics — cwd-or-explicit path.
    return cfg


# --------------------------------------------------------------------- #
# pg-recall command
# --------------------------------------------------------------------- #


def test_pg_recall_invokes_recall_and_prints_table(monkeypatch, config_path):
    from pollypm.storage.memory_recall import RecallHit

    captured: dict[str, object] = {}

    def fake_recall(query, *, limit, source_tables, config, **_kw):
        captured["query"] = query
        captured["limit"] = limit
        captured["source_tables"] = source_tables
        return [
            RecallHit(
                source_table="memory_entries",
                source_id="42",
                score=0.875,
                text_preview="hello world",
                metadata={
                    "title": "thing",
                    "components": {
                        "semantic": 0.9,
                        "fts": 0.5,
                        "importance": 0.6,
                        "recency": 0.95,
                    },
                },
            ),
        ]

    monkeypatch.setattr(
        "pollypm.memory_recall_cli.run_recall", fake_recall,
    )

    result = runner.invoke(
        memory_app,
        [
            "pg-recall",
            "test query",
            "--limit",
            "5",
            "--source",
            "memory_entries",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["query"] == "test query"
    assert captured["limit"] == 5
    assert captured["source_tables"] == ["memory_entries"]
    # Table layout shows id + preview.
    assert "memory_entries" in result.output
    assert "42" in result.output
    assert "hello world" in result.output


def test_pg_recall_json_output(monkeypatch, config_path):
    from pollypm.storage.memory_recall import RecallHit

    def fake_recall(query, **_kw):
        return [
            RecallHit(
                source_table="messages",
                source_id="7",
                score=0.42,
                text_preview="subject body",
                metadata={"subject": "subject", "components": {}},
            ),
        ]

    monkeypatch.setattr(
        "pollypm.memory_recall_cli.run_recall", fake_recall,
    )

    result = runner.invoke(
        memory_app,
        [
            "pg-recall",
            "anything",
            "--json",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert payload[0]["source_table"] == "messages"
    assert payload[0]["source_id"] == "7"
    assert payload[0]["score"] == pytest.approx(0.42, rel=1e-3)


def test_pg_recall_alias_work_context(monkeypatch, config_path):
    captured: dict[str, object] = {}

    def fake_recall(query, *, source_tables, **_kw):
        captured["source_tables"] = source_tables
        return []

    monkeypatch.setattr(
        "pollypm.memory_recall_cli.run_recall", fake_recall,
    )

    result = runner.invoke(
        memory_app,
        [
            "pg-recall",
            "x",
            "--source",
            "work_context",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0, result.output
    # The alias must expand to the canonical table name.
    assert captured["source_tables"] == ["work_context_entries"]


def test_pg_recall_bad_source_rejects(monkeypatch, config_path):
    result = runner.invoke(
        memory_app,
        [
            "pg-recall",
            "x",
            "--source",
            "nope",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code != 0
    assert "unknown" in result.output.lower() or "invalid" in result.output.lower()


def test_pg_recall_empty_results_says_no_results(monkeypatch, config_path):
    monkeypatch.setattr(
        "pollypm.memory_recall_cli.run_recall", lambda *a, **kw: [],
    )
    result = runner.invoke(
        memory_app,
        [
            "pg-recall",
            "anything",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "No results" in result.output


def test_pg_recall_recall_failure_propagates(monkeypatch, config_path):
    def boom(*_a, **_kw):
        raise RuntimeError("simulated pg outage")

    monkeypatch.setattr("pollypm.memory_recall_cli.run_recall", boom)
    result = runner.invoke(
        memory_app,
        [
            "pg-recall",
            "x",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code != 0
    assert "simulated pg outage" in result.output


def test_pg_recall_limit_zero_rejected(monkeypatch, config_path):
    monkeypatch.setattr(
        "pollypm.memory_recall_cli.run_recall", lambda *a, **kw: [],
    )
    result = runner.invoke(
        memory_app,
        [
            "pg-recall",
            "x",
            "--limit",
            "0",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code != 0


# --------------------------------------------------------------------- #
# backfill-embeddings command
# --------------------------------------------------------------------- #


def test_backfill_embeddings_dispatches_per_source(monkeypatch, config_path):
    """The backfill CLI must walk each requested source table."""
    calls: list[tuple[str, int]] = []

    class _StubWriter:
        metrics_batches_processed = 0
        metrics_rows_embedded = 0
        metrics_rows_skipped = 0
        metrics_batch_failures = 0

        def __init__(self, *a, **kw):
            pass

        def enqueue(self, *_a, **_kw):
            pass

        def drain_once(self, *, max_batches=1):
            return 0

    def stub_enqueue(_pool, table, _writer, limit):
        calls.append((table, limit))
        return 3, 1  # scanned=3, queued=1

    class _Pool:
        def connection(self):  # pragma: no cover
            raise AssertionError("pool not expected to be opened")

    monkeypatch.setattr(
        "pollypm.memory_recall_cli._enqueue_missing", stub_enqueue,
    )
    monkeypatch.setattr(
        "pollypm.storage.embedding_writer.EmbeddingWriter", _StubWriter,
    )
    monkeypatch.setattr(
        "pollypm.storage.pg_pool.get_rw_pool", lambda _cfg: _Pool(),
    )
    monkeypatch.setattr(
        "pollypm.storage.embedder.resolve_embedder",
        lambda _m, _k: object(),
    )

    result = runner.invoke(
        memory_app,
        [
            "backfill-embeddings",
            "--source",
            "memory_entries,messages",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0, result.output
    tables = {c[0] for c in calls}
    assert tables == {"memory_entries", "messages"}


def test_backfill_embeddings_json_summary(monkeypatch, config_path):
    class _StubWriter:
        metrics_batches_processed = 0
        metrics_rows_embedded = 0
        metrics_rows_skipped = 0
        metrics_batch_failures = 0

        def __init__(self, *a, **kw):
            pass

        def enqueue(self, *_a, **_kw):
            pass

        def drain_once(self, *, max_batches=1):
            return 1

    class _Pool:
        def connection(self):  # pragma: no cover
            raise AssertionError("pool not expected to be opened")

    monkeypatch.setattr(
        "pollypm.memory_recall_cli._enqueue_missing",
        lambda *_a, **_kw: (5, 2),
    )
    monkeypatch.setattr(
        "pollypm.storage.embedding_writer.EmbeddingWriter", _StubWriter,
    )
    monkeypatch.setattr(
        "pollypm.storage.pg_pool.get_rw_pool", lambda _cfg: _Pool(),
    )
    monkeypatch.setattr(
        "pollypm.storage.embedder.resolve_embedder",
        lambda _m, _k: object(),
    )

    result = runner.invoke(
        memory_app,
        [
            "backfill-embeddings",
            "--source",
            "all",
            "--json",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload.keys()) == {
        "memory_entries", "messages", "work_context_entries",
    }
    assert payload["memory_entries"]["scanned"] == 5
    assert payload["memory_entries"]["queued"] == 2


def test_backfill_bad_source_rejected(monkeypatch, config_path):
    result = runner.invoke(
        memory_app,
        [
            "backfill-embeddings",
            "--source",
            "not_a_table",
            "--config",
            str(config_path),
        ],
    )
    assert result.exit_code != 0
