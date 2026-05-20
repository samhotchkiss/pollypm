"""Regression: writer's ``forked_from`` digest must equal what the drift
detector computes as ``current_ref`` for the same role.

Background: in a packaged install (no git history for the bundled
source files), the writer's ``built_in_guide_fork_ref`` hashes the raw
built-in guide text while ``project_guide_drift_info`` hashed a
``.strip()``-ed copy of the same text. The two sha256 values
disagreed, so freshly-written guides were reported as drifted forever
and ``pm project init-guide ROLE --force`` could not clear ``pm
doctor``'s ``project-guide-drift`` alert.

The doctor's fallback ``_built_in_guide_body`` also disagreed with the
writer for the worker role (it read ``worker_prompt()`` while the
writer reads ``docs/worker-guide.md``). Both surfaces are pinned here
so they cannot diverge again.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from pollypm import doctor, project_guides


ROLES = ("architect", "reviewer", "worker")


def _force_sha_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``_git_sha_for_path`` return ``None`` so both writer and
    detector exercise the ``sha256:`` content-hash path. This is the
    realistic state in a packaged install where the bundled source
    files have no enclosing git repo."""

    monkeypatch.setattr(project_guides, "_git_sha_for_path", lambda _path: None)


@pytest.mark.parametrize("role", ROLES)
def test_writer_forked_from_matches_drift_current_ref(
    role: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After ``init_project_guide`` runs, ``project_guide_drift_info``
    must report ``drifted=False`` and emit the same ref the writer
    recorded."""

    _force_sha_fallback(monkeypatch)

    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    info = project_guides.init_project_guide(project_dir, role)
    drift = project_guides.project_guide_drift_info(project_dir, role)

    assert drift is not None, f"drift info missing for role {role}"
    assert drift.forked_from == info.forked_from, (
        f"forked_from drifted between read paths for role {role}: "
        f"writer-stored {info.forked_from!r} != drift-side {drift.forked_from!r}"
    )
    assert drift.current_ref == info.forked_from, (
        f"writer and detector disagree on source digest for role {role}: "
        f"writer recorded forked_from={info.forked_from!r} but "
        f"detector computed current_ref={drift.current_ref!r}"
    )
    assert drift.drifted is False, (
        f"freshly-initialised {role} guide reported as drifted: "
        f"forked_from={drift.forked_from!r} current_ref={drift.current_ref!r}"
    )


@pytest.mark.parametrize("role", ROLES)
def test_doctor_body_matches_writer_source(
    role: str,
) -> None:
    """The doctor's ``_built_in_guide_body`` must return the same text
    the writer reads (the bespoke worker branch previously read
    ``worker_prompt()`` instead of ``docs/worker-guide.md``)."""

    # Clear the lru_cache so repeated parametrised calls don't leak.
    doctor._built_in_guide_body.cache_clear()  # type: ignore[attr-defined]
    doctor._project_guides_module.cache_clear()  # type: ignore[attr-defined]

    writer_body = project_guides.built_in_guide_text(role)
    doctor_body = doctor._built_in_guide_body(role)

    assert doctor_body == writer_body, (
        f"doctor._built_in_guide_body diverges from "
        f"project_guides.built_in_guide_text for role {role}"
    )


@pytest.mark.parametrize("role", ROLES)
def test_doctor_fallback_branch_does_not_raise(
    role: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit defence-in-depth: when ``project_guides`` is
    unavailable, the bespoke fallback branch must still return a body
    string. We don't assert byte-equality here (the bespoke branch is
    known to disagree for worker — that's why we delegate when
    project_guides is present)."""

    doctor._built_in_guide_body.cache_clear()  # type: ignore[attr-defined]
    doctor._project_guides_module.cache_clear()  # type: ignore[attr-defined]
    real_module = sys.modules.get("pollypm.project_guides")
    monkeypatch.setitem(sys.modules, "pollypm.project_guides", types.ModuleType("stub"))
    try:
        out = doctor._built_in_guide_body(role)
        assert isinstance(out, str) and out
    finally:
        if real_module is not None:
            sys.modules["pollypm.project_guides"] = real_module
        doctor._built_in_guide_body.cache_clear()  # type: ignore[attr-defined]
        doctor._project_guides_module.cache_clear()  # type: ignore[attr-defined]
