---
name: russell
preferred_providers: [claude, codex]
role: reviewer
---

# Russell — provider policy

Russell is the reviewer persona. The provider policy is encoded in the
YAML frontmatter above and consumed by the session launcher when
multiple providers are registered:

- **Claude first, Codex fallback.** Review benefits from Claude's
  longer context window and stronger code-reading heuristics.
- The planner plugin's diversity resolver (pp06) may override this for
  critic-panel members when it needs at least one non-planner-provider
  critic; Russell is not a critic and is exempt from that rule.

The full reviewer prompt lives in
``pollypm.agent_profiles.defaults`` (``reviewer_prompt``).
That Python source is the source of truth for Russell's behaviour; this
Markdown file is the provider-policy declaration only, matching the
planner personas' shape so `pm plugins show` surfaces the policy for
every profile in one place.

Users override the policy per-persona in ``pollypm.toml`` — see
``docs/conventions.md`` for the override precedence.
