"""Deploy-staleness helper tests."""

from __future__ import annotations

import json
from pathlib import Path

import pollypm.deploy_info as deploy_info
from pollypm.deploy_info import RuntimeBuildInfo, assess_deploy_staleness


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_assess_deploy_staleness_detects_copied_package_drift(tmp_path: Path) -> None:
    source = tmp_path / "checkout"
    source_pkg = source / "src" / "pollypm"
    installed_pkg = tmp_path / "tool" / "pollypm"
    _write(source_pkg / "__init__.py", '__version__ = "1"\n')
    _write(source_pkg / "feature.py", "VALUE = 'new'\n")
    _write(installed_pkg / "__init__.py", '__version__ = "1"\n')
    _write(installed_pkg / "feature.py", "VALUE = 'old'\n")

    info = RuntimeBuildInfo(
        package_name="pollypm",
        version="1",
        package_path=str(installed_pkg),
        source_checkout=str(source),
        direct_url_editable=False,
    )

    result = assess_deploy_staleness(info)

    assert result.state == "stale"
    assert "differs from the recorded source checkout" in result.status
    assert result.data["compared_files"] == 2
    assert result.data["source_digest"] != result.data["package_digest"]


def test_assess_deploy_staleness_passes_when_copied_package_matches(tmp_path: Path) -> None:
    source = tmp_path / "checkout"
    source_pkg = source / "src" / "pollypm"
    installed_pkg = tmp_path / "tool" / "pollypm"
    for root in (source_pkg, installed_pkg):
        _write(root / "__init__.py", '__version__ = "1"\n')
        _write(root / "feature.py", "VALUE = 'same'\n")

    info = RuntimeBuildInfo(
        package_name="pollypm",
        version="1",
        package_path=str(installed_pkg),
        source_checkout=str(source),
        direct_url_editable=False,
    )

    result = assess_deploy_staleness(info)

    assert result.state == "ok"
    assert "contents match" in result.status


def test_assess_deploy_staleness_passes_for_editable_checkout(tmp_path: Path) -> None:
    source = tmp_path / "checkout"
    source_pkg = source / "src" / "pollypm"
    _write(source_pkg / "__init__.py", '__version__ = "1"\n')

    info = RuntimeBuildInfo(
        package_name="pollypm",
        version="1",
        package_path=str(source_pkg),
        source_checkout=str(source),
        direct_url_editable=True,
    )

    result = assess_deploy_staleness(info)

    assert result.state == "ok"
    assert "source checkout" in result.status


def test_runtime_build_info_reads_embedded_served_git_sha(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package_path = tmp_path / "tool" / "pollypm"
    _write(package_path / "__init__.py", "")
    _write(
        package_path / deploy_info.BUILD_INFO_FILENAME,
        json.dumps(
            {
                "git_sha": "abc123",
                "git_commit_time": "2026-05-29T12:00:00+00:00",
            }
        ),
    )

    monkeypatch.setattr(
        deploy_info,
        "_package_location",
        lambda _import_name: (package_path, package_path / "__init__.py"),
    )
    monkeypatch.setattr(
        deploy_info,
        "_distribution_info",
        lambda _package_name: ("1", tmp_path / "pollypm-1.dist-info", None),
    )

    info = deploy_info.runtime_build_info()

    assert info.served_git_sha == "abc123"
    assert info.served_git_commit_time == "2026-05-29T12:00:00+00:00"


def test_runtime_code_fingerprint_prefers_imported_package_git_head(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package_path = tmp_path / "checkout" / "src" / "pollypm"
    _write(package_path / "__init__.py", "")
    checkout = tmp_path / "checkout"

    monkeypatch.setattr(
        deploy_info,
        "_package_location",
        lambda _import_name: (package_path, package_path / "__init__.py"),
    )
    monkeypatch.setattr(
        deploy_info,
        "_distribution_info",
        lambda _package_name: ("1", tmp_path / "pollypm-1.dist-info", None),
    )
    monkeypatch.setattr(deploy_info, "_git_root", lambda path: checkout)
    monkeypatch.setattr(deploy_info, "_git_head", lambda path: "abc123")

    fingerprint = deploy_info.runtime_code_fingerprint()

    assert fingerprint is not None
    assert fingerprint.kind == "served_git_sha"
    assert fingerprint.value == "abc123"
    assert fingerprint.source_checkout == str(checkout)


def test_runtime_code_fingerprint_uses_embedded_sha_without_git_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package_path = tmp_path / "tool" / "pollypm"
    _write(package_path / "__init__.py", "")
    _write(
        package_path / deploy_info.BUILD_INFO_FILENAME,
        json.dumps({"git_sha": "embedded-sha"}),
    )

    monkeypatch.setattr(
        deploy_info,
        "_package_location",
        lambda _import_name: (package_path, package_path / "__init__.py"),
    )
    monkeypatch.setattr(
        deploy_info,
        "_distribution_info",
        lambda _package_name: ("1", tmp_path / "pollypm-1.dist-info", None),
    )
    monkeypatch.setattr(deploy_info, "_git_root", lambda path: None)

    fingerprint = deploy_info.runtime_code_fingerprint()

    assert fingerprint is not None
    assert fingerprint.kind == "embedded_git_sha"
    assert fingerprint.value == "embedded-sha"


def test_runtime_build_info_flags_stale_when_embedded_sha_differs_from_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package_path = tmp_path / "tool" / "pollypm"
    source_checkout = tmp_path / "checkout"
    _write(package_path / "__init__.py", "")
    _write(
        package_path / deploy_info.BUILD_INFO_FILENAME,
        json.dumps({"git_sha": "old-sha"}),
    )
    _write(source_checkout / "src" / "pollypm" / "__init__.py", "")

    monkeypatch.setattr(
        deploy_info,
        "_package_location",
        lambda _import_name: (package_path, package_path / "__init__.py"),
    )
    monkeypatch.setattr(
        deploy_info,
        "_distribution_info",
        lambda _package_name: (
            "1",
            tmp_path / "pollypm-1.dist-info",
            {
                "url": source_checkout.as_uri(),
                "dir_info": {"editable": False},
            },
        ),
    )
    monkeypatch.setattr(
        deploy_info,
        "_git_root",
        lambda path: source_checkout if path == source_checkout else None,
    )
    monkeypatch.setattr(
        deploy_info,
        "_git_head",
        lambda path: "new-sha" if path == source_checkout else None,
    )

    info = deploy_info.runtime_build_info()

    assert info.served_git_sha == "old-sha"
    assert info.source_git_sha == "new-sha"
    assert info.stale is True
    assert info.stale_reason == "served git SHA differs from the recorded source checkout"


def test_infer_stale_falls_back_to_package_mtime_without_embedded_sha() -> None:
    info = RuntimeBuildInfo(
        package_name="pollypm",
        version="1",
        direct_url_editable=False,
        source_git_commit_time="2026-05-29T12:10:00Z",
        package_mtime="2026-05-29T12:00:00Z",
    )

    stale, reason = deploy_info._infer_stale(info)

    assert stale is True
    assert reason == "source checkout HEAD is newer than the served package files"
