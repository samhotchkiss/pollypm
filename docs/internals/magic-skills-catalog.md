# Magic Skills Catalog — Initial 60+

**Status:** v1 starter pack. Curated from `ericblue/visual-explainer-skill`, `coleam00/excalidraw-diagram-skill`, `Cocoon-AI/architecture-diagram-generator`, `madewithclaude/awesome-claude-artifacts`, `travisvn/awesome-claude-skills`, and `skills.sh/`.

## What this is

Magic skills are **content** loaded by the `magic` plugin (per `docs/plugin-discovery-spec.md` §1). Each skill is a single markdown file with frontmatter (`name`, `description`, `when_to_trigger`, `kind`, `body`). Workers, PMs, and reviewers see relevant skills surfaced based on their current task.

This catalog is the v1 starter pack — 60+ skills covering architecture, code quality, testing, frontend, backend, deploy, docs, and process. Users can drop additional skills into `~/.pollypm/content/magic/magic_skill/` (per the content_paths convention from #169).

## Format

Each skill is a markdown file at `plugins_builtin/magic/skills/<slug>.md`:

```markdown
---
name: <slug>
description: One-sentence elevator pitch.
when_to_trigger:
  - <pattern A>
  - <pattern B>
kind: magic_skill
attribution: <source repo/url>
---

# <Title>

## When to use
...

## Process
...

## Example invocation
...

## Outputs
...
```

The `when_to_trigger` field drives auto-surfacing — if the agent's current task or context matches any pattern, the skill is offered.

## The 60+

### Architecture & Visualization (10)

1. **visual-explainer** — Turn any concept into AI-generated visual (whiteboard, infographic, diagram, mockup) via OpenAI/Gemini image gen. Triggers: "explain visually," "diagram this," "create a visual."
2. **excalidraw-diagram** — Generate Excalidraw diagrams from natural language; render to PNG via Playwright. Triggers: "excalidraw," "sketch," "system flow."
3. **architecture-diagram** — Standalone HTML/SVG architecture diagrams (dark theme). Triggers: "architecture diagram," "system architecture," "infrastructure layout."
4. **mermaid-diagram** — Mermaid flowcharts / sequence diagrams / state diagrams / ERDs. Triggers: "flowchart," "sequence diagram," "ER diagram."
5. **svg-design** — Hand-crafted SVG icons/illustrations from spec. Triggers: "icon," "logo," "vector illustration."
6. **canvas-design** — PNG/PDF visual designs with deliberate design philosophy. Triggers: "marketing image," "design asset."
7. **algorithmic-art** — Generative art via p5.js with seeded randomness. Triggers: "generative art," "creative visual."
8. **slack-gif-creator** — Animated GIFs sized for Slack. Triggers: "gif," "animated emoji."
9. **brand-guidelines** — Apply consistent brand colors / typography to any artifact. Triggers: "brand colors," "match our style."
10. **extract-design-system** — Extract design tokens (colors, spacing, type) from existing code/screenshots. Triggers: "extract design system," "analyze design tokens."

### Documents (8)

11. **docx-create** — Generate Word documents with tracked changes, comments, formatting. Triggers: "word doc," ".docx," "tracked changes."
12. **pdf-toolkit** — Extract / merge / split / form-fill PDFs. Triggers: "pdf," "extract from pdf," "merge pdfs."
13. **pptx-create** — Generate PowerPoint with layouts / templates / charts. Triggers: "presentation," "slides," ".pptx."
14. **xlsx-create** — Generate Excel with formulas / charts / data analysis. Triggers: "spreadsheet," "excel," "csv to xlsx."
15. **markdown-document** — Well-structured markdown with TOC, sections, tables. Triggers: "writeup," "documentation," "spec doc."
16. **frontend-slides** — Animation-rich HTML presentations (or convert from .pptx). Triggers: "html slides," "animated deck."
17. **internal-comms** — Status reports, newsletters, FAQs. Triggers: "status update," "newsletter," "stakeholder comms."
18. **doc-coauthoring** — Collaboratively author with multiple contributors / styles. Triggers: "co-author," "merge contributions."

### Code Quality & Review (10)

19. **test-driven-development** — Write the failing test first, then implement, then refactor. Triggers: "TDD," "write tests first," "red-green-refactor."
20. **systematic-debugging** — Methodical bug isolation: reproduce → minimize → bisect → fix → regression-test. Triggers: "debug," "stuck on a bug," "intermittent failure."
21. **requesting-code-review** — Structure a PR description so reviewers can act fast. Triggers: "ready for review," "open a PR," "needs review."
22. **receiving-code-review** — Triage feedback, address comments, request re-review without churn. Triggers: "review came back," "address feedback."
23. **verification-before-completion** — Final validation pass before marking done (tests / acceptance / smoke). Triggers: "almost done," "ready to ship," "before pr."
24. **polish** — Refine code: naming, comments, edge cases, dead code. Triggers: "polish," "cleanup," "before merge."
25. **critique** — Generate constructive critique of someone else's code, scoped to actionable items. Triggers: "review this," "feedback on."
26. **extract-module** — Refactor inlined logic into a reusable module / function. Triggers: "extract function," "refactor to module."
27. **impeccable-code** — Patterns for production-grade code: error handling, observability, testability, simplicity. Triggers: "production-ready," "harden this."
28. **security-audit** — Static analysis pass (CodeQL/Semgrep style), variant analysis, common-vuln checks. Triggers: "security review," "audit for vulnerabilities."

### Testing & QA (6)

29. **webapp-testing-playwright** — Drive a local web app via Playwright; capture screenshots; assert behavior. Triggers: "test the UI," "playwright test," "verify in browser."
30. **playwright-best-practices** — Page-object pattern, waiting strategies, fixture usage, parallelism. Triggers: "playwright project setup," "flaky e2e."
31. **ios-simulator** — Build, navigate, and test iOS apps in the simulator. Triggers: "ios," "swift," "iphone simulator."
32. **ffuf-web-fuzzing** — Authenticated web fuzzing for pentesting; auto-calibration; result analysis. Triggers: "pentest," "web fuzzing," "find endpoints."
33. **load-test** — Simple load test scripting (k6 / Locust) + result interpretation. Triggers: "load test," "stress test," "throughput."
34. **regression-suite-curation** — Add a regression test for every fixed bug; keep the suite fast and meaningful. Triggers: "fixed a bug," "prevent regression."

### Frontend / UI (8)

35. **frontend-design** — Bold design decisions, no generic aesthetics; React + Tailwind. Triggers: "design a UI," "make it beautiful," "frontend polish."
36. **shadcn-ui** — Pattern enforcement for shadcn components: composition, theming, accessibility. Triggers: "shadcn," "ui components."
37. **tailwind-design-system** — Build a coherent design system with Tailwind tokens. Triggers: "tailwind setup," "design system."
38. **web-artifacts-builder** — Self-contained HTML artifacts using React / Tailwind / shadcn. Triggers: "html artifact," "demo page."
39. **design-taste-frontend** — Cultivate aesthetic judgment: hierarchy, contrast, whitespace, microinteractions. Triggers: "looks bad," "design feedback."
40. **react-component-patterns** — Composition, hooks, context, suspense, error boundaries. Triggers: "react component," "react hook."
41. **vue-best-practices** — Composition API, reactivity rules, component patterns. Triggers: "vue," "vue 3."
42. **mobile-app-design** — Modern mobile UX: gestures, transitions, native feel. Triggers: "mobile design," "ios design," "android design."

### Backend & Database (8)

43. **supabase-postgres** — Supabase setup, RLS policies, pgvector, edge functions. Triggers: "supabase," "postgres on supabase."
44. **neon-postgres** — Neon serverless Postgres setup, branching, scale-to-zero. Triggers: "neon," "serverless postgres."
45. **firebase-stack** — Auth, Firestore, App Hosting, Genkit basics. Triggers: "firebase," "firestore."
46. **api-design-principles** — REST + GraphQL design: pagination, errors, versioning, idempotency. Triggers: "design an api," "rest endpoint."
47. **nodejs-backend-patterns** — Layered architecture, error handling, async patterns, observability. Triggers: "node backend," "express server."
48. **python-performance-optimization** — Profiling, hot paths, async, vectorization, caching. Triggers: "slow python," "profile python."
49. **typescript-advanced-types** — Generics, conditional types, mapped types, template literals. Triggers: "typescript types," "type-level programming."
50. **better-auth** — Authentication patterns with `better-auth` library: sessions, OAuth, MFA. Triggers: "auth," "login," "session management."

### Deploy / Infrastructure (6)

51. **deploy-to-vercel** — Vercel deployment workflows, env vars, preview branches, edge functions. Triggers: "deploy," "vercel."
52. **github-actions** — CI/CD with GitHub Actions: matrices, caching, secrets, deployments. Triggers: "ci," "github action."
53. **azure-cost-optimization** — Reduce Azure spend via right-sizing, reservations, idle resource cleanup. Triggers: "azure cost," "reduce cloud spend."
54. **kubernetes-deploy** — Container deploy, manifests, helm, observability. Triggers: "kubernetes," "k8s deploy."
55. **observability-stack** — Logs, metrics, traces — pick a stack (OTel/Grafana/Datadog), instrument, dashboard. Triggers: "observability," "monitoring."
56. **release-engineering** — Versioning (semver), changelog, tag conventions, rollback strategy. Triggers: "release," "tag a version."

### Workflow & Process (8)

57. **writing-plans** — Decompose a goal into a written plan with milestones + acceptance criteria. Triggers: "plan a project," "design phase."
58. **executing-plans** — Work through a plan systematically: pick next item, ship, verify, move on. Triggers: "execute plan," "next step."
59. **using-git-worktrees** — Parallel dev across branches via worktrees; safe cleanup. Triggers: "git worktree," "parallel branches."
60. **finishing-a-development-branch** — Cleanup pass before merge: rebase, squash, delete, archive. Triggers: "merge ready," "wrap up branch."
61. **subagent-driven-development** — Coordinate multiple AI agents on a single goal: split work, integrate outputs, resolve conflicts. Triggers: "split this work," "parallel agents."
62. **dispatching-parallel-agents** — When and how to fork concurrent agents vs. sequential work. Triggers: "should we parallelize," "agent fan-out."
63. **git-commit-message** — Write a clean commit: imperative subject, body explains why, footer for refs. Triggers: "commit message," "ready to commit."
64. **pre-mortem** — Imagine the project failed; reverse-engineer why; surface risks before they bite. Triggers: "what could go wrong," "risk audit."

### Browser & Web Automation (4)

65. **browser-use-agent** — Drive a browser as an agent: navigate, fill forms, scrape, screenshot. Triggers: "browser," "automate web task."
66. **firecrawl-scrape** — Scrape a single page or whole site; structured extraction. Triggers: "scrape," "extract from website."
67. **web-asset-generator** — Favicons, app icons, OG images, social cards. Triggers: "favicon," "social card," "og image."
68. **mcp-builder** — Build a Model Context Protocol server to integrate an external API/service. Triggers: "mcp server," "integrate external api."

### Meta (3)

69. **skill-creator** — Interactive skill creation; produces a new magic skill markdown file with proper frontmatter. Triggers: "create a skill," "add a skill."
70. **brainstorming** — Generate-then-prune ideation; constraints-first; cluster + rank. Triggers: "brainstorm," "ideas for."
71. **devil's-advocate** — Steel-man the opposite position; surface objections; rank by severity. Triggers: "devil's advocate," "challenge this."

## How surfacing works

When an agent boots a session (via the M05 memory injection from #234), the magic plugin queries skills whose `when_to_trigger` patterns match the agent's current task summary. Top-N skills (default 5) get prepended as a "Skills available" section, listing name + one-sentence description. The agent can request a specific skill by name to get its full body.

For workers in `implement_module` flow, this means the architect persona sees `test-driven-development` + `requesting-code-review` + `verification-before-completion` automatically surface — without anyone explicitly invoking them.

## Implementation roadmap (one issue, sequential)

1. Create the 71 markdown files at `plugins_builtin/magic/skills/<slug>.md` per the format above.
2. Each `body` is 50–200 lines: when-to-use, process, example invocation, outputs, common failure modes.
3. Update the magic plugin's manifest to declare `[content] kinds = ["magic_skill"]` and `user_paths = ["skills"]` (if not already done in the plugin-discovery migration).
4. Write a smoke test that loads all skills via `magic.list_skills()` and asserts each parses cleanly.

Estimated scope: large. ~71 × 100 LOC = ~7K lines of curated content. One agent, sequential by category. ~3-4 hours of agent time.
