"""Read-only config endpoints (Phase 2, surface §13 of the endpoints spec).

Implements:

- ``GET /api/v1/config`` — the operator's whole :class:`PollyPMConfig`,
  serialised to a JSON-safe dict with every credential-shaped field
  redacted to ``"***"``.
- ``GET /api/v1/config/projects/{key}`` — the same redaction applied to
  a single ``[projects.<key>]`` block (the per-project config view from
  ``KnownProject``).

Per ``~/Desktop/pollypm-phase2-endpoints-spec.md`` §13 (Config,
read-only): mutation is deferred to Phase 3 — config edits should
remain TOML-first so the user can review diffs.

The handler intentionally does not import any work-service / pg-pool
plumbing. The config is already loaded by the app factory and handed
to us via :data:`ConfigDep`; we only need to flatten it into a
JSON-friendly shape and walk it once to redact secrets.
"""

from __future__ import annotations

import dataclasses
import re
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter
from pydantic import BaseModel, Field

from pollypm.web_api.errors import not_found
from pollypm.web_api.routes._deps import ConfigDep

router = APIRouter(tags=["Config"])


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


# Sentinel string the redactor substitutes for any field whose name
# matches the credential heuristic. Kept identical to the spec §13.2
# example so clients can rely on the literal string.
REDACTED = "***"


# Field-name substrings (lowercase) that mark a value as
# credential-shaped. The endpoint walks every nested dict key and
# redacts the value when any of these appear as a substring. The list
# is intentionally broader than just the documented examples
# (``auth_token``, ``api_key``, ``webhook_secret``) — anything that
# *smells* credential-shaped should not leave the process even if a
# future config field slips through review (spec §13 mission note:
# "any field that smells like a token/key/credential…").
_SECRET_NAME_SUBSTRINGS: tuple[str, ...] = (
    "token",
    "secret",
    "password",
    "passwd",
    "pwd",
    "api_key",
    "apikey",
    "credential",
    "credentials",
    "private_key",
    "auth",  # matches auth_token, basic_auth, oauth_*, etc.
    # Codex round-2 P0 on PR #2056: real env credentials whose key
    # names lack "token"/"secret"/"api_key" still need redaction.
    # ``access_key`` catches ``AWS_ACCESS_KEY_ID`` / ``*_ACCESS_KEY``;
    # ``pat`` catches ``GITHUB_PAT`` / personal access tokens; ``dsn``
    # and ``database_url`` cover DSN-style connection strings whose
    # value-shape check might miss query-only password forms.
    "access_key",
    "pat",
    "dsn",
    "database_url",
)


# Value-shape regexes for well-known credential prefixes. Catches
# environment values whose key names slipped through both
# ``_looks_secret`` and the URL-userinfo check (Codex round-2 P0 on
# PR #2056: ``AWS_ACCESS_KEY_ID = "AKIA..."``, ``GITHUB_PAT =
# "ghp_..."``). Anchored so we don't accidentally match ordinary
# strings that happen to contain "AKIA".
_CREDENTIAL_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # AWS access key IDs — always start with AKIA/ASIA + 16 base32-ish chars.
    re.compile(r"^(?:AKIA|ASIA)[0-9A-Z]{16}$"),
    # GitHub personal / OAuth / user / server / refresh tokens. The
    # ``_`` between prefix and body is part of the format; bodies are
    # 36+ base64url chars in practice (current GitHub format).
    re.compile(r"^gh[pousr]_[A-Za-z0-9_]{36,}$"),
    # GitHub fine-grained PATs.
    re.compile(r"^github_pat_[A-Za-z0-9_]{20,}$"),
    # Slack bot/user/app tokens.
    re.compile(r"^xox[abprs]-[A-Za-z0-9-]{10,}$"),
)


# Query-string parameter names (lowercase) whose values mark a URL as
# carrying credentials outside the userinfo netloc. Catches DSN forms
# like ``postgresql://host/db?user=alice&password=secret`` and libpq
# variants that put auth in the query string. Codex round-2 P0 on
# PR #2056 called out password-in-query explicitly.
_URL_QUERY_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "pwd",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "auth",
        "access_token",
        "auth_token",
    },
)


def _looks_secret(field_name: str) -> bool:
    """Return True iff ``field_name`` matches the credential heuristic.

    Case-insensitive substring match against
    :data:`_SECRET_NAME_SUBSTRINGS`. The match is deliberately
    over-eager — false positives (redacting a non-secret field whose
    name happens to contain ``"auth"``) are operationally fine; false
    negatives (leaking a real secret) are not.
    """
    lower = field_name.lower()
    return any(needle in lower for needle in _SECRET_NAME_SUBSTRINGS)


def _value_has_url_userinfo(value: Any) -> bool:
    """Return True iff ``value`` parses as a URL carrying credentials.

    Catches credential-bearing connection strings whose *keys* don't
    match the secret-name heuristic — e.g. ``[storage].url``,
    ``[storage.pg].dsn``, or env entries like ``DATABASE_URL`` /
    ``POSTGRES_DSN`` / ``SENTRY_DSN`` shaped as
    ``scheme://user:password@host/...`` **or**
    ``scheme://host/db?user=...&password=...`` (Codex round-2 P0 on
    PR #2056 — query-string passwords leaked through the prior
    userinfo-only check). Uses :func:`urllib.parse.urlsplit` so the
    check is robust against odd schemes (``postgresql+psycopg``,
    ``redis``, ``amqps``, ...).

    Only flags strings; non-string values fall through. We treat the
    whole value as tainted (rather than masking just the userinfo
    portion) so we never accidentally leave fragments of the password
    in the response.
    """
    if not isinstance(value, str) or not value:
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        # Malformed URL — better to redact below if it *looked* like a
        # URL, but ``urlsplit`` is permissive, so a raise here means
        # the value definitely isn't a URL.
        return False
    if not parts.scheme:
        return False
    try:
        if parts.username or parts.password:
            return True
    except ValueError:
        # ``parts.username`` / ``.password`` can raise on malformed
        # percent-encoding in the netloc. If the netloc contains an
        # ``@``, that's still very likely userinfo — redact to be safe.
        if "@" in (parts.netloc or ""):
            return True
    # Query-string passwords (libpq-style DSN auth, ``?api_key=...``,
    # ``?token=...``). Some DSN dialects carry credentials only in the
    # query, so userinfo-only redaction misses them.
    if parts.query:
        try:
            params = parse_qs(parts.query, keep_blank_values=True)
        except ValueError:
            return False
        for raw_key in params:
            if raw_key.lower() in _URL_QUERY_SECRET_KEYS:
                return True
    return False


def _value_looks_credential(value: Any) -> bool:
    """Return True iff ``value`` matches a known credential value-shape.

    Catches env entries whose key names slipped through ``_looks_secret``
    (Codex round-2 P0 on PR #2056: ``AWS_ACCESS_KEY_ID = "AKIA..."``,
    ``GITHUB_PAT = "ghp_..."``). The regex set is conservative — anchored
    prefixes for vendor-specific token formats — so false positives on
    ordinary strings are unlikely.
    """
    if not isinstance(value, str) or not value:
        return False
    return any(pattern.match(value) for pattern in _CREDENTIAL_VALUE_PATTERNS)


def _coerce_value(value: Any) -> Any:
    """Coerce a single config value into a JSON-friendly shape.

    Handles the non-JSON-native types the config dataclasses use:

    - :class:`pathlib.Path` → ``str``
    - :class:`enum.Enum` (incl. :class:`enum.StrEnum`) → ``.value``
    - ``tuple`` → ``list``
    - dataclasses → ``dataclasses.asdict`` then recursed
    - dicts / lists → recursed element-wise
    - everything else returned as-is (FastAPI's JSON encoder handles
      ``datetime`` / ``bytes`` / etc. if any sneak in later)
    """
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _redact_dict(dataclasses.asdict(value))
    if isinstance(value, dict):
        return _redact_dict(value)
    if isinstance(value, (list, tuple)):
        return [_coerce_value(item) for item in value]
    # Value-shape redaction: any string that parses as a URL carrying
    # credentials (userinfo or password-in-query) is treated as a live
    # credential regardless of the key name it sits under. Catches
    # ``[storage].url`` / ``[storage.pg].dsn`` and env entries like
    # ``DATABASE_URL`` whose keys don't trigger ``_looks_secret``.
    if _value_has_url_userinfo(value):
        return REDACTED
    # Vendor-specific credential prefixes (AWS access keys, GitHub
    # PATs, Slack tokens, …). Backstops env entries whose key names
    # don't include any credential keyword (Codex round-2 P0 on PR #2056).
    if _value_looks_credential(value):
        return REDACTED
    return value


def _redact_dict(data: dict[Any, Any]) -> dict[str, Any]:
    """Walk ``data`` and redact every credential-shaped key.

    A key is redacted when its name matches :func:`_looks_secret`. The
    value is replaced with :data:`REDACTED` **only when the original
    value is truthy** — empty strings / ``None`` stay as-is so the
    operator can tell "field unset" from "field set, value hidden"
    (legacy sessions intentionally hold ``auth_token=""`` per the
    :class:`SessionConfig` docstring; redacting that to ``"***"`` would
    misrepresent the on-disk state).

    Nested dicts and dataclasses recurse through :func:`_coerce_value`,
    so the redaction applies at every depth.
    """
    out: dict[str, Any] = {}
    for raw_key, value in data.items():
        key = str(raw_key)
        if _looks_secret(key) and value not in (None, "", b""):
            out[key] = REDACTED
            continue
        out[key] = _coerce_value(value)
    return out


def _serialise_config(config: Any) -> dict[str, Any]:
    """Return the full :class:`PollyPMConfig` as a redacted dict.

    ``dataclasses.asdict`` recursively unpacks every nested dataclass
    into plain dicts; we then walk the result via :func:`_redact_dict`
    to (a) coerce :class:`Path` / :class:`Enum` values and (b) replace
    credential-shaped fields with :data:`REDACTED`.
    """
    raw = dataclasses.asdict(config)
    return _redact_dict(raw)


def _serialise_project(project: Any) -> dict[str, Any]:
    """Return a single ``KnownProject`` block as a redacted dict.

    Same shape as the per-project entries in ``GET /config``, but
    returned standalone for ``GET /config/projects/{key}``.
    """
    raw = dataclasses.asdict(project)
    return _redact_dict(raw)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ConfigView(BaseModel):
    """``GET /api/v1/config`` envelope.

    ``config`` is the flattened :class:`PollyPMConfig` with secrets
    redacted to ``"***"``. The shape is open-ended (the underlying
    dataclass grows over time) so we declare it as ``dict[str, Any]``
    rather than reflecting every nested field into a Pydantic model;
    the redaction guarantee is implemented by the route, not the
    schema.
    """

    config: dict[str, Any] = Field(
        description=(
            "Redacted snapshot of the loaded PollyPMConfig. "
            "Credential-shaped fields (auth_token, api_key, "
            "webhook_secret, …) are replaced with '***'."
        ),
    )


class ProjectConfigView(BaseModel):
    """``GET /api/v1/config/projects/{key}`` envelope.

    ``project`` mirrors one ``[projects.<key>]`` TOML block as a
    flat dict (the :class:`KnownProject` dataclass). Path-typed fields
    are coerced to strings; enum fields to their ``.value``. Credentials
    are redacted by the same heuristic the full-config endpoint uses.
    """

    key: str
    project: dict[str, Any] = Field(
        description=(
            "Redacted snapshot of the [projects.<key>] config block."
        ),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/config",
    response_model=ConfigView,
    summary="Read the operator config (secrets redacted)",
    operation_id="getConfig",
)
def get_config_endpoint(config: ConfigDep) -> ConfigView:
    """GET /api/v1/config — full config with secrets redacted.

    Implementation note (spec §13.2): the dependency provider
    re-invokes :func:`pollypm.config.load_config` on every request so a
    TOML edit between requests shows up on the next call. The loader
    has an mtime-keyed cache (see ``_config_cache``), so unchanged
    files are O(stat) — no extra parse work per request. Codex round-2
    P0 on PR #2056 fixed the previous stale-snapshot behaviour where
    ``GET /config`` reflected only what was loaded at ``pm serve``
    startup.
    """
    return ConfigView(config=_serialise_config(config))


@router.get(
    "/config/projects/{key}",
    response_model=ProjectConfigView,
    summary="Read one project's config block (secrets redacted)",
    operation_id="getProjectConfig",
)
def get_project_config_endpoint(key: str, config: ConfigDep) -> ProjectConfigView:
    """GET /api/v1/config/projects/{key} — single-project view.

    Returns 404 ``not_found`` when ``key`` isn't registered in
    ``config.projects``. Redaction matches the full-config endpoint
    (the same recursive walker handles both shapes).
    """
    project = config.projects.get(key) if config.projects else None
    if project is None:
        raise not_found(
            f"Project not registered: {key}",
            hint=(
                "Use GET /api/v1/config to list every configured "
                "project key, or check the [projects.*] sections in "
                "your pollypm.toml."
            ),
        )
    return ProjectConfigView(key=key, project=_serialise_project(project))


__all__ = [
    "ConfigView",
    "ProjectConfigView",
    "REDACTED",
    "get_config_endpoint",
    "get_project_config_endpoint",
    "router",
]
