---
## Summary

PollyPM v1 defines a persona and prompt system that gives each project's lead session a named identity and provides task-specific instruction sets that optimize agent behavior. Personas are behavioral, not cosmetic — they change how the agent approaches work, what it prioritizes, and which tools it reaches for. The prompt system is pluggable — PollyPM ships opinionated defaults, but users can replace it. Rules are loaded on demand based on current work type, magic capabilities are injected as awareness at session start, and a set of universal rules applies to every persona (though all are overridable per-project).

---

# 11. Agent Personas and Prompt System

## Agent Naming

Every project's lead worker session gets a persona with a short, common name.

### Naming Rules

- The name is auto-picked: it starts with the same letter as the project name
- Names are short, common, and easy to type
- Examples: "Nora" for a news project, "Pete" for pollypm, "Olive" for otter-camp
- The user can change the name at any time by asking the agent directly ("change your name to X")
- Names are stored in the project config and persist across sessions

### Name Display

The name appears in the TUI left rail in parentheses after the project name:

```
news (Nora)
pollypm (Pete)
otter-camp (Olive)
```


## Core Persona Types

Each persona type defines a behavioral profile that shapes the agent's approach to work.

### Developer/Builder

The default for code projects. Optimized for implementation, testing, git workflow, and code quality.

- Prioritizes working code over documentation
- Writes tests alongside implementation
- Commits in small, meaningful pieces
- Defaults to running and verifying rather than theorizing

### Marketer

Optimized for content, copy, campaigns, and analytics.

- Prioritizes audience impact and messaging clarity
- Understands metrics, funnels, and conversion
- Focuses on tone, voice, and brand consistency
- Defaults to testing copy against stated goals

### Designer

Optimized for UI/UX, visual design, user research, and prototyping.

- Prioritizes user experience and usability
- Thinks in flows, states, and edge cases
- Focuses on consistency and design system adherence
- Defaults to prototyping and visual verification

### Researcher

Optimized for analysis, data science, investigation, and report writing.

- Prioritizes rigor and evidence
- Structures findings hierarchically
- Focuses on reproducibility and citation
- Defaults to thorough exploration before conclusions

### Ops/DevOps

Optimized for infrastructure, deployment, monitoring, and automation.

- Prioritizes reliability and observability
- Thinks in failure modes and recovery paths
- Focuses on automation and repeatability
- Defaults to defensive configuration and rollback plans

### Custom

Users can define their own persona types via agent profile plugins (doc 04). Custom personas follow the same structure as built-in types and can override any behavioral dimension.


## What Persona Affects

The persona type shapes multiple dimensions of agent behavior:

| Dimension | How Persona Affects It |
|-----------|----------------------|
| System prompt tone and framing | Developer gets direct and technical; Marketer gets audience-focused; Researcher gets methodical |
| Default behavior rules | Developer commits often; Marketer checks copy against goals; Ops checks for failure modes |
| Preferred tool usage patterns | Developer leans on git and test runners; Designer leans on browsers and preview tools |
| Ambiguous instruction handling | Developer asks "what should it do?"; Marketer asks "who is the audience?"; Ops asks "what if it fails?" |
| Prioritization | Developer prioritizes tests; Marketer prioritizes audience impact; Researcher prioritizes evidence |


## Universal Rules

These rules apply to every persona regardless of type. They are always loaded as opinionated defaults. They are not sacred cows — users can override any of them per-project through the override system (see Override Hierarchy below).

1. **Always default to action.** Never say "if you want, I can do X" — just do it. The agent is here to work, not to ask permission for things it can clearly do.

2. **If you can do it, do it.** Do not tell the user how to do something the agent can do itself. Execute, do not instruct.

3. **Prove that your work works.** Use tmux, interact with the result, verify from a user's perspective. Unit tests are necessary but not sufficient. If you built a UI, look at it. If you built an API, call it.

4. **Commit meaningful progress regularly.** Small, frequent commits that each represent a working state. Do not accumulate a giant diff.

5. **Try to solve blockers yourself before escalating.** When stuck, exhaust reasonable self-help options before asking the operator. Read docs, search code, try alternatives.


## Rules System

Rules are situational instruction sets stored as markdown files in `<project>/.pollypm/rules/`. When a session starts, the agent is told "here are the scenarios we have rules for." The agent reads the relevant rule file on demand when it starts working on a matching task type.

PollyPM ships built-in default rules. Users add their own alongside or as overrides.

### File Layout

```
<project>/.pollypm/rules/
  bugfix.md         # How to approach bug fixing
  build.md          # How to approach building new features
  audit.md          # How to approach code audits
  deploy.md         # User-added: deployment procedures
  migration.md      # User-added: database migration rules
```

### Built-in Rules

**rules/bugfix.md** — Loaded when the agent is assigned a bug fix issue.

1. First, figure out how to reproduce the bug on your own
2. Write a failing test that captures the bug
3. Fix the bug
4. Verify the fix passes the test AND works from a user perspective
5. Add unit tests to prevent regression
6. Audit all changes you made along the way — make sure nothing else regressed
7. Run the full test suite before declaring done

**rules/build.md** — Loaded when the agent is building a new feature or component.

1. Break work into small, testable pieces
2. Write unit tests as you go
3. Commit after each meaningful piece works
4. Always do full integration tests, not just unit tests
5. Test from a user's perspective — launch the thing and interact with it
6. Do not rely only on automated tests

**rules/audit.md** — Loaded when the agent is reviewing or auditing existing code.

1. Read the target code thoroughly before making judgments
2. Check for: correctness, security vulnerabilities, performance issues, code clarity, test coverage
3. Verify claims against actual behavior (run it, do not trust comments)
4. Document findings with specific file:line references
5. Prioritize findings by severity

### Override Hierarchy

Rules follow a three-tier override hierarchy. Project-local overrides win.

```
built-in → user-global (~/.pollypm/rules/) → project-local (<project>/.pollypm/rules/)
```

- **Built-in**: Ships with PollyPM. The opinionated defaults.
- **User-global**: Stored in `~/.pollypm/rules/`. Applies to all projects for this user. Overrides built-in rules with the same filename.
- **Project-local**: Stored in `<project>/.pollypm/rules/`. Applies only to this project. Overrides both built-in and user-global rules with the same filename.

This hierarchy applies to universal rules as well. Users can override any universal rule per-project.

### Rules and Magic Manifest Injection

At session start, the agent receives an auto-generated manifest — NOT the full content of every rule and magic file, just a brief catalog. Each manifest entry contains:

- **Name** (e.g., "bugfix")
- **One-line description** (e.g., "Specialized instructions for fixing bugs")
- **Trigger condition** (e.g., "When you are assigned a bug fix issue or are debugging a problem")
- **File path** (e.g., ".pollypm/rules/bugfix.md")

Example manifest format:

```
## Available Rules
You have specialized instructions for these scenarios. Read the relevant file before starting that type of work.
- bugfix: Specialized bug fixing process → .pollypm/rules/bugfix.md (when fixing bugs or debugging)
- build: Feature building process → .pollypm/rules/build.md (when building new features)
- audit: Code audit checklist → .pollypm/rules/audit.md (when reviewing or auditing code)

## Available Magic
You have access to these capabilities. Use them when the situation calls for it.
- visual-explainer: Create visual documentation and diagrams → .pollypm/magic/visual-explainer.md
- deploy-site: Put a site online quickly → .pollypm/magic/deploy-site.md
```

The manifest is auto-generated from the merged override hierarchy (built-in package defaults, then `~/.pollypm/rules/`, then `<project>/.pollypm/rules/`). Project-local overrides win.

The agent reads the full rule/magic file only when it enters the relevant scenario. This keeps injection token-light while giving full awareness of available instructions and capabilities.

### Built-in Rules and Magic Packaging

Built-in rules and magic ship as files inside the PollyPM Python package (e.g., `pollypm/defaults/rules/bugfix.md`). They are NOT copied into the project on init — they are read from the package at runtime.

Override hierarchy at file level:

1. **Built-in from package** — baseline defaults shipped with PollyPM
2. **User-global** (`~/.pollypm/rules/` or `~/.pollypm/magic/`) — user overrides that apply to all projects
3. **Project-local** (`<project>/.pollypm/rules/` or `<project>/.pollypm/magic/`) — project-specific overrides

Project-local wins over user-global, which wins over built-in. PollyPM upgrades automatically bring new and improved built-in rules without clobbering user overrides. Users never need to manually manage built-in files.

### Agent-Driven Configuration

When a user expresses disagreement with a rule or behavior during a session, the agent should not just comply silently. Instead, the agent asks: "Would you like me to change this rule for this project?" If the user agrees, the agent creates or edits the appropriate override file in `<project>/.pollypm/rules/` (or `~/.pollypm/rules/` for a global change). This makes configuration a conversation, not a config-file hunt.


## Magic System

Magic is a capability catalog stored in `<project>/.pollypm/magic/`. Each magic entry describes a superpower the agent can use — visual explainers, site deployment, diagram generation, etc. At session start, the agent is told "here are superpowers you can use" and given awareness of all available magic.

### File Layout

```
<project>/.pollypm/magic/
  visual-explainer.md      # Generate visual explanations of code/concepts
  deploy-site.md           # Deploy a site to staging/production
  generate-diagram.md      # Create architecture or flow diagrams
  screenshot-verify.md     # Take and verify screenshots of UI
```

### Magic Entry Format

Each magic file contains:

- **What it does**: A clear description of the capability
- **When to use it**: The situations where this magic is appropriate
- **How to invoke it**: The concrete steps, commands, or tools to use

### Magic Injection

Magic awareness is injected at session start. The agent does not load every magic file in full — it receives a summary catalog ("here are the superpowers available to you and when to use them") and reads individual magic files on demand when a situation calls for them.


## Prompt Assembly

When a session starts or resumes, its prompt is assembled from multiple layers in order:

1. **Base system prompt.** Derived from the persona type. Sets tone, framing, and default behavioral rules (including universal rules, subject to overrides).
2. **Project context injection.** The contents of `docs/project-overview.md` (doc 08) are injected to give the agent high-level understanding of the project's goals, current phase, and conventions.
3. **Rules and magic manifest.** The auto-generated manifest (see Rules and Magic Manifest Injection above) is injected, giving the agent awareness of all available rules and magic capabilities without loading their full content. This is token-light — just names, descriptions, triggers, and file paths.
4. **Active rule.** Loaded based on the current work type (bug fix, build, audit, etc.). Only one rule is active at a time. The agent reads the full rule file on demand when it enters the relevant scenario.
5. **Active issue context.** If the agent is working on a tracked issue, the issue details and any linked context are injected.
6. **Recent checkpoint.** For session continuity, the latest checkpoint is included so the agent knows where it left off.

Each layer adds to the prompt without replacing previous layers. The universal rules are part of the base system prompt and are always present (subject to per-project overrides).


## Agent Profile Backend

The agent profile system uses a backend interface defined by the provider adapter system (doc 04):

| Method | Purpose |
|--------|---------|
| `system_prompt_blocks()` | Return the ordered list of prompt blocks for assembly |
| `behavior_rules()` | Return the active behavior rules for the current persona and task |
| `preferred_provider()` | Return the preferred provider CLI for this persona |
| `preferred_model()` | Return the preferred model for this persona |
| `preferred_reasoning_level()` | Return the preferred reasoning level for this persona |

### Profile Composition

Profiles are composable with a clear override hierarchy:

```
built-in base → user-global (~/.pollypm/) → project-local (<project>/.pollypm/) → session override
```

Each layer can add, modify, or remove settings from the previous layer. Session overrides are ephemeral and do not persist. Project overrides are stored in `<project>/.pollypm/`. User overrides are stored in `~/.pollypm/`. Project-local overrides win over user-global, which win over built-in defaults.


## Resolved Decisions

1. **Auto-naming by project initial.** Agent names start with the same letter as the project name. This is memorable, consistent, and requires no user configuration. Users can override at any time.

2. **Names shown in TUI rail.** The persona name appears in parentheses after the project name in the TUI left rail, making it easy to identify which agent is which at a glance.

3. **Persona types are behavioral, not cosmetic.** The persona type changes how the agent works, what it prioritizes, and how it handles ambiguity. It is not just a name or an avatar.

4. **Rules and magic replace static instruction sets.** Rules are situational instruction files loaded on demand. Magic is a capability catalog injected as awareness. Both are stored in `<project>/.pollypm/` and are user-extensible.

5. **Universal rules are overridable opinionated defaults.** The five universal rules (default to action, do it yourself, prove it works, commit often, self-solve blockers) are strong defaults that apply to all personas, but they are not sacred cows. Users can override any of them per-project through the override hierarchy (built-in, user-global, project-local). When a user disagrees with a rule, the agent should offer to create an override.

6. **Prove-it-works is mandatory, not optional.** Every persona must verify its work from a user's perspective, not just through automated tests. This is a universal rule, not a suggestion.

7. **project-overview.md is always injected.** The project overview is part of every prompt assembly. The agent always has high-level project context available.


## Cross-Doc References

- Provider adapter interface and agent profile backend: [04-extensibility-and-plugin-system.md](04-extensibility-and-plugin-system.md)
- Issue tracker and work assignment: [06-issue-management.md](06-issue-management.md)
- Project overview document: [08-project-state-memory-and-documentation.md](08-project-state-memory-and-documentation.md)
- Inbox routing between PM, PA, and workers: [09-inbox-and-threads.md](09-inbox-and-threads.md)
- Heartbeat and session monitoring: [10-heartbeat-and-supervision.md](10-heartbeat-and-supervision.md)
