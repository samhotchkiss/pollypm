"""Lint gate: ratchet on raw ``/ ".pollypm" /`` joins in ``src/pollypm/`` (#1972).

Why this exists
---------------
The doubled-pollypm-path leak (#1810, #1950, #1966, #1972) keeps
returning because ``_resolve_pollypm_root()`` is opt-in: every new
feature that joins ``some_path / ".pollypm" / ...`` inline can
silently produce ``~/.pollypm/.pollypm/...`` when its caller happens
to be operating on the global config dir as a project root.

Per-PR audits (#1898, #1954) close known leak sites but don't
prevent the next one. The architectural fix (#1972) is to:

1. Funnel every ``.pollypm/<subdir>`` construction through a typed
   helper in :mod:`pollypm.projects` (``project_state_db_path``,
   ``project_audit_log_path``, etc.). Helpers chain through
   ``_resolve_pollypm_root`` so the doubled-path guard always fires.
2. Mechanically forbid NEW raw ``/ ".pollypm" /`` joins in
   ``src/pollypm/``. This test is the gate.

Ratchet design
--------------
Migrating all 155+ existing call sites in one PR is too big for v1
RC. So the gate ships as a **ratchet**: ``_BASELINE`` captures the
pre-fix count of raw joins per file. The test fails if:

* A file's count INCREASES (new leak site introduced), OR
* A file appears in the diff that isn't in the baseline (new file
  with a raw join), OR
* The baseline lists a file that no longer exists / has zero
  offenders (must shrink the baseline so future regressions land
  inside it).

The migration PRs (PR 2 + PR 3 under #1972) shrink the baseline
counts to zero. Once everything's zero, this test enforces "no
raw joins, ever."

Line-scoped allowlist (PR 4 under #1972)
----------------------------------------
Two files legitimately need ``.pollypm`` joins to define the
helpers + the canonical constant. **Previously** the gate excluded
those files wholesale, so any future operational join hidden inside
them slipped past the ratchet — Sam's review on PR #2003 / #2004
found exactly that footgun (3 ops joins outside the helper surface).

The gate now scopes the allowance per-line, not per-file:

* ``src/pollypm/config.py`` — the **only** allowed line is the
  module-level ``GLOBAL_CONFIG_DIR = Path.home() / ".pollypm"``
  constant at module top. Any other join in this file is treated
  exactly like a join in any other file: counted against the
  ratchet, fails the gate.
* ``src/pollypm/projects.py`` — joins are allowed **only** inside
  the typed-helper surface (``_resolve_pollypm_root`` and the
  ``project_*`` helpers funnelled through it). Joins anywhere
  else in the file (module-level, inside an unrelated function,
  inside a future helper that bypasses the resolver) fail the gate.

The allowlist is keyed off the Python AST: a join is allowed if
its line is inside a function whose name appears in
``_ALLOWED_FUNCTIONS[<filename>]``, OR if its line matches one of
``_ALLOWED_LINE_PATTERNS[<filename>]`` for module-level constants.

Per-line escape hatch
---------------------
A line carrying ``# noqa: pollypm-path-join`` is permitted and is
NOT counted against the baseline. Use this sparingly — comments
accompanying these joins should explain why the helper API doesn't
fit (e.g. constructing a fixture path in the package install tree,
where no project root exists).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src" / "pollypm"

# The literal pattern we ban. Matches ``/ ".pollypm" /`` and
# ``/ ".pollypm"`` followed by a closing context (string end, paren,
# comma, etc.). We use a deliberately tight pattern so we catch the
# load-bearing case (``X / ".pollypm" / "..."``) and the trailing
# case (``X / ".pollypm"`` building the pollypm dir itself) without
# matching the string ``".pollypm"`` in comments / docstrings.
_BAN_PATTERN = re.compile(r'/\s*"\.pollypm"')

# Per-line escape hatch.
_NOQA_PRAGMA = "noqa: pollypm-path-join"


# ---------------------------------------------------------------------------
# Line-scoped allowlist (PR 4 under #1972).
#
# Allowlist keys are filename (NOT full path) so the package can move on
# disk without breaking the gate. Match is restricted to files directly
# under ``src/pollypm/`` to avoid third-party plugin shadowing.
#
# ``_ALLOWED_FUNCTIONS[<filename>]`` — set of function names. A join is
# allowed if its line falls inside one of these functions' AST span.
#
# ``_ALLOWED_LINE_PATTERNS[<filename>]`` — list of compiled regexes. A
# join is allowed if its line matches at least one pattern AND lives at
# module scope (not inside any function).
# ---------------------------------------------------------------------------

# Functions in ``projects.py`` that legitimately construct ``.pollypm``
# segments. ``_resolve_pollypm_root`` is the single physical join; the
# typed helpers below it chain through the resolver (so the doubled-path
# guard fires) but textually their bodies contain
# ``.../ "<subdir>"`` joins built off the resolver's result — those are
# the legitimate constructors of the typed surface. We list each helper
# explicitly so a future helper added without going through the resolver
# (i.e. one that joins ``.pollypm`` raw) lands outside this set and
# fails the gate.
_PROJECTS_ALLOWED_FUNCTIONS = frozenset({
    "_resolve_pollypm_root",
    "project_pollypm_dir",
    "project_instruction_dir",
    "project_instruction_file",
    "project_dossier_dir",
    "project_logs_dir",
    "project_artifacts_dir",
    "project_checkpoints_dir",
    "project_worktrees_dir",
    "project_transcripts_dir",
    "project_state_db_path",
    "project_audit_log_path",
    "project_advisor_log_path",
    "project_plugins_dir",
    "project_gates_dir",
    "project_flows_dir",
    "project_rules_dir",
    "project_magic_dir",
    "project_config_dir",
    "project_docs_dir",
    "project_content_dir",
    "project_inbox_dir",
    "project_worker_markers_dir",
    "project_session_markers_dir",
    "project_system_prompts_dir",
    "project_project_guides_dir",
    "project_control_prompts_dir",
    "global_pollypm_dir",
})

_ALLOWED_FUNCTIONS: dict[str, frozenset[str]] = {
    "projects.py": _PROJECTS_ALLOWED_FUNCTIONS,
    # config.py: NO functions are allowlisted. Any join inside a
    # function in config.py is a regression. The only legitimate
    # join is the module-level GLOBAL_CONFIG_DIR constant, handled
    # below.
    "config.py": frozenset(),
}

# Module-level join patterns that are allowlisted in each file.
# These are constants (not function bodies). Any other module-level
# join in these files fails the gate.
_ALLOWED_LINE_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "config.py": (
        # GLOBAL_CONFIG_DIR is the canonical source-of-truth constant
        # the resolver + helpers reference. This line is THE definition.
        re.compile(r'^\s*GLOBAL_CONFIG_DIR\s*=\s*Path\.home\(\)\s*/\s*"\.pollypm"\s*$'),
    ),
    "projects.py": (
        # PROJECT_INSTRUCTIONS_TEMPLATE points at the repo-rooted
        # template under the package install tree; not a project root.
        re.compile(
            r'^\s*PROJECT_INSTRUCTIONS_TEMPLATE\s*=\s*Path\(__file__\)\.resolve\(\)'
            r'\.parents\[2\]\s*/\s*"\.pollypm"\s*/\s*"INSTRUCT\.md"\s*$'
        ),
    ),
}


# ---------------------------------------------------------------------------
# Baseline (#1972 PR 1 — design fix slice 1).
#
# Pre-migration count of raw joins per file in src/pollypm/. PRs 2
# and 3 under #1972 shrink these to zero. New files with raw joins
# (not in the baseline) and any increase against a baselined file
# both fail this test.
#
# When you migrate a file in #1972 PR 2 / PR 3:
#   - Reduce the entry's count by the number of joins you routed
#     through a typed helper.
#   - When the entry hits 0, remove it.
#
# When a legitimate raw join must remain (e.g. a constructor for a
# path inside the package install tree, where no project root
# exists), tag the line with ``# noqa: pollypm-path-join`` instead
# of leaving it in the baseline.
# ---------------------------------------------------------------------------
_BASELINE: dict[str, int] = {
    # Empty after #1972 PR 3 — all raw joins migrated to typed helpers.
    # From this point on, any new raw ``/ ".pollypm" /`` join in
    # ``src/pollypm/`` (other than the helper module and the
    # ``GLOBAL_CONFIG_DIR`` constant in config.py) fails the gate.
}


def _iter_src_files() -> list[Path]:
    return sorted(_SRC_ROOT.rglob("*.py"))


def _docstring_line_ranges(tree: ast.AST) -> set[int]:
    """Return all line numbers that fall inside a docstring.

    Docstrings appear textually in the source but contain example
    syntax that may match our ban pattern (see
    ``global_pollypm_dir.__doc__`` for the canonical case). We drop
    those lines from the offender list so callers don't have to
    sprinkle ``# noqa`` on documentation prose.
    """
    docstring_lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if not isinstance(first, ast.Expr):
            continue
        value = first.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        start = first.lineno
        end = getattr(first, "end_lineno", start) or start
        for lineno in range(start, end + 1):
            docstring_lines.add(lineno)
    return docstring_lines


def _function_spans(tree: ast.AST) -> list[tuple[str, int, int]]:
    """Return ``(qualname, start_lineno, end_lineno)`` for every function.

    Uses the simple function name (NOT a dotted qualname) because the
    allowlist keys on the function name itself. If two functions share
    a name (e.g. a nested helper), both spans are returned and either
    can match.
    """
    spans: list[tuple[str, int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        spans.append((node.name, node.lineno, end))
    return spans


def _enclosing_function(
    spans: list[tuple[str, int, int]], lineno: int
) -> str | None:
    """Return the innermost function name enclosing ``lineno``, or None."""
    candidates = [
        (name, start, end)
        for name, start, end in spans
        if start <= lineno <= end
    ]
    if not candidates:
        return None
    # Innermost = the span with the latest start that still contains lineno.
    candidates.sort(key=lambda t: t[1])
    return candidates[-1][0]


def _is_allowed_line(filename: str, line: str, enclosing_func: str | None) -> bool:
    """Return True if this offending line is in the per-file allowlist."""
    # Inside an allowlisted function?
    allowed_funcs = _ALLOWED_FUNCTIONS.get(filename, frozenset())
    if enclosing_func is not None and enclosing_func in allowed_funcs:
        return True
    # Module-level allowlisted pattern? Only valid OUTSIDE any function.
    if enclosing_func is None:
        for pattern in _ALLOWED_LINE_PATTERNS.get(filename, ()):
            if pattern.match(line):
                return True
    return False


def _find_offending_lines(path: Path) -> list[tuple[int, str]]:
    """Return ``(lineno, line)`` pairs that fail the gate for ``path``.

    Filters applied in order:

    1. Line must contain ``/ ".pollypm"`` (the ban pattern).
    2. Line not tagged ``# noqa: pollypm-path-join``.
    3. Line not a comment-only line (starts with ``#``).
    4. Line not inside a docstring (resolved via AST).
    5. Line not in the per-file allowlist (``_ALLOWED_FUNCTIONS`` /
       ``_ALLOWED_LINE_PATTERNS``).
    """
    out: list[tuple[int, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out

    # Parse the AST once so we can resolve docstrings + enclosing
    # function for each candidate line.
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        # If the file doesn't parse, we can't apply the AST-driven
        # filters. Fall back to the regex-only check so we don't
        # silently let the file off the hook.
        tree = None

    docstring_lines = _docstring_line_ranges(tree) if tree is not None else set()
    func_spans = _function_spans(tree) if tree is not None else []
    filename = path.name
    file_in_allowlist = (
        filename in _ALLOWED_FUNCTIONS or filename in _ALLOWED_LINE_PATTERNS
    ) and path.parent == _SRC_ROOT

    for lineno, line in enumerate(text.splitlines(), start=1):
        if not _BAN_PATTERN.search(line):
            continue
        if _NOQA_PRAGMA in line:
            continue
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if lineno in docstring_lines:
            continue
        if file_in_allowlist:
            enclosing = _enclosing_function(func_spans, lineno)
            if _is_allowed_line(filename, line, enclosing):
                continue
        out.append((lineno, line.rstrip()))
    return out


def _current_counts() -> dict[str, int]:
    """Return ``{relative_path: offender_count}`` for files with offenders.

    NOTE: with the line-scoped allowlist (PR 4), even files in
    ``_ALLOWED_FUNCTIONS`` / ``_ALLOWED_LINE_PATTERNS`` can appear
    here if they contain joins outside the allowlisted surface. The
    whole-file exclusion is gone.
    """
    counts: dict[str, int] = {}
    for path in _iter_src_files():
        n = len(_find_offending_lines(path))
        if n:
            rel = str(path.relative_to(_REPO_ROOT))
            counts[rel] = n
    return counts


def test_lint_gate_ratchet() -> None:
    """Fail on new raw joins or increases over the baseline.

    Three failure modes:

    1. A file's offender count INCREASED — new leak site introduced.
    2. A file appeared with offenders that isn't in the baseline — new
       leak site in a new file.
    3. A baseline entry no longer exists / has zero offenders — the
       baseline must shrink as we migrate. Stale entries hide future
       regressions inside the cleaned file.

    Migration PRs (#1972 PRs 2 + 3) shrink the baseline. Once
    everything is zero we have an absolute "no raw joins" gate.
    """
    current = _current_counts()
    failures: list[str] = []

    # Check every baseline entry vs current.
    for rel, baseline in sorted(_BASELINE.items()):
        actual = current.get(rel, 0)
        if actual > baseline:
            failures.append(
                f"{rel}: offender count went UP "
                f"({baseline} → {actual}). Migrate the new join "
                "through a typed helper in pollypm.projects, or "
                "add `# noqa: pollypm-path-join` with a comment."
            )
        elif actual < baseline:
            failures.append(
                f"{rel}: offender count went DOWN "
                f"({baseline} → {actual}). Update _BASELINE in "
                "tests/test_no_raw_pollypm_path_joins.py to match "
                "(remove the entry if it hit zero) so the ratchet "
                "stays tight."
            )

    # Check for new files with offenders not in the baseline.
    for rel, actual in sorted(current.items()):
        if rel not in _BASELINE:
            failures.append(
                f"{rel}: new file with {actual} raw ``.pollypm`` "
                "join(s). Route through a typed helper in "
                "pollypm.projects, or add `# noqa: pollypm-path-join`."
            )

    if failures:
        msg = "\n".join(["#1972 lint-gate ratchet failed:", "", *failures])
        raise AssertionError(msg)


def test_helper_module_only_internal_joins() -> None:
    """The helper module is the ONLY constructor of ``.pollypm``.

    Sanity-check the allowlist: the helper file must still contain
    the joins (otherwise the helpers can't construct paths), and
    ``config.py`` must still define ``GLOBAL_CONFIG_DIR``. If a
    future refactor moves the resolver elsewhere, this test catches
    the drift so the allowlist gets updated.
    """
    helpers = _SRC_ROOT / "projects.py"
    config = _SRC_ROOT / "config.py"

    helpers_text = helpers.read_text(encoding="utf-8")
    assert "_resolve_pollypm_root" in helpers_text, (
        f"{helpers}: expected to host _resolve_pollypm_root (helper module). "
        "If this moved, update _ALLOWED_FUNCTIONS in this test."
    )
    assert 'GLOBAL_CONFIG_DIR = Path.home() / ".pollypm"' in (
        config.read_text(encoding="utf-8")
    ), (
        f"{config}: expected to define GLOBAL_CONFIG_DIR. "
        "If this moved, update _ALLOWED_LINE_PATTERNS in this test."
    )


def test_noqa_pragma_recognised(tmp_path: Path) -> None:
    """A line tagged with the noqa pragma is permitted.

    Belt-and-suspenders for the pragma path: if we ever break the
    escape hatch, the gate becomes unusable. This test pins it.
    """
    fake = tmp_path / "fake.py"
    fake.write_text(
        'x = some_path / ".pollypm" / "foo"  # noqa: pollypm-path-join — test\n'
    )
    offenders = _find_offending_lines(fake)
    assert offenders == [], (
        f"Expected noqa pragma to suppress the gate but got: {offenders}"
    )


def test_inline_join_without_pragma_caught(tmp_path: Path) -> None:
    """A bare inline join is caught by the gate.

    Pin the positive case: if the regex stops matching, the gate
    silently lets new leak sites through.
    """
    fake = tmp_path / "fake.py"
    fake.write_text('x = some_path / ".pollypm" / "foo"\n')
    offenders = _find_offending_lines(fake)
    assert len(offenders) == 1, (
        f"Expected gate to catch raw join but got: {offenders}"
    )
    assert offenders[0][0] == 1


# ---------------------------------------------------------------------------
# Line-scoped allowlist regression tests (PR 4 under #1972).
#
# Sam's review on #2003 / #2004 found 3 operational joins hiding
# inside whole-file-excluded ``config.py`` / ``projects.py``. These
# tests pin that the new line-scoped allowlist actually catches the
# class of leak — without them, future refactors could re-introduce
# whole-file behaviour and the gate would silently shrink coverage.
# ---------------------------------------------------------------------------


def _write_pkg(tmp_path: Path, **files: str) -> Path:
    """Build a fake ``src/pollypm/`` under ``tmp_path`` for the gate to scan."""
    src_root = tmp_path / "src" / "pollypm"
    src_root.mkdir(parents=True)
    for name, content in files.items():
        (src_root / name).write_text(content)
    return src_root


def _scan_pkg(src_root: Path) -> dict[str, list[tuple[int, str]]]:
    """Mirror ``_find_offending_lines`` over a custom ``src/pollypm/`` root."""
    out: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(src_root.rglob("*.py")):
        offenders = _find_offending_lines_with_root(path, src_root)
        if offenders:
            out[path.name] = offenders
    return out


def _find_offending_lines_with_root(
    path: Path, src_root: Path
) -> list[tuple[int, str]]:
    """Variant of ``_find_offending_lines`` that uses ``src_root`` for the
    allowlist parent-dir check. Lets the regression tests drop a fake
    ``config.py`` / ``projects.py`` under a tmp path and have the gate
    treat them as if they lived under the real ``src/pollypm/``.
    """
    out: list[tuple[int, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        tree = None
    docstring_lines = _docstring_line_ranges(tree) if tree is not None else set()
    func_spans = _function_spans(tree) if tree is not None else []
    filename = path.name
    file_in_allowlist = (
        filename in _ALLOWED_FUNCTIONS or filename in _ALLOWED_LINE_PATTERNS
    ) and path.parent == src_root
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not _BAN_PATTERN.search(line):
            continue
        if _NOQA_PRAGMA in line:
            continue
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if lineno in docstring_lines:
            continue
        if file_in_allowlist:
            enclosing = _enclosing_function(func_spans, lineno)
            if _is_allowed_line(filename, line, enclosing):
                continue
        out.append((lineno, line.rstrip()))
    return out


def test_config_py_allows_only_global_config_dir_constant(tmp_path: Path) -> None:
    """``config.py`` allowlist is line-scoped to the GLOBAL_CONFIG_DIR constant.

    A module-level ``base_dir = root / ".pollypm"`` (the exact shape of
    the line the previous whole-file exclusion hid in PR #2003) must
    fail the gate.
    """
    src_root = _write_pkg(
        tmp_path,
        **{
            "config.py": (
                'from pathlib import Path\n'
                'GLOBAL_CONFIG_DIR = Path.home() / ".pollypm"\n'
                'def _build_example_config(root):\n'
                '    base_dir = root / ".pollypm"\n'  # operational join — must fail
                '    return base_dir\n'
            ),
        },
    )
    offenders = _scan_pkg(src_root)
    assert "config.py" in offenders, (
        "Expected the operational join inside _build_example_config "
        "to fail the gate, but config.py reported zero offenders."
    )
    # The GLOBAL_CONFIG_DIR line must NOT count as an offender.
    flagged_linenos = {ln for ln, _ in offenders["config.py"]}
    assert 4 in flagged_linenos, (
        f"Expected line 4 (`base_dir = root / \".pollypm\"`) to be "
        f"flagged. Got: {offenders['config.py']}"
    )
    assert 2 not in flagged_linenos, (
        f"Expected line 2 (GLOBAL_CONFIG_DIR) to be allowlisted. "
        f"Got: {offenders['config.py']}"
    )


def test_config_py_module_level_non_global_join_fails(tmp_path: Path) -> None:
    """A module-level join in config.py that isn't GLOBAL_CONFIG_DIR fails.

    Closes the "rename the constant, keep the join" footgun: the
    allowlist pattern matches the LHS too, so a future
    ``SOMETHING_ELSE = Path.home() / ".pollypm"`` is caught.
    """
    src_root = _write_pkg(
        tmp_path,
        **{
            "config.py": (
                'from pathlib import Path\n'
                'OTHER_DIR = Path.home() / ".pollypm"\n'
            ),
        },
    )
    offenders = _scan_pkg(src_root)
    assert "config.py" in offenders, (
        "Expected a module-level join with a non-GLOBAL_CONFIG_DIR LHS "
        "to fail the gate."
    )


def test_projects_py_join_outside_helper_function_fails(tmp_path: Path) -> None:
    """``projects.py`` allowlist is line-scoped to the typed-helper surface.

    A raw ``project.path / ".pollypm" / "state.db"`` inside a non-helper
    function (the shape of the line PR #2003 migrated at projects.py:796)
    must fail the gate.
    """
    src_root = _write_pkg(
        tmp_path,
        **{
            "projects.py": (
                'from pathlib import Path\n'
                'def _resolve_pollypm_root(p):\n'
                '    return p / ".pollypm"\n'  # allowed (inside helper)
                'def rename_project_slug(project):\n'
                '    work_db = project.path / ".pollypm" / "state.db"\n'  # must fail
                '    return work_db\n'
            ),
        },
    )
    offenders = _scan_pkg(src_root)
    assert "projects.py" in offenders, (
        "Expected the operational join inside rename_project_slug() "
        "to fail the gate, but projects.py reported zero offenders."
    )
    flagged_linenos = {ln for ln, _ in offenders["projects.py"]}
    assert 5 in flagged_linenos, (
        f"Expected line 5 (work_db = ...) to be flagged. "
        f"Got: {offenders['projects.py']}"
    )
    assert 3 not in flagged_linenos, (
        f"Expected line 3 (inside _resolve_pollypm_root) to be "
        f"allowlisted. Got: {offenders['projects.py']}"
    )


def test_projects_py_join_in_new_unlisted_helper_fails(tmp_path: Path) -> None:
    """A new ``project_*`` helper not in the allowlist still fails.

    A future helper added without going through the resolver (i.e. one
    that builds the ``.pollypm`` segment raw instead of chaining
    through ``_resolve_pollypm_root``) lands outside the allowlist
    and fails. This guards against drift where someone adds a helper
    that *looks* like a typed helper but skips the doubled-path
    resolver.
    """
    src_root = _write_pkg(
        tmp_path,
        **{
            "projects.py": (
                'from pathlib import Path\n'
                'def project_some_new_thing(p):\n'
                '    return p / ".pollypm" / "new-thing"\n'  # not in allowlist
            ),
        },
    )
    offenders = _scan_pkg(src_root)
    assert "projects.py" in offenders, (
        "Expected a join in a non-allowlisted project_* helper to "
        "fail the gate."
    )


def test_projects_py_docstring_join_does_not_trip_gate(tmp_path: Path) -> None:
    """Docstring prose containing ``.pollypm`` joins is not flagged.

    ``global_pollypm_dir`` and friends document their behaviour with
    code-shaped prose like ``Path.home() / ".pollypm"``. Those are
    docs, not joins, and must not count.
    """
    src_root = _write_pkg(
        tmp_path,
        **{
            "projects.py": (
                'from pathlib import Path\n'
                'def global_pollypm_dir():\n'
                '    """Wrapper.\n'
                '\n'
                '    Example: ``Path.home() / ".pollypm"`` is what this returns.\n'
                '    """\n'
                '    return None\n'
            ),
        },
    )
    offenders = _scan_pkg(src_root)
    assert "projects.py" not in offenders, (
        f"Docstring text should not trip the gate, but got: {offenders}"
    )
