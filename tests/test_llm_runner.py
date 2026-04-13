"""Tests for llm_runner JSON parsing and validation."""

from pathlib import Path

from pollypm.llm_runner import run_haiku, run_haiku_json


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


def test_run_haiku_returns_none_when_config_store_init_fails(monkeypatch) -> None:
    monkeypatch.setattr("pollypm.llm_runner.shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("pollypm.llm_runner.load_config", lambda path: (_ for _ in ()).throw(RuntimeError("boom")))

    assert run_haiku("test", config_path=Path("/tmp/missing.toml")) is None
