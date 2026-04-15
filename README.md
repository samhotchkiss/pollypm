# PollyPM

PollyPM is a tmux-first control plane for people who want multiple AI coding sessions working in parallel without losing visibility or control. It is built for operators managing Claude Code and Codex CLI across real projects, with a live cockpit, heartbeat supervision, and issue-driven worker coordination. At a high level, PollyPM launches and monitors dedicated operator and worker sessions, routes work through a shared task pipeline, and keeps state recoverable through logs, checkpoints, and project-aware context. The result is managed multi-session AI coordination through native terminal sessions rather than opaque background agents.

## Commit Message Hook

Install the Conventional Commits `commit-msg` hook with:

```bash
python3 scripts/install_commit_msg_hook.py
```

Git will then run `scripts/commit-msg` on each commit and reject messages that do not match `type(scope): description`.
