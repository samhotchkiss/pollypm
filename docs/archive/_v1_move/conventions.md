# Conventions

## Summary

Coding patterns, naming conventions, testing approaches, and tooling preferences.

## Provider policy

Agent personas declare a ``preferred_providers`` list in YAML
frontmatter in their profile markdown. The list is ordered — first
entry preferred, subsequent entries are fallbacks when the preferred
provider is unavailable / unregistered.

Defaults for the built-in personas:

| Persona | Preferred providers | Rationale |
|---|---|---|
| ``russell`` (reviewer) | ``[claude, codex]`` | Review benefits from longer context and stronger code-reading. |
| ``worker`` (implementer) | ``[codex, claude]`` | Code writing at speed favors Codex; Claude fallback. |
| ``architect`` (planner) | ``[claude, codex]`` | Planning is a reading/reasoning task. |
| Each critic (``critic_*``) | ``[claude, codex]`` | Planner-plugin diversity resolver may override one critic to Codex for real model diversity (pp06). |

``architect`` and each ``critic_*`` persona are contributed by the
``project_planning`` plugin; they are only available when that plugin is
loaded. Core personas shipped in v1 are ``polly``, ``russell``, and
``worker``.

**Overrides.** Users override per-persona in ``pollypm.toml``:

```toml
[agent_profiles.russell]
providers = ["codex"]
```

Override precedence (highest wins):

1. Explicit per-task role assignment on the work-service task.
2. ``pollypm.toml`` ``[agent_profiles.<name>].providers`` list.
3. The persona's ``preferred_providers`` frontmatter (the default).
4. First registered provider on the rail.

When a persona's preferred provider is unregistered at launch time,
the session service falls through the list in order and logs a warning
through the plugin host's event log. A persona whose entire list is
unregistered fails loudly rather than silently — providers are not
interchangeable for every task.

Planner plugin's diversity resolver (pp06) is an explicit exception:
when >1 provider is registered, it forces at least one critic onto the
non-planner provider to reduce correlated blind spots. Users override
per-critic in ``pollypm.toml`` and the resolver respects the override.

## Import boundaries (enforced in CI)

``tests/test_import_boundary.py`` runs on every PR and enforces three
guardrails left by the Supervisor decomposition (#179, #182, #186, #187):

1. **No direct ``from pollypm.supervisor import Supervisor``** outside
   ``pollypm.core/``, the ``service_api`` facade, and a small allow-list
   of integration points. New consumers should use
   ``pollypm.service_api.PollyPMService``.
2. **No ``supervisor._<anything>`` reach-through.** Every private helper
   that had callers was promoted to public during the decomposition; if
   you need one that isn't public, promote it first.
3. **No direct SQL on ``StateStore._conn`` / ``SQLiteWorkService._conn``.**
   Use the typed accessor methods the store exposes. The connection's
   lifecycle is owned by one module only.

If you hit one of these failures and the violation is genuinely
unavoidable (usually: a core-decomposition step that's still mid-flight),
add the file to the relevant allow-list inside
``tests/test_import_boundary.py`` **with a TODO comment pointing at the
issue that will remove it**. The companion ``_has_no_stale_entries``
tests keep the allow-lists honest as migrations land.

## Conventions

- Issue-based tracking (Issue NNNN format)
- tmux pane naming: 'pollypm-storage-closet:worker_XXX'
- Role names: Heartbeat (read-only, Bash only), Operator (extended permissions)
- Issue state machine: 01, 02, 03-needs-review, 04-in-review, completed
- Worker idle detection: 5+ cycles triggers Heartbeat alert
- Heartbeat status classification overrides manual `done` based on tmux pane content
- Documentation regeneration via pollypm repair
- Escalation response protocol: Heartbeat escalations demand immediate acknowledgment and concrete next steps
- Stop-looping directive: cease loop iteration immediately, state remaining task concisely, execute next step, report blocker
- History chunk analysis: sequential processing to build consolidated understanding
- Documentation verification: cross-check claims against actual code state and event timeline

*Last updated: 2026-04-13T01:29:31.935791Z*
