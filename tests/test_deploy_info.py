"""Deploy-staleness helper tests."""

from __future__ import annotations

from pathlib import Path

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
