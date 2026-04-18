# Rules & Magic

Rules are specialized instructions for specific types of work. Magic capabilities are tools you can invoke for specific tasks. Both are discovered automatically from the file system.

## Available Rules

Read the relevant rule file BEFORE starting that type of work.

| Rule | When to use | Path |
|------|------------|------|
| audit | Reviewing or auditing existing code | pollypm/defaults/rules/audit.md |
| bugfix | Fixing bugs or debugging | pollypm/defaults/rules/bugfix.md |
| build | Building new features or components | pollypm/defaults/rules/build.md |

## Available Magic

Use these when the situation calls for them.

| Magic | When to use | Path |
|-------|------------|------|
| deploy-site | Site needs staging or production deployment | pollypm/defaults/magic/deploy-site.md |
| itsalive | Launching or extending an itsalive site | pollypm/defaults/magic/itsalive.md |
| visual-explainer | Generate a rendered HTML page (diagram, plan-review, diff-review, slide deck, data table) instead of a markdown dump | pollypm/defaults/magic/visual-explainer/SKILL.md |

`visual-explainer` is a directory-style skill vendored from
[nicobailon/visual-explainer](https://github.com/nicobailon/visual-explainer)
(MIT). The entrypoint is `SKILL.md`; individual command prompts live under
`commands/` (`plan-review`, `diff-review`, `generate-visual-plan`,
`generate-slides`, `generate-web-diagram`, `project-recap`, `fact-check`,
`share`), with HTML templates under `templates/` and CSS / library / slide
pattern references under `references/`. See `VENDORED.md` in that directory
for upstream version and refresh instructions.

## Adding Custom Rules or Magic

Drop a `.md` file in the appropriate directory. It will be auto-discovered.
You can also drop a subdirectory containing a `SKILL.md` (directory-style
skill); the directory name becomes the skill name and `SKILL.md` is the
entrypoint. Directory-style skills let you ship supporting files
(`commands/`, `templates/`, `references/`, `scripts/`, `LICENSE`) alongside
the prompt.

**User-global** (applies to all projects):
- Rules: `~/.pollypm/rules/my-rule.md`
- Magic: `~/.pollypm/magic/my-magic.md`

**Project-local** (applies to one project, overrides user-global and built-in):
- Rules: `<project>/.pollypm/rules/my-rule.md`
- Magic: `<project>/.pollypm/magic/my-magic.md`

**File format:**
```markdown
Description: One-line description of what this does
Trigger: when to use this (e.g., "when deploying to production")

# Rule Title

Step-by-step instructions...
```

**Precedence:** project-local > user-global > built-in. Same-named files at higher levels replace lower ones.
