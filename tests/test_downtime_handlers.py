"""Tests for dt05 — the five exploration handlers.

Covers the happy path + one edge case per handler (spec §6 +
acceptance bar from the issue). Git integration is covered via a real
``git init`` in tmp_path so the branch setup is exercised end-to-end.

Security-scan additionally verifies the no-source-changes invariant via
:func:`validate_no_source_changes` — the apply path's enforcement point.
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from pollypm.plugins_builtin.downtime.handlers.audit_docs import (
    AuditDocsResult,
    run_audit_docs,
)
from pollypm.plugins_builtin.downtime.handlers.build_speculative import (
    BuildSpeculativeResult,
    run_build_speculative,
)
from pollypm.plugins_builtin.downtime.handlers.dispatch import (
    UnknownCategoryError,
    result_to_dict,
    run_exploration,
)
from pollypm.plugins_builtin.downtime.handlers.pick_candidate import Candidate
from pollypm.plugins_builtin.downtime.handlers.security_scan import (
    REPORT_DIR,
    SecurityScanResult,
    report_filename,
    run_security_scan,
    validate_no_source_changes,
)
from pollypm.plugins_builtin.downtime.handlers.spec_feature import (
    SPEC_DRAFT_DIR,
    SpecFeatureResult,
    render_spec_stub,
    run_spec_feature,
    slugify,
)
from pollypm.plugins_builtin.downtime.handlers.try_alt_approach import (
    COMPARISON_DIR,
    VALID_VERDICTS,
    TryAltApproachResult,
    run_try_alt_approach,
)


GIT_AVAILABLE = shutil.which("git") is not None


def _git_init(path: Path) -> None:
    """Initialise a git repo with one commit so branch ops have a base."""
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "downtime@test.local"], cwd=path, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Downtime Test"], cwd=path, check=True
    )
    # Initial commit so HEAD exists.
    (path / "README.md").write_text("downtime test\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True
    )


def _current_git_branch(path: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------


class TestSlugify:
    def test_basic(self) -> None:
        assert slugify("Hello World") == "hello-world"

    def test_strips_punctuation(self) -> None:
        assert slugify("Make it pop! (v2)") == "make-it-pop-v2"

    def test_empty_fallback(self) -> None:
        assert slugify("!!!") == "untitled"
        assert slugify("") == "untitled"


# ---------------------------------------------------------------------------
# spec_feature
# ---------------------------------------------------------------------------


class TestSpecFeature:
    def test_writes_draft_outside_git(self, tmp_path: Path) -> None:
        result = run_spec_feature(
            project_root=tmp_path,
            title="Great feature",
            description="make it shine",
        )
        assert isinstance(result, SpecFeatureResult)
        assert result.slug == "great-feature"
        draft = tmp_path / SPEC_DRAFT_DIR / "great-feature.md"
        assert draft.exists()
        content = draft.read_text()
        assert "Great feature" in content
        assert "make it shine" in content

    def test_render_stub_contains_seeded_content(self) -> None:
        stub = render_spec_stub(title="X", description="y")
        assert stub.startswith("# X")
        assert "draft / awaiting human review" in stub

    @pytest.mark.skipif(not GIT_AVAILABLE, reason="git not on PATH")
    def test_creates_branch_in_git_repo(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        result = run_spec_feature(
            project_root=tmp_path,
            title="Neat idea",
            description="shiny",
        )
        assert result.branch_name == "downtime/spec-neat-idea"
        # We restored the original branch — main.
        assert _current_git_branch(tmp_path) == "main"
        # But the downtime branch exists.
        branches = subprocess.run(
            ["git", "branch", "--list", "downtime/spec-neat-idea"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert "downtime/spec-neat-idea" in branches


# ---------------------------------------------------------------------------
# build_speculative
# ---------------------------------------------------------------------------


class TestBuildSpeculative:
    def test_scratch_written(self, tmp_path: Path) -> None:
        result = run_build_speculative(
            project_root=tmp_path,
            title="Proto X",
            description="build it",
        )
        assert isinstance(result, BuildSpeculativeResult)
        assert result.branch_name == "downtime/proto-x"
        scratch = tmp_path / ".pollypm" / "downtime-scratch" / "proto-x.md"
        assert scratch.exists()

    @pytest.mark.skipif(not GIT_AVAILABLE, reason="git not on PATH")
    def test_commit_sha_populated(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        result = run_build_speculative(
            project_root=tmp_path,
            title="Proto X",
            description="build it",
        )
        # Non-empty 40-char hex sha.
        assert len(result.commit_sha) >= 7


# ---------------------------------------------------------------------------
# audit_docs
# ---------------------------------------------------------------------------


class TestAuditDocs:
    def test_scaffold(self, tmp_path: Path) -> None:
        result = run_audit_docs(
            project_root=tmp_path,
            title="Audit session_manager",
            description="drift observed",
        )
        assert isinstance(result, AuditDocsResult)
        assert result.branch_name == "downtime/docs-audit-session-manager"
        assert "downtime/docs:" in result.pr_title
        assert "drift observed" in result.pr_body or "drift observed" in result.summary
        # PR not opened at dt05 — stays 0 / "".
        assert result.pr_number == 0
        assert result.pr_url == ""


# ---------------------------------------------------------------------------
# security_scan — report-only + no-source-changes invariant
# ---------------------------------------------------------------------------


class TestSecurityScan:
    def test_report_written_under_reports_dir(self, tmp_path: Path) -> None:
        result = run_security_scan(
            project_root=tmp_path,
            title="subprocess hardening",
            description="Review session_manager spawn path.",
            today=date(2026, 4, 16),
        )
        assert isinstance(result, SecurityScanResult)
        # Path shape: .pollypm/security-reports/2026-04-16-subprocess-hardening.md
        expected_rel = REPORT_DIR / "2026-04-16-subprocess-hardening.md"
        assert result.report_path == str(expected_rel)
        assert (tmp_path / expected_rel).exists()

    def test_no_branch_created(self, tmp_path: Path) -> None:
        """Per spec §6: security_scan produces NO branch."""
        if GIT_AVAILABLE:
            _git_init(tmp_path)
        run_security_scan(
            project_root=tmp_path,
            title="some scan",
            description="some scope",
            today=date(2026, 4, 16),
        )
        if GIT_AVAILABLE:
            # No downtime branches exist.
            branches = subprocess.run(
                ["git", "branch", "--list", "downtime/*"],
                cwd=tmp_path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            assert branches == ""

    def test_report_filename_shape(self) -> None:
        path = report_filename(title="Foo Bar", today=date(2026, 1, 2))
        assert path.as_posix().endswith("2026-01-02-foo-bar.md")

    def test_validate_no_source_changes_ok(self) -> None:
        ok, offenders = validate_no_source_changes(
            changed_paths=[
                ".pollypm/security-reports/2026-04-16-x.md",
                ".pollypm/security-reports/2026-04-17-y.md",
            ]
        )
        assert ok is True
        assert offenders == []

    def test_validate_no_source_changes_rejects_other_paths(self) -> None:
        ok, offenders = validate_no_source_changes(
            changed_paths=[
                ".pollypm/security-reports/ok.md",
                "src/pollypm/cli.py",
                "docs/cli.md",
            ]
        )
        assert ok is False
        assert "src/pollypm/cli.py" in offenders
        assert "docs/cli.md" in offenders

    def test_validate_rejects_paths_outside_dir_even_by_prefix_collision(self) -> None:
        ok, offenders = validate_no_source_changes(
            changed_paths=[".pollypm/security-reports-evil/sneaky.md"],
        )
        assert ok is False
        assert offenders == [".pollypm/security-reports-evil/sneaky.md"]


# ---------------------------------------------------------------------------
# try_alt_approach
# ---------------------------------------------------------------------------


class TestTryAltApproach:
    def test_comparison_written(self, tmp_path: Path) -> None:
        result = run_try_alt_approach(
            project_root=tmp_path,
            title="alt auth",
            description="try auth v2",
        )
        assert isinstance(result, TryAltApproachResult)
        comp = tmp_path / COMPARISON_DIR / "alt-auth.md"
        assert comp.exists()
        assert result.branch_name == "downtime/alt-auth"
        assert result.verdict in VALID_VERDICTS


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_routes_to_spec_feature(self, tmp_path: Path) -> None:
        cand = Candidate(
            title="Spec X",
            kind="spec_feature",
            description="",
        )
        result = run_exploration(cand, project_root=tmp_path)
        assert isinstance(result, SpecFeatureResult)

    def test_routes_to_security_scan(self, tmp_path: Path) -> None:
        cand = Candidate(title="scan", kind="security_scan", description="scope")
        result = run_exploration(cand, project_root=tmp_path)
        assert isinstance(result, SecurityScanResult)

    def test_unknown_kind_raises(self, tmp_path: Path) -> None:
        cand = Candidate(title="x", kind="bogus", description="")
        with pytest.raises(UnknownCategoryError):
            run_exploration(cand, project_root=tmp_path)

    def test_result_to_dict_round_trip(self, tmp_path: Path) -> None:
        cand = Candidate(title="Spec X", kind="spec_feature", description="")
        result = run_exploration(cand, project_root=tmp_path)
        payload = result_to_dict(result)
        assert payload["slug"] == "spec-x"
        assert "summary" in payload
