# Plugin Discovery & Advertisement Specification

**Status:** draft — target for v1.1. Supersedes ad-hoc discovery in `plugin_host.py`.

> **Warning:** This document is still a draft target for v1.1, and some
> sections may describe behavior that is not yet shipped or not yet
> stable in the current v1 release.
> If you're authoring a plugin today, start with
> [`plugin-authoring.md`](plugin-authoring.md) and confirm details
> against the current `pm plugins` behavior.

This spec defines how PollyPM finds plugins, how plugins advertise what they provide, and how users add, disable, and inspect plugins. It also codifies the distinction between *plugins* (code that extends the rail) and *content* (data that extends what a plugin already provides).

---

## 1. Principle: plugins are code, content is data

A **plugin** adds a new *kind* of thing the rail can do: a new provider adapter, a new session-service implementation, a new task backend. Plugins ship as Python packages with a manifest and a `plugin.py` that exports a `PollyPMPlugin`.

**Content** is a new *instance* of an existing kind: a magic skill, a flow template, an agent persona prompt, a deploy recipe. Content is data — a markdown or TOML file — loaded and registered by a plugin at startup. Adding content does not require a plugin install.

The rail provides a single mechanism — `host.content_paths(plugin_name)` — that returns the ordered directories a plugin should scan for content. Every content-hosting plugin (magic, core_agent_profiles, flow templates, future triage/briefing) uses this helper, so precedence and override semantics are uniform.

---

## 2. Discovery paths

Plugins are discovered from four sources, in this precedence order (later wins on name collisions):

| Source | Path | Purpose |
|---|---|---|
| 1. Built-in | `src/pollypm/plugins_builtin/*/` | Shipped with PollyPM |
| 2. Python entry_points | `pollypm.plugins` group in installed packages | `pip install polly-foo` |
| 3. User-global | `~/.pollypm/plugins/*/` | User-wide drop-in |
| 4. Project-local | `<project>/.pollypm/plugins/*/` | Per-project override |

At startup, `ExtensionHost` walks each source, builds a unified plugin registry, and logs a warning when a later source overrides an earlier one (same pattern as commit `e56ac22`).

Both directory-style (`<dir>/pollypm-plugin.toml` + `<dir>/plugin.py`) and entry-point style are supported. Entry-point plugins point at a module path whose top-level `plugin` symbol is a `PollyPMPlugin` instance. No manifest is required for entry-point plugins — the manifest fields are carried on the `PollyPMPlugin` dataclass itself.

---

## 3. Manifest format

```toml
# pollypm-plugin.toml
name = "polly-github-sync"
version = "0.4.1"
api_version = "1"
entrypoint = "plugin"
description = "Bidirectional sync of work-service tasks with GitHub Issues."
homepage = "https://github.com/example/polly-github-sync"

[[capabilities]]
kind = "sync_adapter"
name = "github"
replaces = []                # optional: names of other capabilities this one supersedes
requires_api = ">=1,<2"      # optional: rail API version constraint

[[capabilities]]
kind = "task_backend"
name = "github"

[content]
kinds = ["flow_template"]     # advertises that this plugin hosts loadable content
user_paths = ["flows"]        # relative paths the plugin scans under each discovery root
```

### Required fields

- `name` — globally unique plugin identifier (reverse-DNS or dash-separated allowed)
- `version` — SemVer of the plugin itself
- `api_version` — PollyPM plugin API major version the plugin targets (currently `"1"`)
- `entrypoint` — module file (relative) or dotted module path whose `plugin` attribute is the `PollyPMPlugin` instance

### Optional fields

- `description`, `homepage` — shown in `pm plugins show`
- `[[capabilities]]` — one block per declared capability (see §4)
- `[content]` — see §5

Validation: unknown top-level keys warn but don't fail. Missing required fields fail load with a clear error; the plugin is skipped, not crashed.

---

## 4. Structured capabilities

Every capability is an object, not a bare string:

```toml
[[capabilities]]
kind = "provider"            # one of: provider, runtime, session_service,
                             # heartbeat, scheduler, agent_profile,
                             # task_backend, memory_backend, doc_backend,
                             # sync_adapter, transcript_source,
                             # job_handler, roster_entry
name = "claude"              # unique within (kind)
replaces = ["claude_legacy"] # optional: names this capability explicitly supersedes
requires_api = ">=1,<2"      # optional per-capability constraint
```

The set of valid `kind` values is versioned with the plugin API. Adding a new kind is a minor-version bump; removing one is a major.

`pm plugins list` reads these and shows the actual surface area of each plugin, not a free-form description.

---

## 5. Content paths

A plugin that hosts content declares it under `[content]`:

```toml
[content]
kinds = ["magic_skill", "deploy_recipe"]
user_paths = ["skills", "deploys"]
```

At runtime, the plugin asks the host for the resolved content paths:

```python
from pollypm.plugin_api.v1 import PluginAPI

def on_startup(api: PluginAPI) -> None:
    for path in api.content_paths("magic", kind="magic_skill"):
        for skill_file in path.glob("*.md"):
            register_skill(skill_file)
```

`content_paths(plugin_name, kind)` returns a list of `Path` objects in discovery-order precedence:

1. `<plugin_dir>/<user_paths[i]>/` (shipped content)
2. `~/.pollypm/content/<plugin_name>/<kind>/` (user-added)
3. `<project>/.pollypm/content/<plugin_name>/<kind>/` (project-added)

Later directories override earlier ones by filename. This means a user or project can shadow a shipped magic skill by dropping a same-named file in their own content path — identical to the plugin discovery precedence model.

---

## 6. Lifecycle

The `PollyPMPlugin` dataclass stays declarative. Plugins with startup side effects register them through the roster API (see issue #162) using `@on_startup`:

```python
from pollypm.plugin_api.v1 import PollyPMPlugin, PluginAPI

def initialize(api: PluginAPI) -> None:
    api.roster.register_recurring(
        schedule="@on_startup",
        handler_name="magic.load_skills",
        payload={},
    )
    api.jobs.register_handler(
        name="magic.load_skills",
        handler=load_all_skills,
    )

plugin = PollyPMPlugin(
    name="magic",
    capabilities=(...),
    initialize=initialize,    # called once after manifest parse + validation
)
```

This keeps the plugin surface declarative (factories + a single `initialize` callback), avoids inventing ad-hoc lifecycle hooks, and means every plugin's startup work flows through the same job queue as every other background task.

---

## 7. CLI surface

```
pm plugins list               # all loaded plugins, their capabilities, their source
pm plugins show <name>        # manifest + resolved content paths + load errors
pm plugins install <spec>     # pip spec, git URL, or local directory
pm plugins uninstall <name>
pm plugins enable <name>
pm plugins disable <name>
pm plugins doctor             # validates all manifests, reports conflicts / missing deps
```

Each command also accepts `--json` for machine consumption. `pm plugins doctor` is the frontend for manifest validation and override-collision reporting — surfaces every warning that today only lives in `logger.debug`.

---

## 8. Disable knob

```toml
# pollypm.toml
[plugins]
disabled = ["magic", "docker_runtime"]
```

Disabled plugins are discovered but not loaded. They appear in `pm plugins list` marked `disabled`, with a one-line reason (explicit config / api version mismatch / missing dependency). No uninstall required to silence a noisy plugin.

---

## 9. Trust model

Plugins run in-process with full Python privileges. There is no sandbox in v1.

Documentation reads: *"Plugins are code you run as part of PollyPM. Treat them like dependencies — review what you install."*

Post-v1 options, not committed to:
- Manifest signing with a project-maintained keyring
- Capability allow-list in `pollypm.toml` (plugin may only register capabilities the user permits)
- Subprocess isolation for high-risk capability kinds (sync adapters, task backends)

None of these are prerequisites for shipping v1. Revisit when the external plugin ecosystem warrants.

---

## 10. Versioning

- Every plugin declares `api_version` targeting a major version of the rail API (currently `"1"`).
- The rail guarantees backward-compatible changes within a major; a plugin targeting `"1"` keeps working across all `1.x` rail releases.
- A plugin that declares `api_version = "2"` is rejected at load on a `1.x` rail, with a clear error in `pm plugins list`.
- No `min_rail_version` / `max_rail_version` is introduced yet — the `api_version` is the compatibility contract.

When the plugin API ships a v2, we publish a migration guide and keep the v1 loader alive for at least one major rail cycle.

---

## 11. Layout summary

```
~/.pollypm/
  plugins/                          # user-installed plugins
    polly-github-sync/
      pollypm-plugin.toml
      plugin.py
  content/                          # user-added content keyed by plugin + kind
    magic/
      magic_skill/
        my-custom-deploy.md

<project>/.pollypm/
  plugins/                          # project-local plugins
  content/                          # project-local content (same shape)

src/pollypm/plugins_builtin/
  magic/
    pollypm-plugin.toml
    plugin.py
    skills/                         # shipped content (declared in [content].user_paths)
      default-deploy.md
```

---

## 12. Implementation roadmap

Tracked as separate issues:

1. Multi-path discovery (`~/.pollypm/plugins`, `<project>/.pollypm/plugins`, entry_points)
2. Structured `[[capabilities]]` in manifest parser; reject bare-string capabilities with a migration warning for one release
3. `host.content_paths(plugin_name, kind)` helper + user/project content layout
4. `PollyPMPlugin.initialize(api)` callback wired through `plugin_host.load()`
5. `pm plugins` CLI surface (list / show / install / uninstall / enable / disable / doctor)
6. `[plugins].disabled` config key
7. Documentation + migration notes for existing built-in plugins

Each item ships independently. The order above is the safe sequencing: discovery and capabilities before the CLI that reads them; CLI before the disable knob that needs a way to surface state.
