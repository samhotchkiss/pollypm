**Last Verified:** 2026-04-22

## Summary

`release_check` queries the GitHub releases API for PollyPM's repo, filters by the user's configured release channel (`stable` drops prereleases; `beta` keeps them), and compares the latest release against the installed version. Results cache for 24 hours in `~/.pollypm/release-check.json`. The module **never raises** — network, DNS, timeout, or parse failures all return `None`, so callers (`pm doctor` banner, rail pill, `pm upgrade`) never need defensive `try/except`.

Touch this module when the repo moves, when the cache format changes, or when the channel set expands. Do not add UI here — the banner text is a pure function consumed by callers.

## Core Contracts

```python
# src/pollypm/release_check.py
@dataclass(slots=True)
class ReleaseCheck:
    current: str
    latest: str
    channel: str
    upgrade_available: bool
    cached_at: float

def check_latest(
    channel: str = "stable",
    *,
    owner: str = "samhotchkiss",
    repo: str = "pollypm",
    cache_path: Path | None = None,
    ttl_seconds: int = 86_400,
    timeout: float = 3.0,
    force_refresh: bool = False,
    network_fetch: Callable[[], list[dict]] | None = None,
) -> ReleaseCheck | None: ...

def banner_text(check: ReleaseCheck) -> str: ...

def update_banner_line(
    config_path: Path | None = None, *, channel: str | None = None,
) -> str: ...          # "" if no upgrade available

def invalidate_cache(path: Path | None = None) -> None: ...
```

## File Structure

- `src/pollypm/release_check.py` — the module.
- `src/pollypm/upgrade.py` — `pm upgrade` implementation; consumes the same cache.
- `src/pollypm/upgrade_notice.py` — small helper for cockpit notices.
- `src/pollypm/cli_features/upgrade.py` — Typer wiring for `pm upgrade`.
- `tests/test_release_check.py` — pinned tests for channel filter, TTL, cache format, offline behavior.

## Implementation Details

- **Cache.** `~/.pollypm/release-check.json` stores one `ReleaseCheck` per channel. Switching between `stable` and `beta` uses different cache keys, so the cache effectively auto-invalidates on channel switch.
- **Channel resolution.** `_resolve_channel(config_path)` reads `config.pollypm.release_channel` with a `"stable"` fallback on missing / unparseable config. Invalid channel strings (e.g. user typo `"batta"`) also fall back to stable — `_validate_release_channel` logs a warning.
- **Beta semantics.** Beta picks the highest version regardless of prerelease flag. A stable release that lands after a beta is still the latest for beta users.
- **`network_fetch` test seam.** Production paths never pass this. Tests pass a callable returning release dicts to avoid touching the network.
- **Known limitation.** The GitHub releases API is called unauthenticated. Rate limits are shared with other unauthenticated clients on the same IP (60 req/hr). The 24 h TTL keeps this well under the limit in practice, but a scripted cache-bust loop will hit it.
- **Version parsing.** `_tag_version` strips a leading `v` and parses via `packaging.version.Version`. Tags that are not PEP 440 are dropped silently.

## Related Docs

- [features/upgrade.md](../features/upgrade.md) — `pm upgrade` wiring.
- [modules/doctor.md](doctor.md) — consumer of `update_banner_line`.
- [modules/config.md](config.md) — `[pollypm].release_channel` setting.
