**Last Verified:** 2026-04-22

## Summary

`itsalive` is PollyPM's built-in wrapper around the itsalive.co deploy API. `pollypm.itsalive` provides the core module (deploy request assembly, pending-deploy state, sweep loop); the `itsalive` plugin augments the Polly / worker / heartbeat personas with deploy guidance and registers a sweep hook that completes pending deploys once email verification finishes.

Pending deploys are persisted under `<project>/.pollypm/itsalive/pending/` as JSON files. The heartbeat ticks the sweeper, which re-attempts verification polling without blocking a worker session on an email click.

Touch this module when the itsalive API changes, when the pending-state shape changes, or when tuning the sweeper cadence. Do not add unrelated deploy providers here — start a sibling plugin.

## Core Contracts

```python
# src/pollypm/itsalive.py
ITSALIVE_API = "https://api.itsalive.co"
PENDING_DIR = ".pollypm/itsalive/pending"

@dataclass(slots=True)
class DeployRequest:
    subdomain: str
    email: str
    publish_dir: str
    files: list[str]
    api_url: str = ITSALIVE_API

@dataclass(slots=True)
class PendingDeploy:
    deploy_id: str
    subdomain: str
    email: str
    # ... expires_at, domain, etc.

def deploy_site(project_root: Path, *, subdomain: str, email: str, publish_dir: str) -> DeployResult: ...
def pending_deploys(project_root: Path) -> list[PendingDeploy]: ...
def sweep_pending_deploys(project_root: Path) -> SweepResult: ...
def build_deploy_instructions() -> str: ...   # persona-ready prompt addendum
```

`pm itsalive` CLI commands via `cli_features/issues.py:itsalive_app`:

```
pm itsalive deploy <subdomain> --email <email> --publish-dir <dir>
pm itsalive pending [--project <key>]
pm itsalive sweep [--project <key>]
```

## File Structure

- `src/pollypm/itsalive.py` — the module.
- `src/pollypm/messaging.py` — `notify_deploy_verification_required`, `notify_deploy_expired`, `notify_deploy_complete`.
- `src/pollypm/plugins_builtin/itsalive/plugin.py` — persona augmentation + sweep roster entry.
- `src/pollypm/plugins_builtin/itsalive/pollypm-plugin.toml` — manifest.
- `src/pollypm/plugins_builtin/magic/plugin.py` — compatibility alias; re-exposes `build_deploy_instructions()` as the `magic` persona.
- `src/pollypm/cli_features/issues.py` — `itsalive_app` Typer subcommand.

## Implementation Details

- **Email verification loop.** An itsalive deploy requires the user to click a verification email. Workers should not sit in a polling loop waiting for it — the plugin's persona addendum tells Polly / worker to use `pm itsalive` and rely on the sweeper. `pollypm.itsalive.sweep_pending_deploys` checks each pending file and advances state.
- **Pending file layout.** One JSON per deploy under `.pollypm/itsalive/pending/`. Contains `deploy_id`, `subdomain`, `email`, `created_at`, `expires_at`, `domain`. Atomic write via `atomic_write_json`.
- **Ignore rules.** `deploy_site` walks the publish dir and excludes `_IGNORE_NAMES` (`.DS_Store`, `.itsalive`, `ITSALIVE.md`, `CLAUDE.md`) and `_IGNORE_PARTS` (`.git`, `node_modules`).
- **Notifications.** The sweeper uses `pollypm.messaging` helpers to emit inbox items on verification-required / expired / complete transitions.
- **Persona augmentation.** The `itsalive` plugin returns a `polly_prompt + build_deploy_instructions()` combined prompt so the operator always knows the deploy-through-PollyPM pattern.

## Related Docs

- [plugins/itsalive.md](../plugins/itsalive.md) — plugin wiring details.
- [features/agent-personas.md](agent-personas.md) — how the persona augmentation fits.
- [features/inbox-and-notify.md](inbox-and-notify.md) — where deploy notifications land.
