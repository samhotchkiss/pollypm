from pollypm.storage.fts_query import NO_MATCH_SENTINEL, normalize_fts_query


def test_normalize_fts_query_quotes_and_ors_tokens() -> None:
    assert normalize_fts_query("testing strategy") == '"testing" OR "strategy"'


def test_normalize_fts_query_strips_operators_and_preserves_identifiers() -> None:
    assert (
        normalize_fts_query('state_store: "foo" (bar) test* AND/OR')
        == '"state_store" OR "foo" OR "bar" OR "test" OR "and" OR "or"'
    )


def test_normalize_fts_query_drops_single_character_terms() -> None:
    assert normalize_fts_query("a b ci d e") == '"ci"'


def test_normalize_fts_query_returns_sentinel_for_empty_or_punctuation() -> None:
    assert normalize_fts_query("") == NO_MATCH_SENTINEL
    assert normalize_fts_query("()():::*+-") == NO_MATCH_SENTINEL
