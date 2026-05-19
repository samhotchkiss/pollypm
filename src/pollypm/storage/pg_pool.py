"""Process-wide Postgres connection pool primitives (issue #1737).

This module owns the lazy, process-wide :class:`psycopg_pool.ConnectionPool`
instances that the rest of PollyPM uses to talk to Postgres. Two pools are
kept side-by-side so accidental writes from read-side facades crash loudly:

* :func:`get_rw_pool` — the canonical read-write pool. Every mutation goes
  through this one.
* :func:`get_ro_pool` — a parallel pool that sets
  ``default_transaction_read_only = on`` at session level. Read-only facades
  (the storage-layer query helpers, doctor probes, recall) use this so a
  rogue ``INSERT``/``UPDATE`` fails with ``cannot execute … in a read-only
  transaction`` instead of silently mutating shared state.

DSN resolution order (highest priority wins):

1. ``POLLYPM_PG_DSN`` env var — operator override / one-shot test runs.
2. ``[storage] url`` in ``pollypm.toml`` — only when it looks like a pg DSN
   (``postgresql://...`` / ``postgres://...``); otherwise treated as a
   sqlite URL and ignored here.
3. The built-in default ``postgresql://localhost:5432/pollypm``.

Note: :class:`~pollypm.models.PgStorageSettings` exposes a ``dsn`` field
that :mod:`pollypm.config` parses from ``[storage.pg] dsn``, but
:func:`resolve_dsn` does **not** read it yet — operators who want to
override the DSN should use ``POLLYPM_PG_DSN`` or ``[storage] url``.
Wiring ``[storage.pg].dsn`` is tracked as a post-RC follow-up.

The pools are lazy module-level singletons. :func:`pg_pool_shutdown` is the
graceful close used by ``pm reset`` / test teardown — calling it is safe
even when no pool was ever opened.

The consumers (PgWorkService, the storage facades, the doctor check) all
wire into this module; the unit tests cover the primitive contract.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

    from pollypm.models import PollyPMConfig


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------- #

DEFAULT_DSN = "postgresql://localhost:5432/pollypm"
ENV_DSN = "POLLYPM_PG_DSN"

# Pool sizing defaults. The cockpit + rail process is the heaviest caller
# and runs many concurrent read-side facades, but Sam's solo-operator
# workload doesn't need a huge pool. min_size=1 keeps idle resource use
# tiny on first-run installs; max_size=10 leaves headroom for the burst
# of cockpit refreshes that motivated the migration. Operators tune via
# ``[storage.pg] pool_min`` / ``[storage.pg] pool_max``.
DEFAULT_POOL_MIN = 1
DEFAULT_POOL_MAX = 10


# --------------------------------------------------------------------- #
# Module-level singletons
# --------------------------------------------------------------------- #

# Both pools are created lazily on first access. The lock guards the
# creation race only — calls into the pool itself are pool-safe.
_RW_POOL: "ConnectionPool | None" = None
_RO_POOL: "ConnectionPool | None" = None
_POOL_LOCK = threading.Lock()


# --------------------------------------------------------------------- #
# Configuration resolution
# --------------------------------------------------------------------- #


def _looks_like_pg_dsn(value: str) -> bool:
    """Heuristic: does ``value`` smell like a Postgres DSN?

    The shared ``[storage] url`` knob currently carries a sqlite URL
    (``sqlite:///...``) for the existing backend. Until the cutover
    flips the default backend, we don't want the pg pool to try and
    open a sqlite URL — so we filter on the scheme. Empty strings,
    ``None``, and non-pg schemes all return False.
    """
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped:
        return False
    lower = stripped.lower()
    return lower.startswith(("postgresql://", "postgres://", "postgresql+"))


def resolve_dsn(config: "PollyPMConfig | None" = None) -> str:
    """Return the DSN to use, honouring the documented priority order.

    Priority (highest wins):

    1. ``POLLYPM_PG_DSN`` env var.
    2. ``config.storage.url`` — only when it parses as a pg DSN.
    3. The built-in :data:`DEFAULT_DSN`.

    Note: ``config.storage.pg.dsn`` is parsed by :mod:`pollypm.config`
    but **not** read here. Operators who set ``[storage.pg] dsn`` in
    ``pollypm.toml`` are currently silently ignored; wiring that
    source is tracked as a post-RC follow-up.

    Parameters
    ----------
    config:
        Optional :class:`pollypm.models.PollyPMConfig`. When provided,
        ``config.storage.url`` is consulted as the second-priority
        source. Pass ``None`` to resolve purely from env + the built-in
        default (used by ``pm doctor`` before a config is loaded, and
        by the tests).

    Returns
    -------
    str
        The resolved DSN. Never empty — callers can pass the result
        straight into ``psycopg.connect`` or :class:`ConnectionPool`.
    """
    env_dsn = os.environ.get(ENV_DSN, "").strip()
    if env_dsn:
        return env_dsn

    if config is not None:
        url = getattr(config.storage, "url", "") or ""
        if _looks_like_pg_dsn(url):
            return url.strip()

    return DEFAULT_DSN


def resolve_pool_sizing(
    config: "PollyPMConfig | None" = None,
) -> tuple[int, int]:
    """Return ``(min_size, max_size)`` for the pool, honouring config.

    Reads ``[storage.pg] pool_min`` and ``[storage.pg] pool_max`` from
    the loaded config when they're present and well-typed; falls back
    to :data:`DEFAULT_POOL_MIN` / :data:`DEFAULT_POOL_MAX` otherwise.

    The values come from :class:`~pollypm.models.PgStorageSettings`,
    which :class:`~pollypm.models.StorageSettings` exposes as the
    ``pg`` attribute. The ``getattr`` lookups below tolerate older
    configs or test fixtures that synthesise a bare ``StorageSettings``
    without the subsection — those callers transparently fall through
    to the module defaults.
    """
    if config is None:
        return DEFAULT_POOL_MIN, DEFAULT_POOL_MAX

    pg_section = getattr(config.storage, "pg", None)
    if pg_section is None:
        return DEFAULT_POOL_MIN, DEFAULT_POOL_MAX

    raw_min = getattr(pg_section, "pool_min", DEFAULT_POOL_MIN)
    raw_max = getattr(pg_section, "pool_max", DEFAULT_POOL_MAX)
    try:
        min_size = max(1, int(raw_min))
    except (TypeError, ValueError):
        min_size = DEFAULT_POOL_MIN
    try:
        max_size = max(min_size, int(raw_max))
    except (TypeError, ValueError):
        max_size = DEFAULT_POOL_MAX

    return min_size, max_size


# --------------------------------------------------------------------- #
# Pool construction
# --------------------------------------------------------------------- #


def _import_pool():
    """Late-import :class:`ConnectionPool` so the module imports clean.

    ``psycopg`` / ``psycopg_pool`` are runtime deps for the pg backend
    only; importing them at module top would force every CLI invocation
    (sqlite-backed installs included) to pay the import cost and would
    leak ``ImportError`` into doctor checks that report missing-driver
    states with their own friendly errors. Localising the import keeps
    the storage/pg_pool import surface free of side effects on sqlite
    installs.
    """
    try:
        from psycopg_pool import ConnectionPool
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "psycopg_pool is not installed. The postgres backend "
            "requires `psycopg[binary]` and `psycopg_pool`; install "
            "them via `uv pip install 'psycopg[binary]' psycopg_pool` "
            "or switch `[storage] backend` back to `sqlite`."
        ) from exc
    return ConnectionPool


def _build_pool(
    dsn: str,
    *,
    min_size: int,
    max_size: int,
    read_only: bool,
    application_name: str,
) -> "ConnectionPool":
    """Construct a :class:`ConnectionPool` with the documented defaults.

    ``read_only=True`` installs a session-level configure callback
    that runs ``SET default_transaction_read_only = on`` plus
    ``SET application_name = ...`` whenever ``psycopg_pool`` adopts a
    fresh connection. The RW pool only sets ``application_name`` so
    ``pg_stat_activity`` is readable but writes are still allowed.
    """
    ConnectionPool = _import_pool()

    # SQL ``SET`` does not accept parameter placeholders, so we sanitize
    # the application_name to a tight allow-listed shape and inline it.
    # The fixed-shape sanitizer below rejects anything outside
    # ``[A-Za-z0-9._/-]`` so the inlined value cannot smuggle SQL.
    safe_application_name = _sanitize_application_name(application_name)

    def _configure(conn) -> None:
        # Stamp application_name so an operator running ``SELECT *
        # FROM pg_stat_activity`` can tell which PollyPM process owns
        # which connection. Cheap and removes a real debugging head-
        # scratcher when multiple PollyPM processes share one DB.
        with conn.cursor() as cur:
            cur.execute(f"SET application_name = '{safe_application_name}'")
            if read_only:
                cur.execute("SET default_transaction_read_only = on")
        conn.commit()

    return ConnectionPool(
        conninfo=dsn,
        min_size=min_size,
        max_size=max_size,
        configure=_configure,
        open=True,
        # The ``name`` ends up in psycopg_pool's debug logs — easier
        # than reading object reprs when both pools are in flight.
        name=f"pollypm-{'ro' if read_only else 'rw'}",
    )


# --------------------------------------------------------------------- #
# Public accessors
# --------------------------------------------------------------------- #


def get_rw_pool(config: "PollyPMConfig | None" = None) -> "ConnectionPool":
    """Return the process-wide read-write Postgres pool.

    Lazy: first call constructs the pool; subsequent calls return the
    cached instance. The pool is **not** keyed by DSN — at runtime
    there's exactly one Postgres backing PollyPM. Tests that need a
    different DSN should call :func:`pg_pool_shutdown` between cases
    to clear the singleton.
    """
    global _RW_POOL
    if _RW_POOL is not None:
        return _RW_POOL
    with _POOL_LOCK:
        if _RW_POOL is not None:
            return _RW_POOL
        dsn = resolve_dsn(config)
        min_size, max_size = resolve_pool_sizing(config)
        logger.info(
            "pg_pool: opening rw pool dsn=%s min=%d max=%d",
            _safe_dsn(dsn),
            min_size,
            max_size,
        )
        _RW_POOL = _build_pool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            read_only=False,
            application_name="pollypm/rw",
        )
        return _RW_POOL


def get_ro_pool(config: "PollyPMConfig | None" = None) -> "ConnectionPool":
    """Return the process-wide read-only Postgres pool.

    Each session run on this pool runs ``SET
    default_transaction_read_only = on``, so a mistaken write from a
    read-side facade fails fast with ``cannot execute … in a read-only
    transaction`` instead of corrupting state.
    """
    global _RO_POOL
    if _RO_POOL is not None:
        return _RO_POOL
    with _POOL_LOCK:
        if _RO_POOL is not None:
            return _RO_POOL
        dsn = resolve_dsn(config)
        min_size, max_size = resolve_pool_sizing(config)
        logger.info(
            "pg_pool: opening ro pool dsn=%s min=%d max=%d",
            _safe_dsn(dsn),
            min_size,
            max_size,
        )
        _RO_POOL = _build_pool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            read_only=True,
            application_name="pollypm/ro",
        )
        return _RO_POOL


def pg_pool_shutdown() -> None:
    """Close both pools and clear the singletons. Idempotent.

    Used by ``pm reset`` and the test fixtures. Safe to call when no
    pool was ever opened (no-op). Calling :func:`get_rw_pool` /
    :func:`get_ro_pool` after shutdown re-opens fresh instances —
    that's the intended "between test cases" lifecycle.
    """
    global _RW_POOL, _RO_POOL
    with _POOL_LOCK:
        pools = [p for p in (_RW_POOL, _RO_POOL) if p is not None]
        _RW_POOL = None
        _RO_POOL = None
    for pool in pools:
        try:
            pool.close()
        except Exception:  # noqa: BLE001 — shutdown must never raise
            logger.warning("pg_pool: pool close failed", exc_info=True)


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


_APP_NAME_SAFE_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-"
)


def _sanitize_application_name(value: str) -> str:
    """Reduce ``value`` to the alphabet pg's SET command can take inline.

    SQL ``SET application_name`` doesn't accept parameter placeholders,
    so the value is inlined into the configure-time DDL. The sanitizer
    drops anything outside the allow-listed shape used by PollyPM's
    own naming (``pollypm/rw``, ``pollypm-test-rw``). On an empty
    result it returns ``"pollypm"`` so pg_stat_activity always shows
    something useful.
    """
    cleaned = "".join(c for c in value if c in _APP_NAME_SAFE_CHARS)
    return cleaned or "pollypm"


def _safe_dsn(dsn: str) -> str:
    """Return ``dsn`` with the password redacted for log output.

    psycopg DSNs may include ``postgresql://user:password@host/db``.
    The naive log line would leak the password to disk — which is
    embarrassing even for local-only installs. Redaction is best-
    effort and falls back to "***" when the URL doesn't parse.
    """
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(dsn)
        if not parts.password:
            return dsn
        netloc = parts.netloc
        # urlsplit gives us userinfo intact; redact the password slice
        # without losing the username.
        userinfo, _, hostinfo = netloc.rpartition("@")
        user = userinfo.partition(":")[0]
        new_netloc = f"{user}:***@{hostinfo}" if user else f":***@{hostinfo}"
        return urlunsplit(parts._replace(netloc=new_netloc))
    except Exception:  # noqa: BLE001 — never break a log line
        return "***"


__all__ = [
    "DEFAULT_DSN",
    "DEFAULT_POOL_MAX",
    "DEFAULT_POOL_MIN",
    "ENV_DSN",
    "get_ro_pool",
    "get_rw_pool",
    "pg_pool_shutdown",
    "resolve_dsn",
    "resolve_pool_sizing",
]
