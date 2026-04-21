# Writing a PollyPM Plugin

A hello-world walkthrough for building, testing, and installing a PollyPM plugin end-to-end. Target audience: external authors who want to ship a new provider, runtime, session service, or agent profile without forking the core repo.

For the exhaustive spec see [`plugin-discovery-spec.md`](plugin-discovery-spec.md). This doc is the on-ramp.

---

## 1. Trust model first

PollyPM plugins run in-process with full Python privileges. There is no sandbox in v1.

> **Plugins are code you run as part of PollyPM. Treat them like dependencies — review what you install.**

This is why the CLI deliberately has no auto-install prompt and no signing. Post-v1 may add manifest signing, capability allow-lists, or subprocess isolation; v1 ships without any of that.

---

## 2. Anatomy of a plugin

Every plugin is either:

| Shape | Layout |
|---|---|
| **Directory** | A folder with `pollypm-plugin.toml` + `plugin.py`, dropped into a discovery path |
| **Entry-point** | A Python package that registers a `pollypm.plugins` entry point |

The runtime payload is identical: a `PollyPMPlugin` instance from `pollypm.plugin_api.v1`.

### Minimum directory layout

```
my-plugin/
├── pollypm-plugin.toml    # manifest
└── plugin.py              # exports `plugin = PollyPMPlugin(...)`
```

### Minimum entry-point layout

```
my_plugin_pkg/
├── pyproject.toml         # declares entry point under [project.entry-points."pollypm.plugins"]
└── my_plugin_pkg/
    └── __init__.py        # module-level `plugin = PollyPMPlugin(...)`
```

No `pollypm-plugin.toml` is required for entry-point plugins — the manifest fields live on the `PollyPMPlugin` dataclass itself.

---

## 3. Hello, world: the shortest possible plugin

`~/.pollypm/plugins/hello-world/pollypm-plugin.toml`:

```toml
api_version = "1"
name = "hello-world"
version = "0.1.0"
entrypoint = "plugin.py:plugin"
description = "The tiniest possible PollyPM plugin."
requires_api = ">=1,<2"

[[capabilities]]
kind = "agent_profile"
name = "hello"
requires_api = ">=1,<2"
```

`~/.pollypm/plugins/hello-world/plugin.py`:

```python
from pollypm.plugin_api.v1 import Capability, PollyPMPlugin
from pollypm.agent_profiles.base import AgentProfile, AgentProfileContext


class HelloProfile(AgentProfile):
    name = "hello"

    def build_prompt(self, context: AgentProfileContext) -> str | None:
        return "Hello, world! I am a PollyPM plugin."


plugin = PollyPMPlugin(
    name="hello-world",
    version="0.1.0",
    description="The tiniest possible PollyPM plugin.",
    capabilities=(Capability(kind="agent_profile", name="hello"),),
    agent_profiles={"hello": lambda: HelloProfile()},
)
```

Verify it loaded:

```
$ pm plugins list
NAME          SOURCE  VERSION  STATUS / CAPABILITIES
hello-world   user    0.1.0    agent_profile:hello
...
```

`pm plugins show hello-world` prints the manifest snapshot plus every resolved path.

---

## 4. Structured capabilities

The `[[capabilities]]` blocks are how `pm plugins list`, `pm plugins doctor`, and future tooling reason about what each plugin provides. Each block carries:

```toml
[[capabilities]]
kind = "provider"           # what rail surface this extends
name = "claude"             # unique within (kind)
replaces = ["claude_legacy"]  # optional: capability names this supersedes
requires_api = ">=1,<2"       # optional: rail API version constraint
```

Recognised `kind` values (as of API v1):

```
provider, runtime, session_service, heartbeat, scheduler,
agent_profile, task_backend, memory_backend, doc_backend,
sync_adapter, transcript_source, recovery_policy,
job_handler, roster_entry, hook
```

Unknown kinds are preserved but logged as warnings — a plugin targeting a future API still loads on an older rail, just with reduced reporting fidelity.

**Bare-string capabilities (`capabilities = ["provider"]`) still parse** for one release with a deprecation warning. Migrate before the next major.

---

## 5. Startup side effects — `initialize(api)`

If your plugin needs to register recurring jobs, wire event hooks, or pre-warm caches, use the `initialize` callback instead of import-time side effects:

```python
from pollypm.plugin_api.v1 import Capability, PluginAPI, PollyPMPlugin


def _sweep(payload: dict) -> dict:
    # run your work…
    return {"status": "ok"}


def _initialize(api: PluginAPI) -> None:
    api.jobs.register_handler("mine.sweep", _sweep)
    api.roster.register_recurring("@every 5m", "mine.sweep", {})


plugin = PollyPMPlugin(
    name="mine",
    capabilities=(
        Capability(kind="job_handler", name="mine.sweep"),
        Capability(kind="roster_entry", name="mine.sweep"),
    ),
    initialize=_initialize,
)
```

`PluginAPI` surface:

| Attribute | Purpose |
|---|---|
| `api.roster` | `RosterAPI` — register recurring schedules |
| `api.jobs` | `JobHandlerAPI` — register job handlers |
| `api.content_paths(kind=...)` | Resolved directories for this plugin's content |
| `api.config` | The loaded `PollyPMConfig` (may be `None` in some test contexts) |
| `api.state_store` | Lazy `StateStore` handle (may be `None`) |
| `api.emit_event(name, payload)` | Record a `plugin.<name>.<event>` row into the events table |

`initialize` runs once per process, after all plugins load and validate, before the first heartbeat tick. A raised exception marks your plugin **degraded** — kept loaded, surfaced by `pm plugins show`, but does not prevent other plugins from initialising.

---

## 6. Shipping content with a plugin

Plugins can ship data (markdown skills, flow templates, deploy recipes) alongside the code. Declare it with `[content]`:

```toml
[content]
kinds = ["magic_skill", "deploy_recipe"]
user_paths = ["skills", "deploys"]
```

At runtime ask the host for resolved paths:

```python
for path in api.content_paths(kind="magic_skill"):
    for skill_file in path.glob("*.md"):
        register_skill(skill_file)
```

`api.content_paths(kind=...)` returns an ordered list of directories in precedence order (later shadows earlier by filename):

1. `<plugin_dir>/<user_paths[i]>/` — shipped bundled content
2. `~/.pollypm/content/<plugin_name>/<kind>/` — user-added
3. `<project>/.pollypm/content/<plugin_name>/<kind>/` — project-added

A user can drop a same-named file in `~/.pollypm/content/magic/magic_skill/my-deploy.md` and shadow a shipped skill without touching the plugin install.

---

## 7. Testing your plugin

Install `pytest` in your development environment and test against a real `ExtensionHost`:

```python
from pathlib import Path
from pollypm.plugin_host import ExtensionHost


def test_hello_world_loads(tmp_path: Path) -> None:
    plugin_dir = tmp_path / ".pollypm" / "plugins" / "hello-world"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "pollypm-plugin.toml").write_text(
        """api_version = "1"
name = "hello-world"
version = "0.1.0"
entrypoint = "plugin.py:plugin"
requires_api = ">=1,<2"

[[capabilities]]
kind = "agent_profile"
name = "hello"
""",
    )
    (plugin_dir / "plugin.py").write_text(
        "from pollypm.plugin_api.v1 import Capability, PollyPMPlugin\n"
        "plugin = PollyPMPlugin(name='hello-world')\n"
    )

    host = ExtensionHost(tmp_path)
    assert "hello-world" in host.plugins()
```

`ExtensionHost(tmp_path)` treats `tmp_path` as the project root, so the project-local `.pollypm/plugins/` path is what gets scanned.

---

## 8. Installing into an existing install

Four options, pick the one that matches how your users consume you:

| Distribution | Command | Where it lands |
|---|---|---|
| Local directory | `pm plugins install ./my-plugin` | `~/.pollypm/plugins/my-plugin/` |
| Git URL | `pm plugins install https://github.com/you/my-plugin.git` | `~/.pollypm/plugins/my-plugin/` |
| PyPI package | `pm plugins install my-plugin` | via `pip install`, loaded through entry_points |
| Source checkout | Drop into `<project>/.pollypm/plugins/` directly | Project-local override |

A restart of `pm` is required for newly installed plugins to take effect — no hot-reload in v1.

---

## 9. Disabling without uninstalling

```
$ pm plugins disable hello-world
Disabled 'hello-world'. Restart PollyPM for the change to take effect.
```

This writes `[plugins].disabled = ["hello-world"]` in `~/.pollypm/pollypm.toml`. `pm plugins list` still shows the plugin with a `disabled` marker and reason.

Re-enable with `pm plugins enable hello-world`.

---

## 10. Debugging a misbehaving plugin

`pm plugins doctor` is the all-purpose diagnostic. It runs strict-mode validation against every plugin and prints every warning, disabled record, and degraded record:

```
$ pm plugins doctor --json
{
  "plugins_loaded": 12,
  "plugins_disabled": 1,
  "plugins_degraded": 0,
  "validation": { "all_passed": true, ... },
  "disabled": [
    {"name": "magic", "source": "builtin", "reason": "config", "detail": "disabled by pollypm.toml [plugins].disabled"}
  ],
  ...
}
```

Common failure modes:

| Symptom | Likely cause | Fix |
|---|---|---|
| Plugin not in `pm plugins list` | Path wrong / manifest parse error | `pm plugins doctor` — check `errors` |
| `status: degraded` | `initialize()` raised | Check `degraded_reason` in `pm plugins show` |
| `status: disabled`, reason `api_version` | Your `api_version` excludes the rail | Widen `requires_api` |
| `status: disabled`, reason `load_error` | Validation failed (missing methods on a factory output) | Fix the factory; re-run `pm plugins doctor` |

---

## 11. Capability version & forward compatibility

A plugin targeting API `"1"` keeps working across every `1.x` rail release. Declaring `api_version = "2"` on a `1.x` rail causes the plugin to be rejected at load with a clear error — no partial loading, no silent misbehavior.

When the rail ships v2, v1 plugins get a one-major-cycle deprecation window and a published migration guide.

---

## 12. Where to ask questions

- Spec: [`plugin-discovery-spec.md`](plugin-discovery-spec.md)
- Core API: `src/pollypm/plugin_api/v1.py`
- Reference implementations: `src/pollypm/plugins_builtin/`
- Issue tracker: open a plugin-author issue with the `plugin` label on github.com/samhotchkiss/pollypm

Happy plugging.
