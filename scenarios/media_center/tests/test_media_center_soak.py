from __future__ import annotations

import sys
from pathlib import Path

import pytest


BENCHMARK_ROOT = Path(__file__).resolve().parents[1] / "benchmarks"
if str(BENCHMARK_ROOT) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_ROOT))

from run_media_center_soak import run  # noqa: E402


def test_short_soak_exercises_reads_during_agent_deltas() -> None:
    result = run(count=500, duration_seconds=2, enforce=True)

    assert result["passed"] is True
    assert result["workload"]["agent_delta_items"] >= 25
    assert result["result"]["page_count"] == 30
    assert result["result"]["playback_ok"] is True
    assert all(
        result["latency_ms"][name]["samples"] > 0
        for name in ("fts", "catalog_page", "playback_plan", "agent_delta")
    )


def test_acceptance_mode_rejects_short_or_small_runs() -> None:
    with pytest.raises(ValueError, match="duration"):
        run(count=20_000, duration_seconds=10, acceptance=True)
    with pytest.raises(ValueError, match="fixture"):
        run(count=1_000, duration_seconds=3_600, acceptance=True)
