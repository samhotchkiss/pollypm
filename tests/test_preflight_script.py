"""Smoke tests for ``scripts/preflight.sh`` (issue #1752).

The preflight script is the first thing a new user runs — a syntax
break or accidental regression to the install hint table is a P0 for
the onboarding path. These tests don't try to *pass* the preflight
(that depends on the developer's machine); they just confirm:

* the shell is syntactically valid (``bash -n``),
* every pg-related check (psql, pg_isready, pgvector) is named, and
* the hint table mentions the pgvector install URL for the
  "everything else" fallback.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = REPO_ROOT / "scripts" / "preflight.sh"


def test_preflight_script_exists() -> None:
    assert PREFLIGHT.exists(), f"missing preflight script: {PREFLIGHT}"


def test_preflight_shell_syntax_is_valid() -> None:
    """``bash -n preflight.sh`` must succeed (no syntax errors)."""
    result = subprocess.run(  # noqa: S603 — explicit args
        ["bash", "-n", str(PREFLIGHT)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, (
        f"preflight.sh has a syntax error:\n{result.stderr}"
    )


def test_preflight_covers_pg_checks() -> None:
    """The preflight script must check psql, pg_isready, and pgvector."""
    text = PREFLIGHT.read_text(encoding="utf-8")
    assert 'check "psql"' in text
    assert 'check "pg_isready"' in text
    # pgvector is checked via a control-file walk (not `command -v`),
    # so we just look for the brand name + extension control file.
    assert "pgvector" in text
    assert "vector.control" in text


def test_preflight_hint_table_includes_pgvector_url() -> None:
    """The hint table must point at the pgvector install notes for non-brew."""
    text = PREFLIGHT.read_text(encoding="utf-8")
    assert "github.com/pgvector/pgvector" in text


def test_preflight_recommends_bootstrap_pg_in_install_steps() -> None:
    """The ``Ready to install`` block must mention ``pm bootstrap-pg``."""
    text = PREFLIGHT.read_text(encoding="utf-8")
    assert "pm bootstrap-pg" in text
