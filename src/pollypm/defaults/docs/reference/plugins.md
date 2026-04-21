# Plugin System

PollyPM uses a file-based plugin system. Plugins are directories containing a manifest (`pollypm-plugin.toml`) and a Python module. No pip install required — just drop the directory in the right location.

Third-party plugins and provider packages are not sandboxed. They run with the same user privileges as PollyPM, so only install code you trust.

## Plugin Types

| Type | What it provides | Example |
|------|-----------------|---------|
| provider | CLI agent adapter (launch commands, transcript sources) | Claude, Codex |
| heartbeat | Health classification and recovery logic | local heartbeat |
| scheduler | Job scheduling backend | inline scheduler |
| runtime | Execution environment | local tmux, Docker |
| agent_profile | Persona prompts for sessions | Polly, heartbeat, worker |

## Plugin Locations (Precedence: low → high)

1. **Built-in** — shipped with PollyPM (lowest precedence)
2. **Python entry_points** — `pollypm.plugins` group in installed packages
3. **User-global** — `~/.pollypm/plugins/<name>/` (overrides built-in)
4. **Project-local** — `<project>/.pollypm/plugins/<name>/` (overrides everything)

## Creating a Plugin

1. Create a directory: `~/.pollypm/plugins/my-plugin/`
2. Add a manifest `pollypm-plugin.toml`:
   ```toml
   api_version = "1"
   name = "my-plugin"
   kind = "provider"
   version = "0.1.0"
   entrypoint = "plugin.py:plugin"
   capabilities = ["provider"]
   description = "My custom agent provider"
   ```
3. Add `plugin.py`:
   ```python
   from pollypm.plugin_api.v1 import PollyPMPlugin
   from my_module import MyAdapter

   plugin = PollyPMPlugin(
       name="my-plugin",
       capabilities=("provider",),
       providers={"my-agent": MyAdapter},
   )
   ```

## Configuring Plugins

After installing a plugin, reference it in config:

```toml
# Use a custom heartbeat backend
[pollypm]
heartbeat_backend = "my-custom-heartbeat"

# Use a custom runtime for an account
[accounts.my_account]
runtime = "my-custom-runtime"

# Use a custom agent profile for a session
[sessions.my_session]
agent_profile = "my-custom-profile"
```

## Example: Look at Built-ins

The best way to learn the plugin API is to read the built-in plugins:
- `src/pollypm/plugins_builtin/claude/` — provider plugin
- `src/pollypm/plugins_builtin/local_heartbeat/` — heartbeat plugin
- `src/pollypm/plugins_builtin/core_agent_profiles/` — agent profile plugin
