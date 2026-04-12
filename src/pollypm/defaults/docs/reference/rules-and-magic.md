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
| visual-explainer | A concept or architecture would be clearer with a diagram | pollypm/defaults/magic/visual-explainer.md |

## Adding Custom Rules or Magic

Drop a `.md` file in the appropriate directory. It will be auto-discovered.

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
