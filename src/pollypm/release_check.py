"""Release-channel update check for the ``pm doctor`` banner (#714).

Queries GitHub releases for the PollyPM repo, filters by channel (stable
→ non-prerelease only; beta → includes prereleases), and compares
against the installed version. Never raises — a check that fails at the
network, DNS, or parsing layer returns ``None`` so callers never need a
try/except. Results are cached for 24h in ``~/.pollypm/release-check.json``
so repeat invocations inside that window don't touch the network.

This module is consumed by ``pm doctor`` (banner) and by the rail
update-pill (#715). The actual upgrade path (``pm upgrade``) lives in
#716 and consumes the same cache.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

import pollypm


_DEFAULT_OWNER = "samhotchkiss"
_DEFAULT_REPO = "pollypm"
_DEFAULT_TTL_SECONDS = 86_400
_DEFAULT_TIMEOUT_SECONDS = 3.0


@dataclass(slots=True)
class ReleaseCheck:
    """Outcome of a ``check_latest`` invocation.

    ``upgrade_available`` is True only when ``Version(latest) > Version(current)``.
    Callers should render a banner only when True; the struct is
    returned in all non-offline cases so the rail can still show
    "up-to-date on v1.3.2 (beta)" if it wants to.
    """

    current: str
    latest: str
    channel: str
    upgrade_available: bool
    cached_at: float


def _cache_path() -> Path:
    return Path.home() / ".pollypm" / "release-check.json"


def _load_cached(path: Path, channel: str, ttl_seconds: int) -> ReleaseCheck | None:
    try:
        raw = path.read_text()
    except (OSError, FileNotFoundError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("channel") != channel:
        # Channel changed since the cache was written — invalidate so
        # the caller re-queries against the new filter.
        return None
    cached_at = data.get("cached_at")
    if not isinstance(cached_at, (int, float)):
        return None
    if (time.time() - float(cached_at)) > ttl_seconds:
        return None
    try:
        return ReleaseCheck(
            current=str(data["current"]),
            latest=str(data["latest"]),
            channel=str(data["channel"]),
            upgrade_available=bool(data["upgrade_available"]),
            cached_at=float(cached_at),
        )
    except KeyError:
        return None


def _save_cache(path: Path, check: ReleaseCheck) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(check)))
    except OSError:
        # Cache-write failure shouldn't break the upgrade-check flow;
        # the next invocation just pays the network cost again.
        return


def invalidate_cache(path: Path | None = None) -> None:
    """Drop the cached release-check result.

    Called when the user switches channels in settings (#713) so the
    next check queries with the new filter rather than serving stale
    data from the old channel.
    """
    target = path or _cache_path()
    try:
        target.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def _fetch_releases(
    owner: str, repo: str, *, timeout: float
) -> list[dict[str, Any]] | None:
    url = f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=30"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"pollypm/{pollypm.__version__}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, list):
        return None
    return data


def _tag_version(tag: str) -> Version | None:
    raw = tag.lstrip("v").strip()
    if not raw:
        return None
    try:
        return Version(raw)
    except InvalidVersion:
        return None


def _select_latest(
    releases: list[dict[str, Any]], channel: str,
) -> tuple[str, Version] | None:
    best: tuple[str, Version] | None = None
    for release in releases:
        if not isinstance(release, dict):
            continue
        if release.get("draft"):
            continue
        is_pre = bool(release.get("prerelease"))
        if channel == "stable" and is_pre:
            continue
        # Beta picks the highest release regardless of prerelease flag —
        # a stable release that lands after a beta is still the
        # "latest" for beta users who want the newest bits.
        tag = release.get("tag_name")
        if not isinstance(tag, str):
            continue
        version = _tag_version(tag)
        if version is None:
            continue
        if best is None or version > best[1]:
            best = (tag.lstrip("v"), version)
    return best


def check_latest(
    channel: str = "stable",
    *,
    owner: str = _DEFAULT_OWNER,
    repo: str = _DEFAULT_REPO,
    cache_path: Path | None = None,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    force_refresh: bool = False,
    network_fetch: Any | None = None,
) -> ReleaseCheck | None:
    """Return the latest-release comparison, or ``None`` when unknown.

    ``channel`` picks the filter: ``"stable"`` drops prereleases;
    ``"beta"`` keeps them. Unknown channels are treated as stable.

    The ``network_fetch`` hook is a test seam — pass a callable that
    returns a list of release dicts to skip the real HTTP path. The
    production call site never supplies this.
    """
    if channel not in {"stable", "beta"}:
        channel = "stable"
    target_cache = cache_path or _cache_path()
    if not force_refresh:
        cached = _load_cached(target_cache, channel, ttl_seconds)
        if cached is not None:
            return cached

    if network_fetch is not None:
        releases = network_fetch()
    else:
        releases = _fetch_releases(owner, repo, timeout=timeout)
    if not releases:
        return None

    selected = _select_latest(releases, channel)
    if selected is None:
        return None
    latest_label, latest_version = selected

    current_str = pollypm.__version__
    current_version = _tag_version(current_str)
    upgrade_available = (
        current_version is not None and latest_version > current_version
    )
    check = ReleaseCheck(
        current=current_str,
        latest=latest_label,
        channel=channel,
        upgrade_available=upgrade_available,
        cached_at=time.time(),
    )
    _save_cache(target_cache, check)
    return check


def banner_text(check: ReleaseCheck) -> str:
    """Render the one-line banner shown by ``pm doctor`` and the rail.

    Callers decide whether to show it (only when ``upgrade_available``).
    """
    channel_label = f" ({check.channel} channel)" if check.channel != "stable" else ""
    return (
        f"↑ PollyPM v{check.current} → v{check.latest} available"
        f"{channel_label}. Run: pm upgrade"
    )


def _resolve_channel(config_path: Path | None) -> str:
    """Read the active release channel from config with a safe fallback.

    A missing / unparseable config returns ``"stable"`` rather than
    raising — the update check is a nice-to-have signal, never a
    startup blocker.
    """
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config

    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return "stable"
    try:
        config = load_config(path)
    except Exception:  # noqa: BLE001
        return "stable"
    channel = getattr(config.pollypm, "release_channel", "stable")
    if channel not in {"stable", "beta"}:
        return "stable"
    return channel


def update_banner_line(
    config_path: Path | None = None,
    *,
    channel: str | None = None,
    network_fetch: Any | None = None,
) -> str:
    """Return the ``pm doctor`` update-available banner line, or "".

    Empty string means either no upgrade is available, the network was
    unreachable, or a cache-miss hit a quiet failure. Callers should
    only print the result when non-empty.

    ``channel`` overrides the config lookup — used by tests and by the
    rail pill to show a "what would I see on beta?" preview.
    """
    resolved = channel if channel is not None else _resolve_channel(config_path)
    check = check_latest(resolved, network_fetch=network_fetch)
    if check is None or not check.upgrade_available:
        return ""
    return banner_text(check)
