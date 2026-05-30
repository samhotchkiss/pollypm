from __future__ import annotations

from types import SimpleNamespace

from pollypm.work import cli as work_cli


def test_infer_project_from_cwd_uses_registered_project(
    tmp_path, monkeypatch,
) -> None:
    project_root = tmp_path / "savethenovel"
    nested = project_root / "chapters"
    other_root = tmp_path / "media"
    nested.mkdir(parents=True)
    other_root.mkdir()
    config = SimpleNamespace(
        projects={
            "savethenovel": SimpleNamespace(path=project_root),
            "media": SimpleNamespace(path=other_root),
        }
    )

    monkeypatch.chdir(nested)

    assert work_cli._infer_project_from_cwd(config) == "savethenovel"


def test_task_next_defaults_to_current_registered_project(
    monkeypatch, capsys,
) -> None:
    calls: dict[str, str | None] = {}

    class _Svc:
        def next(self, *, agent: str | None, project: str | None):
            calls["agent"] = agent
            calls["next_project"] = project
            return None

    def fake_svc(*, project: str | None = None) -> _Svc:
        calls["svc_project"] = project
        return _Svc()

    monkeypatch.setattr(
        "pollypm.config.load_config",
        lambda: SimpleNamespace(projects={}),
    )
    monkeypatch.setattr(
        work_cli,
        "_infer_project_from_cwd",
        lambda _config: "savethenovel",
    )
    monkeypatch.setattr(work_cli, "_svc", fake_svc)

    work_cli.task_next(project=None, agent="architect", output_json=True)

    assert calls == {
        "agent": "architect",
        "next_project": "savethenovel",
        "svc_project": "savethenovel",
    }
    assert capsys.readouterr().out.strip() == "null"
