# 0027 Overnight stability test: comprehensive test coverage audit

# Overnight Stability Test

## Objective
Audit the test suite and add missing test coverage. This issue is designed to keep
the worker busy for several hours, exercising the heartbeat monitoring pipeline.

## Tasks (do them in order, commit after each)
1. Check which source files in src/pollypm/ have NO corresponding test file. List them.
2. For the 3 most important untested files, write basic test files with at least 2 tests each.
3. Run the full test suite and report the results.
4. Check for any functions in supervisor.py that have 0 test coverage. Write tests for 2 of them.
5. Run the test suite again and report final count.

## Rules
- Commit after each task with a descriptive message
- Run pytest after each change to make sure nothing breaks
- If you get stuck, say what you are stuck on and wait for guidance
- Take your time — quality over speed

## Acceptance Criteria
- At least 6 new test functions added
- All tests pass
- Each task has its own commit

## Task 1 Audit Result
Source files in `src/pollypm/` with no direct `tests/test_<module>.py` counterpart:

- `src/pollypm/__init__.py`
- `src/pollypm/account_tui.py`
- `src/pollypm/agent_profiles/__init__.py`
- `src/pollypm/agent_profiles/base.py`
- `src/pollypm/agent_profiles/builtin.py`
- `src/pollypm/cockpit_rail.py`
- `src/pollypm/cockpit_ui.py`
- `src/pollypm/control_prompts.py`
- `src/pollypm/defaults/__init__.py`
- `src/pollypm/defaults/magic/__init__.py`
- `src/pollypm/defaults/rules/__init__.py`
- `src/pollypm/doc_backends/__init__.py`
- `src/pollypm/doc_backends/base.py`
- `src/pollypm/doc_backends/markdown.py`
- `src/pollypm/heartbeats/__init__.py`
- `src/pollypm/heartbeats/api.py`
- `src/pollypm/heartbeats/base.py`
- `src/pollypm/heartbeats/local.py`
- `src/pollypm/knowledge_extract.py`
- `src/pollypm/memory_backends/__init__.py`
- `src/pollypm/memory_backends/base.py`
- `src/pollypm/memory_backends/file.py`
- `src/pollypm/models.py`
- `src/pollypm/plugin_api/__init__.py`
- `src/pollypm/plugin_api/v1.py`
- `src/pollypm/plugin_host.py`
- `src/pollypm/plugins_builtin/__init__.py`
- `src/pollypm/plugins_builtin/claude/plugin.py`
- `src/pollypm/plugins_builtin/codex/plugin.py`
- `src/pollypm/plugins_builtin/core_agent_profiles/plugin.py`
- `src/pollypm/plugins_builtin/docker_runtime/plugin.py`
- `src/pollypm/plugins_builtin/inline_scheduler/plugin.py`
- `src/pollypm/plugins_builtin/local_heartbeat/plugin.py`
- `src/pollypm/plugins_builtin/local_runtime/plugin.py`
- `src/pollypm/providers/__init__.py`
- `src/pollypm/providers/base.py`
- `src/pollypm/providers/claude.py`
- `src/pollypm/providers/codex.py`
- `src/pollypm/runtime_env.py`
- `src/pollypm/runtime_launcher.py`
- `src/pollypm/runtimes/__init__.py`
- `src/pollypm/runtimes/base.py`
- `src/pollypm/runtimes/docker.py`
- `src/pollypm/runtimes/local.py`
- `src/pollypm/schedulers/__init__.py`
- `src/pollypm/schedulers/base.py`
- `src/pollypm/schedulers/inline.py`
- `src/pollypm/storage/__init__.py`
- `src/pollypm/task_backends/__init__.py`
- `src/pollypm/task_backends/base.py`
- `src/pollypm/task_backends/file.py`
- `src/pollypm/task_backends/github.py`
- `src/pollypm/tmux/__init__.py`
- `src/pollypm/tmux/client.py`

## Task 3 Full Suite Result
`uv run pytest -q`

- `466 passed in 118.23s (0:01:58)`
