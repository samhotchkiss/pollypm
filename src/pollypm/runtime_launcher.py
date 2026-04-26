from __future__ import annotations

import base64
import json
import os
import shutil
import sys
from pathlib import Path


def _decode_payload(encoded: str) -> dict[str, object]:
    padded = encoded + "=" * (-len(encoded) % 4)
    raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("invalid launcher payload")
    return payload


def _ensure_path(path: str | None) -> None:
    if path:
        Path(path).mkdir(parents=True, exist_ok=True)


def _touch(path: str | None) -> None:
    if path:
        marker = Path(path)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()


def _cleanup_fresh_codex_home(codex_home: str | None) -> None:
    if not codex_home:
        return
    home = Path(codex_home)
    for child in (home / "history.jsonl",):
        child.unlink(missing_ok=True)
    for child in (home / "sessions", home / "shell_snapshots"):
        if child.exists():
            shutil.rmtree(child, ignore_errors=True)
    for pattern in ("logs_*.sqlite*", "state_*.sqlite*"):
        for match in home.glob(pattern):
            if match.is_dir():
                shutil.rmtree(match, ignore_errors=True)
            else:
                match.unlink(missing_ok=True)


def _write_codex_agents_md(codex_home: str | None, content: str | None) -> None:
    if not codex_home or not content:
        return
    target = Path(codex_home) / "AGENTS.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content.rstrip() + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv
    if len(args) != 2:
        raise SystemExit("usage: python -m pollypm.runtime_launcher <payload>")
    payload = _decode_payload(args[1])
    cwd = str(payload["cwd"])
    env = {str(key): str(value) for key, value in dict(payload.get("env", {})).items()}
    codex_agents_md = env.pop("PM_CODEX_HOME_AGENTS_MD", None)
    command_argv = [str(item) for item in list(payload["argv"])]
    resume_argv = [str(item) for item in list(payload.get("resume_argv") or [])]
    resume_marker = payload.get("resume_marker")
    fresh_marker = payload.get("fresh_launch_marker")
    home = payload.get("home")
    codex_home = payload.get("codex_home")
    claude_config_dir = payload.get("claude_config_dir")

    os.chdir(cwd)
    _ensure_path(str(home) if isinstance(home, str) else None)
    _ensure_path(str(codex_home) if isinstance(codex_home, str) else None)
    _ensure_path(str(claude_config_dir) if isinstance(claude_config_dir, str) else None)
    _write_codex_agents_md(str(codex_home) if isinstance(codex_home, str) else None, codex_agents_md)
    if isinstance(resume_marker, str):
        _ensure_path(str(Path(resume_marker).parent))
    if isinstance(fresh_marker, str):
        _ensure_path(str(Path(fresh_marker).parent))

    exec_env = os.environ.copy()
    exec_env.update(env)

    if resume_argv and isinstance(resume_marker, str) and Path(resume_marker).exists():
        os.execvpe(resume_argv[0], resume_argv, exec_env)

    if isinstance(fresh_marker, str):
        Path(fresh_marker).unlink(missing_ok=True)
    if isinstance(resume_marker, str) and not Path(resume_marker).exists():
        _cleanup_fresh_codex_home(str(codex_home) if isinstance(codex_home, str) else None)
    if isinstance(fresh_marker, str):
        _touch(fresh_marker)

    os.execvpe(command_argv[0], command_argv, exec_env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
