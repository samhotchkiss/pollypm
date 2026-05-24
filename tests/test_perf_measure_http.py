from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_measure_http():
    path = Path(__file__).resolve().parents[1] / "scripts" / "perf" / "measure_http.py"
    spec = importlib.util.spec_from_file_location("measure_http", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


measure_http = _load_measure_http()


def test_percentile_uses_nearest_rank_reference_math() -> None:
    values = [0.30, 0.10, 0.20, 0.40]

    assert measure_http.percentile(values, 0.50) == 0.20
    assert measure_http.percentile(values, 0.95) == 0.40
    assert measure_http.percentile(values, 0.99) == 0.40


def test_summary_counts_non_2xx_without_dropping_latency_samples() -> None:
    samples = [
        measure_http.Sample("dashboard", 200, 0.10, 100),
        measure_http.Sample("dashboard", 503, 0.40, 10, "HTTPError: unavailable"),
        measure_http.Sample("dashboard", 404, 0.20, 30, "HTTPError: missing"),
        measure_http.Sample("dashboard", 200, 0.30, 120),
    ]

    summary = measure_http.scenario_summary(samples)

    assert summary["samples"] == 4
    assert summary["status_counts"] == {"200": 2, "404": 1, "503": 1}
    assert summary["non_2xx_count"] == 2
    assert summary["p95_seconds"] == 0.40
    assert summary["payload_bytes"]["max"] == 120
