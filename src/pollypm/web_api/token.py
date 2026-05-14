"""Bearer-token storage + rotation for the Web API.

Per `docs/web-api-spec.md` §3:

- Token lives at ``~/.pollypm/api-token`` with mode ``0600``.
- Generated on first ``pm serve`` startup if absent.
- 256-bit random, base64url-encoded (~43 chars).
- Regenerated via ``pm api regen-token`` (rotates the token, prints the
  new value once, invalidates all prior tokens).

The file is just the token bytes (no JSON wrapper) so a frontend
developer can ``cat`` it directly. We expose ``load_token`` /
``ensure_token`` / ``regenerate_token`` so the FastAPI app, the
``pm serve`` command, and ``pm api regen-token`` all share one
storage layer.
"""

from __future__ import annotations

import logging
import os
import secrets
import stat
from pathlib import Path

logger = logging.getLogger(__name__)


def _config_home() -> Path:
    """Return the PollyPM config directory (typically ``~/.pollypm``).

    Mirrors :data:`pollypm.config.DEFAULT_CONFIG_PATH`'s parent so a
    custom ``$POLLYPM_HOME`` (set via ``DEFAULT_CONFIG_PATH``) wins.
    Falling back to ``~/.pollypm`` keeps the function safe to import
    even when config loading itself is broken (e.g. corrupt TOML).
    """
    try:
        from pollypm.config import DEFAULT_CONFIG_PATH

        return Path(DEFAULT_CONFIG_PATH).parent
    except Exception:  # noqa: BLE001 — never block token ops on config import
        return Path.home() / ".pollypm"


# Module-level constant — useful for tests that want to assert paths.
DEFAULT_TOKEN_PATH = _config_home() / "api-token"


def _resolve_token_path(path: Path | None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    # Re-resolve every call so monkeypatching ``DEFAULT_CONFIG_PATH``
    # in tests is honored without requiring the test to also import
    # this module afresh.
    return _config_home() / "api-token"


def _new_token_bytes() -> str:
    """Generate a 256-bit random token, base64url-encoded.

    ``token_urlsafe(32)`` returns 32 random bytes (= 256 bits)
    encoded base64url, ~43 characters with no padding. That matches
    the spec's "256-bit random, base64url-encoded (≈43 chars)".
    """
    return secrets.token_urlsafe(32)


def _write_token_file(path: Path, token: str) -> None:
    """Write ``token`` to ``path`` with mode 0600.

    Best-effort: parents are created, the file is written atomically
    (write to ``<path>.tmp`` then rename), and the mode is enforced
    after the write. We don't surface chmod failures because some
    filesystems (Windows / network shares) reject the call but the
    write itself is fine.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(token)
    os.replace(tmp, path)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:  # noqa: BLE001
        logger.warning("api-token: could not set mode 0600 on %s: %s", path, exc)


def load_token(path: Path | None = None) -> str | None:
    """Read the token from disk, or return ``None`` if absent.

    Returning ``None`` (rather than raising) lets the FastAPI auth
    dependency emit the spec's standard 401 response with
    ``code=unauthorized`` instead of leaking a filesystem error.
    """
    resolved = _resolve_token_path(path)
    if not resolved.exists():
        return None
    try:
        text = resolved.read_text(encoding="utf-8").strip()
    except OSError as exc:  # noqa: BLE001
        logger.warning("api-token: could not read %s: %s", resolved, exc)
        return None
    return text or None


def ensure_token(path: Path | None = None) -> tuple[str, bool]:
    """Return ``(token, generated)``; create one when missing.

    ``generated`` is True iff this call wrote a new token. ``pm serve``
    uses that flag to print the value to stderr exactly once on
    first launch.
    """
    resolved = _resolve_token_path(path)
    existing = load_token(resolved)
    if existing:
        return existing, False
    token = _new_token_bytes()
    _write_token_file(resolved, token)
    return token, True


def regenerate_token(path: Path | None = None) -> str:
    """Rotate the token, returning the new value.

    Old tokens are invalidated atomically — the on-disk file holds
    exactly one valid token at any time, and ``load_token`` reads it
    fresh on every auth check.
    """
    resolved = _resolve_token_path(path)
    token = _new_token_bytes()
    _write_token_file(resolved, token)
    return token


__all__ = [
    "DEFAULT_TOKEN_PATH",
    "ensure_token",
    "load_token",
    "regenerate_token",
]
