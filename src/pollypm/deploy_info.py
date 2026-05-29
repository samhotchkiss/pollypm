"""Runtime build/source introspection for deploy-staleness checks.

The local ``uv tool install --from /path/to/pollypm pollypm`` path copies
the package into a tool environment. Because PollyPM's dev version string
does not change on every commit, that installed copy can silently fall
behind the source checkout it was installed from. This module keeps the
introspection in one place so the health route stays thin and ``pm
doctor`` can reuse the same facts.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from importlib.metadata import PackageNotFoundError, distribution, version
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse


PACKAGE_NAME = "pollypm"
PACKAGE_IMPORT_NAME = "pollypm"
_GIT_TIMEOUT_SECONDS = 1.0
_STALE_CLOCK_SKEW_SECONDS = 60
_EXCLUDED_DIRS = frozenset({"__pycache__", ".git", ".mypy_cache", ".pytest_cache"})
_EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo"})
_PACKAGE_SUFFIXES = frozenset(
    {
        ".css",
        ".html",
        ".js",
        ".json",
        ".md",
        ".py",
        ".sh",
        ".template",
        ".toml",
        ".yaml",
        ".yml",
    }
)


@dataclass(frozen=True, slots=True)
class RuntimeBuildInfo:
    """Build/source facts safe to expose from ``/api/v1/health``."""

    package_name: str
    version: str
    package_path: str | None = None
    package_file: str | None = None
    dist_info_path: str | None = None
    direct_url: str | None = None
    direct_url_editable: bool | None = None
    direct_url_vcs: str | None = None
    direct_url_vcs_commit_id: str | None = None
    source_checkout: str | None = None
    source_git_sha: str | None = None
    source_git_commit_time: str | None = None
    served_git_sha: str | None = None
    served_git_commit_time: str | None = None
    package_mtime: str | None = None
    package_mtime_path: str | None = None
    stale: bool | None = None
    stale_reason: str | None = None

    def as_dict(self) -> dict[str, object | None]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DeployStaleness:
    """Result of comparing the served package to its source checkout."""

    state: str  # "ok" | "stale" | "unknown"
    status: str
    reason: str
    data: dict[str, object]


def _safe_resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _run_git(cwd: Path, args: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return (-1, "")
    return (result.returncode, (result.stdout or "").strip())


def _git_root(path: Path | None) -> Path | None:
    if path is None:
        return None
    start = path if path.is_dir() else path.parent
    if not start.exists():
        return None
    rc, out = _run_git(start, ["rev-parse", "--show-toplevel"])
    if rc != 0 or not out:
        return None
    return _safe_resolve(Path(out))


def _git_value(path: Path | None, args: list[str]) -> str | None:
    root = _git_root(path)
    if root is None:
        return None
    rc, out = _run_git(root, args)
    if rc != 0 or not out:
        return None
    return out


def _git_head(path: Path | None) -> str | None:
    return _git_value(path, ["rev-parse", "HEAD"])


def _git_head_time(path: Path | None) -> str | None:
    return _git_value(path, ["show", "-s", "--format=%cI", "HEAD"])


def _iso_from_mtime(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=UTC).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _package_location(import_name: str) -> tuple[Path | None, Path | None]:
    spec = importlib.util.find_spec(import_name)
    package_file: Path | None = None
    package_path: Path | None = None
    if spec is not None:
        if spec.origin and spec.origin not in {"built-in", "namespace"}:
            package_file = _safe_resolve(Path(spec.origin))
        locations = spec.submodule_search_locations
        if locations:
            package_path = _safe_resolve(Path(next(iter(locations))))
    if package_path is None and package_file is not None:
        package_path = package_file.parent
    return package_path, package_file


def _distribution_info(package_name: str) -> tuple[str, Path | None, dict[str, Any] | None]:
    try:
        dist = distribution(package_name)
        package_version = version(package_name)
    except PackageNotFoundError:
        return ("0.0.0+unknown", None, None)
    except Exception:  # noqa: BLE001
        return ("0.0.0+unknown", None, None)

    dist_path = getattr(dist, "_path", None)
    dist_info_path = _safe_resolve(Path(dist_path)) if dist_path is not None else None
    direct_url: dict[str, Any] | None = None
    try:
        raw = dist.read_text("direct_url.json")
    except Exception:  # noqa: BLE001
        raw = None
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            direct_url = parsed
    return (package_version, dist_info_path, direct_url)


def _local_path_from_file_url(url: str | None) -> Path | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme and parsed.scheme != "file":
        return None
    if parsed.scheme == "file":
        path = unquote(parsed.path)
        if parsed.netloc and parsed.netloc not in {"localhost", "127.0.0.1"}:
            path = f"//{parsed.netloc}{path}"
        return _safe_resolve(Path(path))
    return _safe_resolve(Path(unquote(url)))


def _direct_url_fields(
    direct_url: dict[str, Any] | None,
) -> tuple[str | None, bool | None, str | None, str | None, Path | None]:
    if not direct_url:
        return (None, None, None, None, None)
    raw_url = direct_url.get("url")
    url = raw_url if isinstance(raw_url, str) else None
    dir_info = direct_url.get("dir_info")
    editable: bool | None = None
    if isinstance(dir_info, dict) and isinstance(dir_info.get("editable"), bool):
        editable = bool(dir_info["editable"])
    vcs_info = direct_url.get("vcs_info")
    vcs: str | None = None
    commit_id: str | None = None
    if isinstance(vcs_info, dict):
        raw_vcs = vcs_info.get("vcs")
        raw_commit = vcs_info.get("commit_id")
        vcs = raw_vcs if isinstance(raw_vcs, str) else None
        commit_id = raw_commit if isinstance(raw_commit, str) else None
    local_path = _local_path_from_file_url(url)
    return (url, editable, vcs, commit_id, local_path)


def _latest_package_mtime(root: Path | None) -> tuple[str | None, str | None]:
    if root is None or not root.exists():
        return (None, None)
    latest: tuple[float, Path] | None = None
    try:
        candidates = root.rglob("*") if root.is_dir() else iter([root])
        for path in candidates:
            try:
                rel_parts = path.relative_to(root).parts if root.is_dir() else ()
            except ValueError:
                rel_parts = ()
            if any(part in _EXCLUDED_DIRS for part in rel_parts):
                continue
            if not path.is_file() or path.suffix in _EXCLUDED_SUFFIXES:
                continue
            if path.suffix and path.suffix not in _PACKAGE_SUFFIXES:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if latest is None or mtime > latest[0]:
                latest = (mtime, path)
    except OSError:
        latest = None
    if latest is None:
        try:
            latest = (root.stat().st_mtime, root)
        except OSError:
            return (None, None)
    return (_iso_from_mtime(latest[0]), str(latest[1]))


def _infer_stale(info: RuntimeBuildInfo) -> tuple[bool | None, str | None]:
    source_sha = info.source_git_sha
    served_sha = info.served_git_sha
    if source_sha and served_sha:
        if source_sha == served_sha:
            return (False, None)
        return (True, "served git SHA differs from the recorded source checkout")

    if info.direct_url_editable is True:
        return (False, None)

    source_time = _parse_iso(info.source_git_commit_time)
    package_time = _parse_iso(info.package_mtime)
    if source_time is not None and package_time is not None:
        if source_time > package_time + timedelta(seconds=_STALE_CLOCK_SKEW_SECONDS):
            return (
                True,
                "source checkout HEAD is newer than the served package files",
            )
        return (False, None)
    return (None, None)


def runtime_build_info(
    *,
    package_name: str = PACKAGE_NAME,
    import_name: str = PACKAGE_IMPORT_NAME,
) -> RuntimeBuildInfo:
    """Collect runtime package/source facts without touching storage."""

    package_path, package_file = _package_location(import_name)
    package_version, dist_info_path, direct_url = _distribution_info(package_name)
    (
        direct_url_text,
        direct_url_editable,
        direct_url_vcs,
        direct_url_vcs_commit_id,
        direct_url_source,
    ) = _direct_url_fields(direct_url)

    served_git_root = _git_root(package_path)
    served_git_sha = _git_head(served_git_root) if served_git_root else None
    served_git_commit_time = (
        _git_head_time(served_git_root) if served_git_root else None
    )
    if served_git_sha is None and direct_url_vcs_commit_id:
        served_git_sha = direct_url_vcs_commit_id

    source_checkout = direct_url_source
    if source_checkout is not None:
        source_git_root = _git_root(source_checkout)
        if source_git_root is not None:
            source_checkout = source_git_root
    elif served_git_root is not None:
        source_checkout = served_git_root

    source_git_sha = _git_head(source_checkout) if source_checkout else None
    source_git_commit_time = (
        _git_head_time(source_checkout) if source_checkout else None
    )
    if source_git_sha is None and direct_url_vcs_commit_id:
        source_git_sha = direct_url_vcs_commit_id

    package_mtime, package_mtime_path = _latest_package_mtime(package_path)
    info = RuntimeBuildInfo(
        package_name=package_name,
        version=package_version,
        package_path=str(package_path) if package_path else None,
        package_file=str(package_file) if package_file else None,
        dist_info_path=str(dist_info_path) if dist_info_path else None,
        direct_url=direct_url_text,
        direct_url_editable=direct_url_editable,
        direct_url_vcs=direct_url_vcs,
        direct_url_vcs_commit_id=direct_url_vcs_commit_id,
        source_checkout=str(source_checkout) if source_checkout else None,
        source_git_sha=source_git_sha,
        source_git_commit_time=source_git_commit_time,
        served_git_sha=served_git_sha,
        served_git_commit_time=served_git_commit_time,
        package_mtime=package_mtime,
        package_mtime_path=package_mtime_path,
    )
    stale, reason = _infer_stale(info)
    return RuntimeBuildInfo(
        **{
            **info.as_dict(),
            "stale": stale,
            "stale_reason": reason,
        }
    )


@cache
def cached_runtime_build_info() -> RuntimeBuildInfo:
    """Cached variant for the public health endpoint."""

    return runtime_build_info()


def _source_package_root(source_checkout: Path, package_name: str = PACKAGE_NAME) -> Path | None:
    candidates = (
        source_checkout / "src" / package_name,
        source_checkout / package_name,
    )
    for candidate in candidates:
        if candidate.is_dir():
            return _safe_resolve(candidate)
    return None


def _rel_included(rel: str) -> bool:
    path = PurePosixPath(rel)
    if any(part in _EXCLUDED_DIRS for part in path.parts):
        return False
    if path.suffix in _EXCLUDED_SUFFIXES:
        return False
    return path.suffix in _PACKAGE_SUFFIXES


def _walk_relpaths(root: Path) -> list[str]:
    rels: list[str] = []
    try:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if _rel_included(rel):
                rels.append(rel)
    except OSError:
        return []
    return sorted(set(rels))


def _source_relpaths(source_package_root: Path, source_checkout: Path) -> list[str]:
    git_root = _git_root(source_checkout)
    if git_root is not None and _path_is_relative_to(source_package_root, git_root):
        prefix = source_package_root.relative_to(git_root).as_posix()
        rc, out = _run_git(git_root, ["ls-files", "--", prefix])
        if rc == 0 and out:
            rels: list[str] = []
            prefix_path = PurePosixPath(prefix)
            for line in out.splitlines():
                path = PurePosixPath(line.strip())
                try:
                    rel = path.relative_to(prefix_path).as_posix()
                except ValueError:
                    continue
                if rel and _rel_included(rel):
                    rels.append(rel)
            if rels:
                return sorted(set(rels))
    return _walk_relpaths(source_package_root)


def _digest_tree(root: Path, rels: list[str]) -> tuple[str, int, list[str]]:
    digest = hashlib.sha256()
    count = 0
    missing: list[str] = []
    for rel in sorted(set(rels)):
        path = root / rel
        try:
            data = path.read_bytes()
        except OSError as exc:
            missing.append(f"{rel} ({exc})")
            continue
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
        count += 1
    return (digest.hexdigest(), count, missing)


def _same_path(left: Path, right: Path) -> bool:
    return _safe_resolve(left) == _safe_resolve(right)


def assess_deploy_staleness(
    info: RuntimeBuildInfo | None = None,
    *,
    package_name: str = PACKAGE_NAME,
) -> DeployStaleness:
    """Compare the served package copy against its recorded source checkout."""

    info = info or runtime_build_info(package_name=package_name)
    data: dict[str, object] = {
        key: value for key, value in info.as_dict().items() if value is not None
    }

    if not info.package_path:
        return DeployStaleness(
            "unknown",
            "served package path not detectable",
            "Could not locate the imported PollyPM package on disk.",
            data,
        )
    package_path = _safe_resolve(Path(info.package_path))

    if not info.source_checkout:
        return DeployStaleness(
            "unknown",
            "no local source checkout recorded for this install",
            "Package metadata does not point at a local checkout to compare.",
            data,
        )
    source_checkout = _safe_resolve(Path(info.source_checkout))
    data["source_checkout"] = str(source_checkout)
    data["package_path"] = str(package_path)
    if not source_checkout.exists():
        return DeployStaleness(
            "unknown",
            f"recorded source checkout is missing: {source_checkout}",
            "The install metadata points at a local checkout that is no longer present.",
            data,
        )

    source_package_root = _source_package_root(source_checkout, package_name)
    if source_package_root is None:
        return DeployStaleness(
            "unknown",
            f"source checkout has no {package_name!r} package tree",
            "The recorded source checkout does not look like a PollyPM checkout.",
            data,
        )
    data["source_package_path"] = str(source_package_root)

    if info.direct_url_editable is True or _same_path(package_path, source_package_root):
        return DeployStaleness(
            "ok",
            "running directly from the source checkout",
            "The imported package path is the recorded source checkout.",
            data,
        )

    rels = _source_relpaths(source_package_root, source_checkout)
    if rels:
        source_digest, source_count, source_missing = _digest_tree(
            source_package_root, rels
        )
        package_digest, package_count, package_missing = _digest_tree(
            package_path, rels
        )
        data.update(
            {
                "compared_files": len(rels),
                "source_digest": source_digest[:16],
                "source_digest_files": source_count,
                "package_digest": package_digest[:16],
                "package_digest_files": package_count,
                "missing_from_source": source_missing[:20],
                "missing_from_package": package_missing[:20],
            }
        )
        if source_missing or package_missing or source_digest != package_digest:
            return DeployStaleness(
                "stale",
                "served package differs from the recorded source checkout",
                (
                    "The installed/running PollyPM package does not match the "
                    "source checkout recorded by the package install metadata."
                ),
                data,
            )
        return DeployStaleness(
            "ok",
            "served package contents match the recorded source checkout",
            "The package files match the recorded source checkout.",
            data,
        )

    if info.stale is True:
        return DeployStaleness(
            "stale",
            info.stale_reason or "served package appears older than source checkout",
            (
                "The source checkout commit timestamp is newer than the served "
                "package files."
            ),
            data,
        )
    if info.stale is False:
        return DeployStaleness(
            "ok",
            "served package timestamp is not older than source checkout",
            "The inexpensive timestamp check did not detect staleness.",
            data,
        )
    return DeployStaleness(
        "unknown",
        "source/package comparison had no comparable package files",
        "No comparable package files were found under the recorded source checkout.",
        data,
    )
