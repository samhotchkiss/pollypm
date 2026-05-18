"""Operator dashboard surface (#1572).

Public exports for the dashboard categorization + view-model helpers
that back both the operator dashboard app and the rail glyph
vocabulary. See :mod:`pollypm.dashboard.categorization` for the
canonical ``ProjectState`` enum and ``categorize_project`` function;
those are the single source of truth shared between the rail and the
dashboard so the two surfaces never disagree on what a project is
doing.
"""

from __future__ import annotations

from pollypm.dashboard.categorization import (
    OperatorDashboardRow,
    OperatorDashboardView,
    ProjectState,
    build_operator_dashboard_view,
    categorize_project,
    glyph_for_project_state,
    what_working,
    why_waiting,
)

__all__ = [
    "OperatorDashboardRow",
    "OperatorDashboardView",
    "ProjectState",
    "build_operator_dashboard_view",
    "categorize_project",
    "glyph_for_project_state",
    "what_working",
    "why_waiting",
]
