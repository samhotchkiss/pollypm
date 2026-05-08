---
name: worker
preferred_providers: [codex, claude]
role: implementer
---

# Worker — provider policy

The worker persona is the implementer — hands on the keyboard. The
YAML frontmatter above encodes the default provider policy:

- **Codex first, Claude fallback.** Code generation at speed is where
  Codex shines; Claude is the fallback when Codex is unavailable or
  rate-limited.
- The policy flips the priority from Russell's: reviewers read (Claude
  wins), workers write (Codex wins).

The full worker prompt lives in
``pollypm.agent_profiles.defaults`` (``worker_prompt``).
That Python source is the source of truth for worker behaviour; this
Markdown file is the provider-policy declaration only, matching the
planner personas' shape so `pm plugins show` can surface the policy
for every profile in one place.

Users override the policy per-persona in ``pollypm.toml`` — see
``docs/conventions.md`` for the override precedence.
