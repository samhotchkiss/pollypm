"""Setuptools build customizations for PollyPM."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


BUILD_INFO_FILENAME = "_build_info.json"


def _git_value(repo: Path, args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


class build_py_with_build_info(build_py):
    """Embed source git metadata into built wheels from local checkouts."""

    def run(self) -> None:
        super().run()
        repo = Path(__file__).resolve().parent
        target = Path(self.build_lib) / "pollypm" / BUILD_INFO_FILENAME
        git_sha = _git_value(repo, ["rev-parse", "HEAD"])
        if not git_sha:
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            return
        build_info: dict[str, str] = {"git_sha": git_sha}
        commit_time = _git_value(repo, ["show", "-s", "--format=%cI", "HEAD"])
        if commit_time:
            build_info["git_commit_time"] = commit_time

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(build_info, sort_keys=True) + "\n",
            encoding="utf-8",
        )


setup(cmdclass={"build_py": build_py_with_build_info})
