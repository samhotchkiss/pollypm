"""System prerequisite checks extracted from :mod:`pollypm.doctor`."""

from __future__ import annotations

import os
import sys

import pollypm.doctor as doctor


def check_python_version() -> doctor.CheckResult:
    want = doctor._read_pyproject_required_python()
    have = sys.version_info[:3]
    want_str = ".".join(str(x) for x in want)
    have_str = ".".join(str(x) for x in have)
    if have >= want:
        return doctor._ok(f"Python {have_str} (>= {want_str})", data={"version": have_str})
    return doctor._fail(
        f"Python {have_str} is below required {want_str}",
        why=(
            f"PollyPM pins requires-python to >= {want_str} in pyproject.toml. "
            "Older interpreters lack typing features the codebase depends on "
            "(StrEnum, PEP 695 generics, tomllib)."
        ),
        fix=(
            f"Install Python >= {want_str} and re-run with that interpreter —\n"
            "  macOS:  brew install python@3.13\n"
            "  Ubuntu: sudo apt install python3.13\n"
            "  Or:     uv python install 3.13 && uv sync --reinstall\n"
            "Recheck: pm doctor"
        ),
        data={"have": have_str, "want": want_str},
    )


def check_tmux() -> doctor.CheckResult:
    path = doctor._tool_path("tmux")
    if path is None:
        auto_fix = doctor._brew_auto_fix("tmux", description="Install tmux with Homebrew")
        if auto_fix is None:
            auto_fix = doctor._linux_pkg_manager_auto_fix(
                "tmux",
                description="Install tmux with the system package manager",
            )
        return doctor._fail(
            "tmux not found on PATH",
            why=(
                "PollyPM is a tmux-first orchestrator — every session, worker, "
                "and storage closet pane lives inside tmux. There is no fallback."
            ),
            fix=(
                "Install tmux —\n"
                "  macOS:  brew install tmux\n"
                "  Ubuntu: sudo apt install tmux\n"
                "  Build:  https://github.com/tmux/tmux/wiki/Installing\n"
                "Recheck: pm doctor"
            ),
            auto_fix=auto_fix,
        )
    rc, out = doctor._run_cmd(["tmux", "-V"])
    version = doctor._parse_version(out) if rc == 0 else None
    version_str = ".".join(str(x) for x in version) if version else "unknown"
    if version is None:
        return doctor._fail(
            f"tmux version not detectable ({out[:60]!r})",
            why=(
                "PollyPM needs tmux >= 3.3 for pane-pipe-mode, kill-pane -a, "
                "and window option inheritance. We could not parse `tmux -V` output."
            ),
            fix=(
                "Reinstall tmux and re-check —\n"
                "  macOS:  brew reinstall tmux\n"
                "  Ubuntu: sudo apt reinstall tmux\n"
                "Recheck: pm doctor"
            ),
            data={"raw": out},
        )
    if version < (3, 3, 0):
        return doctor._fail(
            f"tmux version too old ({version_str})",
            why=(
                "PollyPM uses tmux features introduced in 3.3 (pane-pipe-mode, "
                "kill-pane -a, window option inheritance). Older tmux silently "
                "loses pane output and fragments storage-closet layout."
            ),
            fix=(
                "Upgrade tmux —\n"
                "  macOS:  brew upgrade tmux\n"
                "  Ubuntu: sudo apt install --only-upgrade tmux\n"
                "  Build:  https://github.com/tmux/tmux/wiki/Installing\n"
                "Recheck: pm doctor"
            ),
            data={"version": version_str, "min": "3.3.0"},
        )
    return doctor._ok(f"tmux {version_str} (>= 3.3)", data={"version": version_str, "path": path})


def check_git() -> doctor.CheckResult:
    path = doctor._tool_path("git")
    if path is None:
        return doctor._fail(
            "git not found on PATH",
            why=(
                "PollyPM tracks worktrees, creates per-task branches, and "
                "delegates issue syncing to git. Without git the worker flow "
                "cannot run."
            ),
            fix=(
                "Install git —\n"
                "  macOS:  brew install git\n"
                "  Ubuntu: sudo apt install git\n"
                "Recheck: pm doctor"
            ),
        )
    rc, out = doctor._run_cmd(["git", "--version"])
    version = doctor._parse_version(out) if rc == 0 else None
    if version is None:
        return doctor._fail(
            f"git version not detectable ({out[:60]!r})",
            why="PollyPM requires git >= 2.40 for safe worktree management.",
            fix=(
                "Reinstall git —\n"
                "  macOS:  brew reinstall git\n"
                "  Ubuntu: sudo apt reinstall git\n"
                "Recheck: pm doctor"
            ),
        )
    if version < (2, 40, 0):
        version_str = ".".join(str(x) for x in version)
        return doctor._fail(
            f"git version too old ({version_str})",
            why=(
                "PollyPM needs git >= 2.40 for `git worktree add --orphan` and "
                "the per-task worktree lifecycle introduced with the work service."
            ),
            fix=(
                "Upgrade git —\n"
                "  macOS:  brew upgrade git\n"
                "  Ubuntu: sudo apt install --only-upgrade git\n"
                "Recheck: pm doctor"
            ),
            data={"version": version_str, "min": "2.40.0"},
        )
    return doctor._ok(
        f"git {'.'.join(str(x) for x in version)} (>= 2.40)",
        data={"version": ".".join(str(x) for x in version), "path": path},
    )


def check_gh_installed() -> doctor.CheckResult:
    path = doctor._tool_path("gh")
    if path is None:
        return doctor._fail(
            "gh CLI not found on PATH",
            why=(
                "PollyPM's issue backend and many worker flows shell out to `gh`. "
                "Without it, `pm issue list`, PR creation, and GitHub sync break."
            ),
            fix=(
                "Install the GitHub CLI —\n"
                "  macOS:  brew install gh\n"
                "  Ubuntu: https://github.com/cli/cli/blob/trunk/docs/install_linux.md\n"
                "Then:   gh auth login\n"
                "Recheck: pm doctor"
            ),
        )
    return doctor._ok(f"gh CLI installed", data={"path": path})


def check_gh_authenticated() -> doctor.CheckResult:
    if doctor._tool_path("gh") is None:
        return doctor._skip("gh auth skipped (gh not installed)")
    rc, out = doctor._run_cmd(["gh", "auth", "status", "--active"], timeout=3.0)
    if rc != 0 and "unknown flag" in out.lower():
        rc, out = doctor._run_cmd(["gh", "auth", "status"], timeout=3.0)
    if rc == 0:
        return doctor._ok("gh authenticated")
    return doctor._fail(
        "gh is not authenticated",
        why=(
            "PollyPM's GitHub flows (issue sync, PR creation, review pulls) "
            "assume `gh auth status --active` reports a logged-in account."
        ),
        fix=(
            "Authenticate gh —\n"
            "  gh auth login\n"
            "Recheck: pm doctor"
        ),
        data={"stderr": out[:200]},
    )


def check_uv() -> doctor.CheckResult:
    path = doctor._tool_path("uv")
    if path is None:
        return doctor._fail(
            "uv not found on PATH",
            why=(
                "PollyPM ships as an `uv`-managed editable install; `pm` and "
                "`pollypm` entry points are provisioned via `uv tool install`."
            ),
            fix=(
                "Install uv —\n"
                "  macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh\n"
                "  Or: brew install uv\n"
                "Recheck: pm doctor"
            ),
            auto_fix=doctor._uv_install_auto_fix(),
        )
    return doctor._ok("uv installed", data={"path": path})


def check_claude_cli() -> doctor.CheckResult:
    path = doctor._tool_path("claude")
    if path is None:
        return doctor._fail(
            "claude CLI not found on PATH",
            why=(
                "PollyPM launches Claude Code sessions for Claude provider accounts. "
                "Without the `claude` binary, onboarding and session bootstrap cannot start."
            ),
            fix=(
                "Install Claude Code —\n"
                "  npm i -g @anthropic-ai/claude-code\n"
                "Recheck: pm doctor"
            ),
            auto_fix=doctor._npm_global_auto_fix(
                "@anthropic-ai/claude-code",
                description="Install Claude Code globally",
            ),
        )
    return doctor._ok("claude CLI installed", data={"path": path})


def check_codex_cli() -> doctor.CheckResult:
    path = doctor._tool_path("codex")
    if path is None:
        return doctor._fail(
            "codex CLI not found on PATH",
            why=(
                "PollyPM launches Codex sessions for Codex provider accounts. "
                "Without the `codex` binary, onboarding and session bootstrap cannot start."
            ),
            fix=(
                "Install Codex —\n"
                "  npm i -g @openai/codex\n"
                "Recheck: pm doctor"
            ),
            auto_fix=doctor._npm_global_auto_fix(
                "@openai/codex",
                description="Install Codex globally",
            ),
        )
    return doctor._ok("codex CLI installed", data={"path": path})


def check_terminal_color_support() -> doctor.CheckResult:
    term = os.environ.get("TERM", "")
    colorterm = os.environ.get("COLORTERM", "")
    if term in {"", "dumb"}:
        return doctor._fail(
            f"terminal likely lacks color support (TERM={term!r})",
            why=(
                "PollyPM's cockpit and rail rely on 256-color / true-color ANSI "
                "escapes. A 'dumb' or missing TERM renders them as garbage."
            ),
            fix=(
                "Use a modern terminal:\n"
                "  macOS:  iTerm2 (https://iterm2.com) or Terminal.app\n"
                "  Linux:  kitty, WezTerm, Ghostty, gnome-terminal\n"
                "Or set:  export TERM=xterm-256color\n"
                "Recheck: pm doctor"
            ),
            severity="warning",
            data={"TERM": term, "COLORTERM": colorterm},
        )
    return doctor._ok(
        f"terminal color ok (TERM={term}, COLORTERM={colorterm or 'unset'})",
        data={"TERM": term, "COLORTERM": colorterm},
    )
