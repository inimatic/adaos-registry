from __future__ import annotations

import sys
from pathlib import Path

import pytest


BENCHMARK_ROOT = Path(__file__).resolve().parents[1] / "benchmarks"
if str(BENCHMARK_ROOT) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_ROOT))

from run_media_center_soak import _rss_window_summary, _warmup_duration, run  # noqa: E402
import run_media_center_soak as soak  # noqa: E402


@pytest.fixture
def deterministic_latency(monkeypatch):
    # Keep real concurrent SQLite operations and error recording. Timing budget
    # qualification belongs to the benchmark on its measured host; these unit
    # tests inject samples to verify both sides of the unchanged budget gate.
    original = soak._record_call
    agent_ms = [1.0]

    def record(name, call, timings, errors):
        result = original(name, call, timings, errors)
        timings[name][-1] = agent_ms[0] if name == "agent_delta" else 1.0
        return result

    monkeypatch.setattr(soak, "_record_call", record)
    return agent_ms


def test_short_soak_exercises_reads_during_agent_deltas(deterministic_latency) -> None:
    result = run(count=500, duration_seconds=5, enforce=True)

    assert result["passed"] is True
    assert result["workload"]["agent_delta_items"] >= 25
    assert result["result"]["page_count"] == 30
    assert result["result"]["playback_ok"] is True
    assert all(
        result["latency_ms"][name]["samples"] > 0
        for name in ("fts", "catalog_page", "playback_plan", "agent_delta")
    )


def test_soak_enforces_agent_delta_latency_budget(deterministic_latency) -> None:
    deterministic_latency[0] = 251.0
    with pytest.raises(SystemExit, match="agent_delta.p95"):
        run(count=100, duration_seconds=1, enforce=True)


def test_acceptance_mode_rejects_short_or_small_runs() -> None:
    with pytest.raises(ValueError, match="duration"):
        run(count=50_000, duration_seconds=10, acceptance=True)
    with pytest.raises(ValueError, match="fixture"):
        run(count=1_000, duration_seconds=3_600, acceptance=True)


def test_acceptance_warmup_allows_allocator_and_sqlite_caches_to_stabilize() -> None:
    assert _warmup_duration(3_600, acceptance=True) == 300
    assert _warmup_duration(90, acceptance=False) == 18


def test_rss_gate_measures_sustained_windows_and_retains_peak_range() -> None:
    summary = _rss_window_summary([40.0] * 10 + [90.0] + [44.0] * 10)

    assert summary["window_samples"] == 2
    assert summary["baseline_p95"] == 40.0
    assert summary["terminal_p95"] == 44.0
    assert summary["sustained_growth"] == 4.0
    assert summary["range_growth"] == 50.0
