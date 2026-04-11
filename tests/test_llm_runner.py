"""Tests for llm_runner JSON parsing and validation."""

from pollypm.llm_runner import run_haiku_json


def test_run_haiku_json_returns_none_for_list_response(monkeypatch) -> None:
    """LLM returning a JSON array instead of an object should return None."""
    monkeypatch.setattr("pollypm.llm_runner.run_haiku", lambda *a, **kw: '[1, 2, 3]')
    assert run_haiku_json("test") is None


def test_run_haiku_json_strips_markdown_fences(monkeypatch) -> None:
    monkeypatch.setattr("pollypm.llm_runner.run_haiku", lambda *a, **kw: '```json\n{"key": "val"}\n```')
    result = run_haiku_json("test")
    assert result == {"key": "val"}


def test_run_haiku_json_returns_none_for_invalid_json(monkeypatch) -> None:
    monkeypatch.setattr("pollypm.llm_runner.run_haiku", lambda *a, **kw: 'not json at all')
    assert run_haiku_json("test") is None


def test_run_haiku_json_returns_dict_for_valid_response(monkeypatch) -> None:
    monkeypatch.setattr("pollypm.llm_runner.run_haiku", lambda *a, **kw: '{"goals": ["ship v1"]}')
    result = run_haiku_json("test")
    assert result == {"goals": ["ship v1"]}


def test_run_haiku_json_returns_none_when_haiku_unavailable(monkeypatch) -> None:
    monkeypatch.setattr("pollypm.llm_runner.run_haiku", lambda *a, **kw: None)
    assert run_haiku_json("test") is None
