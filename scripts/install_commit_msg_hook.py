#!/usr/bin/env python3
"""Install the Conventional Commits commit-msg hook into .git/hooks."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_SOURCE = REPO_ROOT / "scripts" / "commit-msg"


def main() -> int:
    git_dir = REPO_ROOT / ".git"
    hooks_dir = git_dir / "hooks"
    hook_target = hooks_dir / "commit-msg"

    if not git_dir.exists():
        print("No .git directory found in repository root.")
        return 1

    hooks_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HOOK_SOURCE, hook_target)
    os.chmod(hook_target, 0o755)
    print(f"Installed commit-msg hook at {hook_target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
