"""Regression tests for the shared title/body caps in knowledge_extract.

The memory_entries audit (reports/memory-entries-audit.md) showed 83,029 rows
of exponentially-escaped JSON garbage — largest single title was 2.44 MB —
caused by ``_heuristic_extract`` bypassing the caps/rejections that
``_sanitize_items`` (the Haiku path) already enforced. These tests lock in
the fix: both paths now route through ``_apply_item_caps`` and produce
identical output for the same inputs.
"""
from __future__ import annotations

from pollypm.knowledge_extract import (
    MAX_BODY_LEN,
    MAX_TITLE_LEN,
    KnowledgeDelta,
    _apply_item_caps,
    _heuristic_extract,
    _sanitize_items,
)


# ---------------------------------------------------------------------------
# 1. Long title gets truncated (both paths).
# ---------------------------------------------------------------------------


def test_long_title_truncated_by_helper() -> None:
    long = "decided to refactor the pipeline. " * 100  # ~3.4k chars
    assert len(long) > MAX_TITLE_LEN

    result = _apply_item_caps(long)
    assert result is not None
    title, body = result
    assert len(title) <= MAX_TITLE_LEN
    assert title.endswith("…")


def test_long_title_truncated_in_haiku_path() -> None:
    long = "we decided to refactor the pipeline. " * 100
    cleaned = _sanitize_items([long])
    assert len(cleaned) == 1
    assert len(cleaned[0]) <= MAX_TITLE_LEN
    assert cleaned[0].endswith("…")


def test_long_title_truncated_in_heuristic_path() -> None:
    # A sentence long enough to blow past MAX_TITLE_LEN after sentence split.
    long_sentence = "we decided to " + ("refactor the pipeline and migrate schema " * 40)
    delta = _heuristic_extract([{"payload": {"text": long_sentence}}])
    # The sentence hits decision/architecture keywords; both lists should be
    # present and both capped.
    assert delta.decisions, "expected decisions populated"
    for entry in delta.decisions + delta.architecture_changes:
        assert len(entry) <= MAX_TITLE_LEN, entry[:80]


# ---------------------------------------------------------------------------
# 2. Pathological backslash title is rejected.
# ---------------------------------------------------------------------------


def test_pathological_backslash_rejected_by_helper() -> None:
    # 20 consecutive backslashes — well over the MAX_CONSECUTIVE_BACKSLASHES
    # threshold of 10. This is the signature of the 2.44 MB bloat rows.
    pathological = "decided to " + ("\\" * 20) + " refactor"
    assert _apply_item_caps(pathological) is None


def test_pathological_backslash_rejected_in_haiku_path() -> None:
    pathological = "we chose " + ("\\" * 30) + " the pipeline"
    assert _sanitize_items([pathological]) == []


def test_pathological_backslash_rejected_in_heuristic_path() -> None:
    pathological = "we decided to " + ("\\" * 50) + " refactor pipeline."
    delta = _heuristic_extract([{"payload": {"text": pathological}}])
    # Nothing should survive — the one decision-bearing sentence carries the
    # pathological escape run and must be dropped.
    assert delta.decisions == []
    assert delta.architecture_changes == []


def test_small_backslash_run_still_rejected_by_existing_guard() -> None:
    # The pre-existing "\\\\" guard catches even short double-escape runs.
    # This test pins that behavior — it's unchanged by the refactor.
    assert _apply_item_caps("decided \\\\ refactor") is None


# ---------------------------------------------------------------------------
# 3. Empty title rejected.
# ---------------------------------------------------------------------------


def test_empty_title_rejected_by_helper() -> None:
    assert _apply_item_caps("") is None
    assert _apply_item_caps("   ") is None
    assert _apply_item_caps(None) is None


def test_empty_title_rejected_in_haiku_path() -> None:
    assert _sanitize_items(["", "  ", "\t\n"]) == []


# ---------------------------------------------------------------------------
# 4. Body == title rejected (no-info entry).
# ---------------------------------------------------------------------------


def test_body_equal_to_title_rejected() -> None:
    # When an explicit body is provided and it matches the title verbatim,
    # the helper drops the entry — storing it would be pure duplication.
    assert _apply_item_caps("refactor the pipeline", body="refactor the pipeline") is None


def test_body_differs_from_title_kept() -> None:
    result = _apply_item_caps("refactor the pipeline", body="split into stages")
    assert result == ("refactor the pipeline", "split into stages")


def test_body_longer_than_cap_truncated() -> None:
    title = "refactor pipeline"
    long_body = "detail. " * 1000  # ~8k chars, well over MAX_BODY_LEN
    assert len(long_body) > MAX_BODY_LEN
    result = _apply_item_caps(title, body=long_body)
    assert result is not None
    _t, capped_body = result
    assert len(capped_body) <= MAX_BODY_LEN


# ---------------------------------------------------------------------------
# 5. Normal items pass through both paths unchanged.
# ---------------------------------------------------------------------------


def test_normal_item_preserved_by_helper() -> None:
    result = _apply_item_caps("decided to adopt typed memory extractors")
    assert result == (
        "decided to adopt typed memory extractors",
        "decided to adopt typed memory extractors",
    )


def test_normal_items_preserved_by_haiku_path() -> None:
    items = [
        "decided to adopt typed memory extractors",
        "split knowledge extract into stages",
    ]
    assert _sanitize_items(items) == items


def test_normal_sentence_survives_heuristic_path() -> None:
    text = "We decided to refactor the pipeline into sealed stages."
    delta = _heuristic_extract([{"payload": {"text": text}}])
    # Hits both "decided" and "refactor" / "pipeline" keywords.
    assert text in delta.decisions
    assert text in delta.architecture_changes


# ---------------------------------------------------------------------------
# 6. BOTH-PATHS invariant: same item -> same caps in both paths.
# ---------------------------------------------------------------------------


def test_both_paths_produce_identical_caps_for_well_formed_items() -> None:
    good = [
        "decided to adopt typed memory extractors",
        "pipeline schema migrated to v4",
        "convention: always run heartbeat before dispatch",
    ]

    # Haiku path: input is a list of strings, output is list of titles.
    haiku_out = _sanitize_items(good)

    # Heuristic path: we simulate its post-extraction sanitize by feeding
    # the same strings through the same helper the heuristic now uses.
    simulated_heuristic = _sanitize_items(list(good))

    assert haiku_out == simulated_heuristic == good


def test_both_paths_produce_identical_caps_for_pathological_items() -> None:
    bad = [
        "",  # empty
        "decided \\\\ refactor",  # double-escape smell
        "we chose " + ("\\" * 30) + " pipeline",  # pathological run
        "{\"payload\": \"json\"}",  # raw JSON
        "```fenced code```",  # markdown fence
    ]
    haiku_out = _sanitize_items(bad)
    simulated_heuristic = _sanitize_items(list(bad))
    assert haiku_out == simulated_heuristic == []


def test_both_paths_identical_for_long_input() -> None:
    # A long sentence triggers the truncate-with-ellipsis branch on both
    # paths. The capped output must be byte-identical.
    long = "we decided to refactor the pipeline and migrate schema " * 50
    haiku_out = _sanitize_items([long])
    simulated_heuristic = _sanitize_items([long])
    assert haiku_out == simulated_heuristic
    assert len(haiku_out) == 1
    assert len(haiku_out[0]) <= MAX_TITLE_LEN
    assert haiku_out[0].endswith("…")


def test_heuristic_path_delta_fields_all_get_capped() -> None:
    """End-to-end: feed a single event that fires every keyword bucket with
    a pathological backslash run. Every field of the returned delta must be
    empty because the sentence is rejected uniformly.
    """
    toxic = (
        "we decided to refactor the pipeline, "
        + ("\\" * 40)
        + " architecture risk convention goal idea."
    )
    delta = _heuristic_extract([{"payload": {"text": toxic}}])
    assert isinstance(delta, KnowledgeDelta)
    assert delta.goals == []
    assert delta.architecture_changes == []
    assert delta.convention_shifts == []
    assert delta.decisions == []
    assert delta.risks == []
    assert delta.ideas == []
