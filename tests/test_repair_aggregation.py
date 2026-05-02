"""Issue #1045: ``pm repair --check`` aggregates by problem class.

The original implementation dumped one line per problem (89 lines on
the user's machine, with two patterns dominating). This test pins the
new doctor-styled aggregated layout and the trailing summary line, plus
the new ``--dry-run`` flag's "would not write" framing.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import typer
from typer.testing import CliRunner

from pollypm.cli_features.maintenance import (
    RepairProblem,
    _aggregate_problems,
    _format_aggregated_report,
    _format_summary_line,
    _problem_group,
    _split_doc_problem,
    register_maintenance_commands,
)


# Project keys lifted from the issue body so the fixture matches the
# user's actual ``Found 89 problems`` shape: 20 projects all missing
# ``.pollypm/checkpoints``, with 13 of them also showing 6 outdated
# reference docs each (78 outdated rows). 20 + 78 = 98 — the 9-row gap
# vs the user's reported 89 corresponds to projects that don't have a
# ``docs/reference/`` directory yet.
_REFERENCE_DOCS = [
    "commands.md",
    "operator-runbook.md",
    "plugins.md",
    "rules-and-magic.md",
    "sessions.md",
    "tasks.md",
]

_PROJECTS = [
    "agentic_dev_starter",
    "camptown",
    "expense_tracker",
    "github_dash",
    "habitica",
    "hello",
    "homelab",
    "infra",
    "lottery",
    "notes",
    "pets",
    "pollypm",
    "promptcraft",
    "recipes",
    "rss",
    "smoketest",
    "solo_chess",
    "stocks",
    "vault",
    "weather",
]

_PROJECTS_WITH_OUTDATED_DOCS = _PROJECTS[1:14]  # 13 projects (camptown..recipes)


def _build_89_problem_fixture() -> list[RepairProblem]:
    problems: list[RepairProblem] = []
    # 20 missing-checkpoint problems, one per project.
    for key in _PROJECTS:
        problems.append(
            RepairProblem(
                project=key,
                klass="missing",
                path=".pollypm/checkpoints",
                group=_problem_group("missing", ".pollypm/checkpoints"),
            )
        )
    # 13 projects x 6 docs = 78 outdated rows in the worst case. The
    # issue body reports an actual total of 89; some projects don't
    # have every reference doc present yet, so trim 9 rows but make
    # sure all 13 projects still surface at least one outdated row.
    skip_count = 0
    skip_target = 9
    for key in _PROJECTS_WITH_OUTDATED_DOCS:
        for doc_idx, doc in enumerate(_REFERENCE_DOCS):
            # Drop late docs (not the first one) for the first few
            # projects so each project keeps at least one outdated row.
            if skip_count < skip_target and doc_idx == len(_REFERENCE_DOCS) - 1:
                skip_count += 1
                continue
            problems.append(
                RepairProblem(
                    project=key,
                    klass="outdated",
                    path=f".pollypm/docs/reference/{doc}",
                    group=_problem_group(
                        "outdated", f".pollypm/docs/reference/{doc}"
                    ),
                )
            )
    return problems


def test_problem_group_collapses_reference_docs() -> None:
    assert (
        _problem_group("outdated", ".pollypm/docs/reference/sessions.md")
        == ".pollypm/docs/reference/"
    )
    assert (
        _problem_group("missing", ".pollypm/docs/reference/sessions.md")
        == ".pollypm/docs/reference/"
    )
    # Non-reference paths group as themselves.
    assert (
        _problem_group("missing", ".pollypm/checkpoints")
        == ".pollypm/checkpoints"
    )
    assert (
        _problem_group("missing", ".pollypm/docs/SYSTEM.md")
        == ".pollypm/docs/SYSTEM.md"
    )


def test_split_doc_problem_handles_known_prefixes() -> None:
    assert _split_doc_problem("missing .pollypm/docs/SYSTEM.md") == (
        "missing",
        ".pollypm/docs/SYSTEM.md",
    )
    assert _split_doc_problem(
        "outdated .pollypm/docs/reference/sessions.md"
    ) == ("outdated", ".pollypm/docs/reference/sessions.md")
    # Unknown prefix falls back to the ``config`` class so the row stays
    # surfaced rather than silently dropped.
    assert _split_doc_problem("weird thing happened") == (
        "config",
        "weird thing happened",
    )


def test_aggregate_problems_groups_by_class_and_group() -> None:
    problems = _build_89_problem_fixture()
    buckets = _aggregate_problems(problems)
    # Two classes total: missing checkpoints, outdated reference docs.
    assert len(buckets) == 2
    keys = list(buckets.keys())
    assert ("missing", ".pollypm/checkpoints") in keys
    assert ("outdated", ".pollypm/docs/reference/") in keys

    missing = buckets[("missing", ".pollypm/checkpoints")]
    assert missing.count == 20
    assert len(missing.project_set) == 20

    outdated = buckets[("outdated", ".pollypm/docs/reference/")]
    assert outdated.count == 78 - 9  # 69
    assert len(outdated.project_set) == 13
    # All six reference docs surface in the bucket's path set.
    assert len(outdated.paths) == 6


def test_format_aggregated_report_matches_issue_shape() -> None:
    problems = _build_89_problem_fixture()
    lines = _format_aggregated_report(problems)
    text = "\n".join(lines)

    # Header reflects total + project count, not just total.
    assert lines[0] == "Found 89 problems across 20 projects:"

    # Missing-checkpoints class shows count==n_projects (single number form).
    assert "[missing] .pollypm/checkpoints" in text
    assert "20 projects" in text

    # Outdated docs collapse the per-doc rows into a single labelled line.
    assert "[outdated] .pollypm/docs/reference/<6 docs each>" in text
    # Count != n_projects, so we render `count / n_projects` form.
    assert "69 / 13 projects" in text

    # Sample list with (+N more) hint for buckets exceeding the sample limit.
    assert "agentic_dev_starter, camptown, expense_tracker (+17 more)" in text
    assert "(+10 more)" in text  # 13 outdated-doc projects, sample 3, +10 more


def test_format_aggregated_report_singularises_lone_project() -> None:
    """A single-project bucket reads ``1 project`` not ``1 projects``."""
    problems = [
        RepairProblem(
            project="solo",
            klass="missing",
            path=".pollypm/checkpoints",
            group=".pollypm/checkpoints",
        )
    ]
    lines = _format_aggregated_report(problems)
    assert lines[0] == "Found 1 problem across 1 project:"
    assert "1 project" in "\n".join(lines)
    assert "1 projects" not in "\n".join(lines)


def test_format_summary_line_orientation() -> None:
    problems = _build_89_problem_fixture()
    summary = _format_summary_line(problems)
    assert summary == (
        "Summary: 89 problems across 20 projects "
        "(2 classes, all auto-fixable)"
    )


def test_format_summary_line_singularises_one_class() -> None:
    problems = [
        RepairProblem(
            project="solo",
            klass="missing",
            path=".pollypm/checkpoints",
            group=".pollypm/checkpoints",
        )
    ]
    summary = _format_summary_line(problems)
    assert summary == (
        "Summary: 1 problem across 1 project "
        "(1 class, all auto-fixable)"
    )


# ---- CLI surface tests --------------------------------------------------


def _build_app() -> typer.Typer:
    app = typer.Typer()
    register_maintenance_commands(app)
    return app


def _make_project(root: Path) -> Path:
    """Create a project root that triggers a ``missing checkpoints`` problem."""
    root.mkdir(parents=True, exist_ok=True)
    state = root / ".pollypm"
    # Pre-create everything except checkpoints so we land on a single,
    # predictable problem class.
    for sub in ["dossier", "logs", "artifacts", "worktrees", "rules", "magic"]:
        (state / sub).mkdir(parents=True, exist_ok=True)
    return root


def test_cli_check_renders_aggregated_block(tmp_path: Path) -> None:
    """``pm repair --check`` must use the aggregated layout, not a flat dump."""
    project_root = _make_project(tmp_path / "demo")

    cfg = tmp_path / "pollypm.toml"
    cfg.write_text("")

    from types import SimpleNamespace

    fake_project = SimpleNamespace(path=project_root)
    fake_config = SimpleNamespace(projects={"demo": fake_project})

    app = _build_app()
    runner = CliRunner()

    with patch(
        "pollypm.cli_features.maintenance.load_config",
        lambda _p: fake_config,
    ), patch(
        "pollypm.cli_features.maintenance.verify_docs",
        lambda _root: [],
    ), patch(
        "pollypm.cli_features.maintenance.GLOBAL_CONFIG_DIR",
        tmp_path / "global",
    ):
        result = runner.invoke(app, ["repair", "--check", "--config", str(cfg)])

    assert result.exit_code == 0, result.output
    # Aggregated header rather than the old flat header.
    assert "Found 1 problem across 1 project:" in result.output
    assert "[missing] .pollypm/checkpoints" in result.output
    # No flat-dump bullet rows.
    assert "  - [demo]" not in result.output
    # Next-action hints + dry-run pointer.
    assert "Run `pm repair` to fix all" in result.output
    assert "Run `pm repair --dry-run` to preview writes." in result.output
    # Trailing summary line.
    assert "Summary: 1 problem across 1 project" in result.output
    assert "(1 class, all auto-fixable)" in result.output
    # Confirm the directory was NOT created (check-only).
    assert not (project_root / ".pollypm" / "checkpoints").exists()


def test_cli_dry_run_previews_without_writing(tmp_path: Path) -> None:
    """``pm repair --dry-run`` reports problems and explicitly does not write."""
    project_root = _make_project(tmp_path / "demo")

    cfg = tmp_path / "pollypm.toml"
    cfg.write_text("")

    from types import SimpleNamespace

    fake_project = SimpleNamespace(path=project_root)
    fake_config = SimpleNamespace(projects={"demo": fake_project})

    app = _build_app()
    runner = CliRunner()

    with patch(
        "pollypm.cli_features.maintenance.load_config",
        lambda _p: fake_config,
    ), patch(
        "pollypm.cli_features.maintenance.verify_docs",
        lambda _root: [],
    ), patch(
        "pollypm.cli_features.maintenance.GLOBAL_CONFIG_DIR",
        tmp_path / "global",
    ):
        result = runner.invoke(
            app, ["repair", "--dry-run", "--config", str(cfg)]
        )

    assert result.exit_code == 0, result.output
    assert "Found 1 problem across 1 project:" in result.output
    assert "Dry-run; no writes performed." in result.output
    assert "Run `pm repair` to apply." in result.output
    assert "Summary: 1 problem across 1 project" in result.output
    # Critical: --dry-run must not create the directory.
    assert not (project_root / ".pollypm" / "checkpoints").exists()


def test_cli_check_and_dry_run_are_mutually_exclusive(tmp_path: Path) -> None:
    cfg = tmp_path / "pollypm.toml"
    cfg.write_text("")
    app = _build_app()
    runner = CliRunner()
    result = runner.invoke(
        app, ["repair", "--check", "--dry-run", "--config", str(cfg)]
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in (result.output + (result.stderr or ""))
