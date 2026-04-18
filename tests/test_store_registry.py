"""Tests for :mod:`pollypm.store.registry` — entry-point backend resolver.

Run with an isolated HOME so the suite never leaks into ``~/.pollypm/``:

    HOME=/tmp/pytest-store-registry uv run pytest tests/test_store_registry.py -x

Coverage (per issue #343 acceptance):

1. :func:`get_store` returns a real :class:`SQLAlchemyStore` for the
   default ``backend='sqlite'`` + derived URL.
2. A test-only entry point (injected via monkeypatch) loads and gets
   passed the resolved URL.
3. An unknown backend raises :class:`StoreBackendNotFound` whose
   message includes the requested name and the available backends.
4. Config round-trip: missing ``[storage]`` applies defaults, a present
   block parses ``backend`` / ``url``, and an empty URL auto-derives
   ``sqlite:///<state_db>``.
5. The ``pollypm.store_backend`` entry-point group is actually
   populated by PollyPM's own ``pyproject.toml`` — sanity check that
   the install registered the default.
"""

from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from pollypm.config import _parse_storage_settings, load_config
from pollypm.errors import StoreBackendNotFound
from pollypm.models import (
    PollyPMConfig,
    PollyPMSettings,
    ProjectSettings,
    StorageSettings,
)
from pollypm.store import SQLAlchemyStore
from pollypm.store.registry import (
    ENTRY_POINT_GROUP,
    _resolve_url,
    get_store,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _make_config(
    tmp_path: Path,
    *,
    backend: str = "sqlite",
    url: str = "",
) -> PollyPMConfig:
    state_db = tmp_path / "state.db"
    project = ProjectSettings(
        name="Test",
        root_dir=tmp_path,
        state_db=state_db,
        base_dir=tmp_path,
        logs_dir=tmp_path / "logs",
        snapshots_dir=tmp_path / "snapshots",
    )
    return PollyPMConfig(
        project=project,
        pollypm=PollyPMSettings(controller_account=""),
        accounts={},
        sessions={},
        storage=StorageSettings(backend=backend, url=url),
    )


@dataclass
class _FakeEntryPoint:
    """Minimal duck-type stand-in for :class:`importlib.metadata.EntryPoint`.

    The real ``EntryPoint`` can't be constructed freely in all Python
    versions; the registry only touches ``.name`` and ``.load()`` so
    this is sufficient for the monkeypatch.
    """

    name: str
    target: Any

    def load(self) -> Any:
        return self.target


# --------------------------------------------------------------------------
# 1. Default backend round-trip.
# --------------------------------------------------------------------------


def test_get_store_returns_sqlalchemy_store_by_default(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    store = get_store(config)
    try:
        assert isinstance(store, SQLAlchemyStore)
        # URL auto-derives when config.storage.url is empty.
        assert store.url == f"sqlite:///{(tmp_path / 'state.db').resolve()}"
    finally:
        store.dispose()


def test_get_store_honours_explicit_url(tmp_path: Path) -> None:
    explicit = f"sqlite:///{tmp_path / 'custom.db'}"
    config = _make_config(tmp_path, url=explicit)
    store = get_store(config)
    try:
        assert store.url == explicit
    finally:
        store.dispose()


# --------------------------------------------------------------------------
# 2. Fake entry-point injection.
# --------------------------------------------------------------------------


class _FakeBackend:
    """Non-functional backend used to prove the registry loads arbitrary EPs."""

    constructed_with: list[str] = []

    def __init__(self, url: str) -> None:
        self._url = url
        _FakeBackend.constructed_with.append(url)

    @property
    def url(self) -> str:
        return self._url

    def dispose(self) -> None:
        return None


def _patch_entry_points(
    monkeypatch: pytest.MonkeyPatch,
    entries: list[_FakeEntryPoint],
) -> None:
    """Force :mod:`importlib.metadata` to return ``entries`` for our group."""
    real = importlib.metadata.entry_points

    def _fake(*args: Any, **kwargs: Any) -> Any:
        group = kwargs.get("group")
        if group == ENTRY_POINT_GROUP:
            return entries
        return real(*args, **kwargs)

    monkeypatch.setattr(importlib.metadata, "entry_points", _fake)


def test_get_store_loads_registered_fake_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeBackend.constructed_with = []
    fake_ep = _FakeEntryPoint(name="fake", target=_FakeBackend)
    _patch_entry_points(monkeypatch, [fake_ep])

    config = _make_config(tmp_path, backend="fake", url="postgresql://x/y")
    store = get_store(config)
    assert isinstance(store, _FakeBackend)
    # URL must be passed through to the backend constructor.
    assert _FakeBackend.constructed_with == ["postgresql://x/y"]
    assert store.url == "postgresql://x/y"


def test_get_store_passes_derived_url_to_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty URL → resolver derives, backend receives the derivation."""
    _FakeBackend.constructed_with = []
    _patch_entry_points(
        monkeypatch,
        [_FakeEntryPoint(name="fake", target=_FakeBackend)],
    )
    config = _make_config(tmp_path, backend="fake", url="")
    store = get_store(config)
    expected = f"sqlite:///{(tmp_path / 'state.db').resolve()}"
    assert store.url == expected
    assert _FakeBackend.constructed_with == [expected]


# --------------------------------------------------------------------------
# 3. Unknown backend error.
# --------------------------------------------------------------------------


def test_get_store_raises_when_backend_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Install two fake backends so the "available" list is non-trivial.
    _patch_entry_points(
        monkeypatch,
        [
            _FakeEntryPoint(name="alpha", target=_FakeBackend),
            _FakeEntryPoint(name="beta", target=_FakeBackend),
        ],
    )
    config = _make_config(tmp_path, backend="gamma")
    with pytest.raises(StoreBackendNotFound) as excinfo:
        get_store(config)
    err = excinfo.value
    assert err.backend == "gamma"
    # _available_backends sorts its output; validate membership rather
    # than exact equality so future entry points added by tests don't
    # break this assertion.
    assert "alpha" in err.available
    assert "beta" in err.available
    msg = str(err)
    # Three-question rule: the message names what, why, and how to fix.
    assert "gamma" in msg
    assert "alpha" in msg and "beta" in msg
    assert "Fix:" in msg


def test_store_backend_not_found_handles_empty_available() -> None:
    """Message stays readable when no backends are installed at all."""
    err = StoreBackendNotFound("nothing", available=[])
    msg = str(err)
    assert "nothing" in msg
    assert "Available backends: none" in msg
    assert "Fix:" in msg


# --------------------------------------------------------------------------
# 4. Config round-trip for [storage].
# --------------------------------------------------------------------------


def test_storage_settings_defaults_when_section_missing(tmp_path: Path) -> None:
    project = ProjectSettings(
        name="Test",
        root_dir=tmp_path,
        state_db=tmp_path / "state.db",
        base_dir=tmp_path,
        logs_dir=tmp_path / "logs",
        snapshots_dir=tmp_path / "snapshots",
    )
    storage = _parse_storage_settings({}, project=project)
    assert storage.backend == "sqlite"
    # Default-derive from the resolved state_db path.
    assert storage.url == f"sqlite:///{(tmp_path / 'state.db').resolve()}"


def test_storage_settings_parses_backend_and_url(tmp_path: Path) -> None:
    project = ProjectSettings(
        name="Test",
        root_dir=tmp_path,
        state_db=tmp_path / "state.db",
        base_dir=tmp_path,
        logs_dir=tmp_path / "logs",
        snapshots_dir=tmp_path / "snapshots",
    )
    storage = _parse_storage_settings(
        {"storage": {"backend": "postgres", "url": "postgresql://u@h/db"}},
        project=project,
    )
    assert storage.backend == "postgres"
    assert storage.url == "postgresql://u@h/db"


def test_storage_settings_empty_url_derives_from_state_db(tmp_path: Path) -> None:
    project = ProjectSettings(
        name="Test",
        root_dir=tmp_path,
        state_db=tmp_path / "custom.db",
        base_dir=tmp_path,
        logs_dir=tmp_path / "logs",
        snapshots_dir=tmp_path / "snapshots",
    )
    storage = _parse_storage_settings(
        {"storage": {"backend": "sqlite", "url": ""}},
        project=project,
    )
    assert storage.url == f"sqlite:///{(tmp_path / 'custom.db').resolve()}"


def test_storage_settings_bogus_types_fall_back_to_defaults(tmp_path: Path) -> None:
    project = ProjectSettings(
        name="Test",
        root_dir=tmp_path,
        state_db=tmp_path / "state.db",
        base_dir=tmp_path,
        logs_dir=tmp_path / "logs",
        snapshots_dir=tmp_path / "snapshots",
    )
    # A non-dict [storage] section → defaults.
    assert _parse_storage_settings({"storage": "nope"}, project=project).backend == "sqlite"
    # Non-string backend → falls back.
    storage = _parse_storage_settings(
        {"storage": {"backend": 42, "url": None}},
        project=project,
    )
    assert storage.backend == "sqlite"
    assert storage.url.startswith("sqlite:///")


def test_load_config_populates_storage_from_toml(tmp_path: Path) -> None:
    """Full config round-trip: TOML → load_config → config.storage."""
    config_dir = tmp_path / "home"
    config_dir.mkdir()
    config_path = config_dir / "pollypm.toml"
    state_db = config_dir / ".pollypm-state" / "state.db"
    config_path.write_text(
        "[project]\n"
        'name = "Test"\n'
        f'base_dir = "{config_dir}"\n'
        f'logs_dir = "{config_dir}/logs"\n'
        f'snapshots_dir = "{config_dir}/snapshots"\n'
        f'state_db = "{state_db}"\n'
        "\n"
        "[storage]\n"
        'backend = "sqlite"\n'
        'url = "sqlite:///tmp/override.db"\n'
    )
    config = load_config(config_path)
    assert config.storage.backend == "sqlite"
    assert config.storage.url == "sqlite:///tmp/override.db"


def test_load_config_applies_storage_defaults_when_section_missing(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "home"
    config_dir.mkdir()
    config_path = config_dir / "pollypm.toml"
    state_db = config_dir / ".pollypm-state" / "state.db"
    config_path.write_text(
        "[project]\n"
        'name = "Test"\n'
        f'base_dir = "{config_dir}"\n'
        f'logs_dir = "{config_dir}/logs"\n'
        f'snapshots_dir = "{config_dir}/snapshots"\n'
        f'state_db = "{state_db}"\n'
    )
    config = load_config(config_path)
    assert config.storage.backend == "sqlite"
    # Resolver derives from state_db.
    assert config.storage.url == f"sqlite:///{state_db.resolve()}"


# --------------------------------------------------------------------------
# 5. Installed entry-point sanity.
# --------------------------------------------------------------------------


def test_sqlite_backend_registered_in_installed_entry_points() -> None:
    """PollyPM's own pyproject.toml must register the sqlite default."""
    names = {ep.name for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)}
    assert "sqlite" in names, (
        f"expected 'sqlite' in entry-point group {ENTRY_POINT_GROUP}; "
        f"found: {sorted(names)}"
    )


# --------------------------------------------------------------------------
# 6. URL resolver fallback coverage (no entry point invoked).
# --------------------------------------------------------------------------


def test_resolve_url_prefers_explicit(tmp_path: Path) -> None:
    config = _make_config(tmp_path, url="postgresql://x/y")
    assert _resolve_url(config) == "postgresql://x/y"


def test_resolve_url_derives_when_empty(tmp_path: Path) -> None:
    config = _make_config(tmp_path, url="   ")  # whitespace only
    assert _resolve_url(config) == f"sqlite:///{(tmp_path / 'state.db').resolve()}"
