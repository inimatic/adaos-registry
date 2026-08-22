from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable


SCENARIO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_ROOT = SCENARIO_ROOT.parents[1]
SKILL_ROOT = REGISTRY_ROOT / "skills" / "media_center_skill"
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from media_center.catalog import MediaCenterRepository  # noqa: E402
from media_center.coordinator import MediaCatalogCoordinator  # noqa: E402
from run_library_benchmark import _seed  # noqa: E402


ACCEPTANCE_DURATION_SECONDS = 3_600
ACCEPTANCE_FIXTURE_COUNT = 20_000
MAX_ERRORS = 100


def _warmup_duration(requested_duration: float, *, acceptance: bool) -> float:
    ceiling = 300.0 if acceptance else 60.0
    return min(ceiling, requested_duration * 0.20)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
    return round(ordered[index], 3)


def _latency_summary(values: list[float]) -> dict[str, float | int]:
    return {
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "max": round(max(values), 3) if values else 0.0,
        "samples": len(values),
    }


def _rss_window_summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "window_samples": 0,
            "baseline_p95": None,
            "terminal_p95": None,
            "sustained_growth": None,
            "range_growth": None,
        }
    window_samples = max(1, min(300, len(values) // 10 or 1))
    baseline_p95 = _percentile(values[:window_samples], 0.95)
    terminal_p95 = _percentile(values[-window_samples:], 0.95)
    return {
        "window_samples": window_samples,
        "baseline_p95": baseline_p95,
        "terminal_p95": terminal_p95,
        "sustained_growth": round(max(0.0, terminal_p95 - baseline_p95), 3),
        "range_growth": round(max(values) - min(values), 3),
    }


def _agent_page(indices: list[int], revision: int) -> dict[str, Any]:
    items = []
    for index in indices:
        name = f"movie-{index:05d}.mp4"
        relative_path = f"Movies/Year {2000 + index % 25}/{name}"
        resource_id = f"resource-{index:05d}"
        items.append(
            {
                "schema": "adaos.media_library_agent.delta.v1",
                "sequence": revision * 100 + index,
                "operation": "upsert",
                "source_id": f"source-{index:05d}",
                "source_revision": revision,
                "source": {
                    "id": f"source-{index:05d}",
                    "revision": revision,
                    "root_id": "root-benchmark",
                    "resource_id": resource_id,
                    "name": name,
                    "relative_path": relative_path,
                    "folder_path": f"Movies/Year {2000 + index % 25}",
                    "media_kind": "video",
                    "mime_type": "video/mp4",
                    "size_bytes": 1_000_000 + index,
                    "fingerprint": f"fingerprint-{index:05d}",
                    "metadata": {
                        "folder_segments": [
                            "Movies",
                            f"Year {2000 + index % 25}",
                        ],
                        "tags": ["h264", f"revision-{revision % 7}"],
                    },
                    "descriptor": {
                        "schema": "adaos.media.resource.v1",
                        "id": resource_id,
                        "resource_id": resource_id,
                        "name": name,
                        "title": f"Movie {index:05d}",
                        "mime_type": "video/mp4",
                        "size_bytes": 1_000_000 + index,
                        "content_path": (
                            f"/api/node/media/resources/content/{resource_id}"
                        ),
                        "routed_content_path": (
                            f"/media/resources/content/{resource_id}"
                        ),
                        "metadata": {
                            "relative_path": relative_path,
                            "folder_path": f"Movies/Year {2000 + index % 25}",
                            "technical": {"codec": "h264", "height": 1080},
                        },
                    },
                },
            }
        )
    return {
        "schema": "adaos.media_library_agent.delta_page.v1",
        "agent": {"id": "agent-benchmark", "node_id": "node-benchmark"},
        "items": items,
        "next_cursor": str(revision),
        "has_more": True,
    }


def _record_call(
    name: str,
    call: Callable[[], Any],
    timings: dict[str, list[float]],
    errors: list[dict[str, str]],
) -> Any:
    started = time.perf_counter()
    try:
        result = call()
        if isinstance(result, dict) and result.get("ok") is False:
            raise RuntimeError(str(result.get("error") or "operation_failed"))
        return result
    except Exception as exc:
        if len(errors) < MAX_ERRORS:
            errors.append({"operation": name, "error": f"{type(exc).__name__}: {exc}"})
        return None
    finally:
        timings[name].append((time.perf_counter() - started) * 1000)


def run(
    *,
    count: int = ACCEPTANCE_FIXTURE_COUNT,
    duration_seconds: float = ACCEPTANCE_DURATION_SECONDS,
    acceptance: bool = False,
    enforce: bool = False,
) -> dict[str, Any]:
    requested_duration = max(1.0, float(duration_seconds))
    fixture_count = max(100, min(100_000, int(count)))
    if acceptance and requested_duration < ACCEPTANCE_DURATION_SECONDS:
        raise ValueError(
            f"acceptance duration must be at least {ACCEPTANCE_DURATION_SECONDS} seconds"
        )
    if acceptance and fixture_count < ACCEPTANCE_FIXTURE_COUNT:
        raise ValueError(
            f"acceptance fixture must contain at least {ACCEPTANCE_FIXTURE_COUNT} items"
        )

    with tempfile.TemporaryDirectory(
        prefix="adaos-media-soak-", ignore_cleanup_errors=True
    ) as temporary:
        db_path = Path(temporary) / "media-center.sqlite3"
        os.environ["MEDIA_CENTER_DB_PATH"] = str(db_path)
        os.environ.setdefault("MEDIA_CENTER_DISCOVERY_MAX_CANDIDATES", "5000")
        repository = MediaCenterRepository(db_path)
        catalog = MediaCatalogCoordinator(repository)
        _seed(repository, fixture_count)
        catalog.refresh_search_index()

        hot_indices = list(range(min(25, fixture_count)))
        primed = catalog.apply_agent_page(_agent_page(hot_indices, 2))
        if not primed.get("ok") or int(primed.get("applied_count") or 0) != len(
            hot_indices
        ):
            raise RuntimeError("failed to prime playback-under-indexing fixture")

        timings: dict[str, list[float]] = {
            "fts": [],
            "catalog_page": [],
            "playback_plan": [],
            "agent_delta": [],
        }
        errors: list[dict[str, str]] = []
        stop = threading.Event()
        completed = {"agent_delta_items": 0}
        last_page: dict[str, Any] = {}
        largest_page: dict[str, Any] = {}
        last_plan: dict[str, Any] = {}
        last_search: dict[str, Any] = {}
        shared_lock = threading.Lock()

        def writer() -> None:
            revision = 3
            while not stop.is_set():
                page = _record_call(
                    "agent_delta",
                    lambda: catalog.apply_agent_page(_agent_page(hot_indices, revision)),
                    timings,
                    errors,
                )
                if page:
                    completed["agent_delta_items"] += int(page.get("applied_count") or 0)
                revision += 1
                stop.wait(0.20)

        def fts_reader() -> None:
            query_index = 0
            queries = (
                "Movie 00000",
                f"Year {2000 + min(12, fixture_count - 1) % 25}",
                "h264",
                f"Movie {fixture_count - 1:05d}",
            )
            nonlocal last_search
            while not stop.is_set():
                query = queries[query_index % len(queries)]
                query_index += 1
                result = _record_call(
                    "fts",
                    lambda: catalog.list_items(
                        query=query, media_kind="video", sort="title", limit=30
                    ),
                    timings,
                    errors,
                )
                if result is not None:
                    with shared_lock:
                        last_search = result
                stop.wait(0.08)

        def page_reader() -> None:
            cursor = ""
            nonlocal last_page, largest_page
            while not stop.is_set():
                result = _record_call(
                    "catalog_page",
                    lambda: catalog.list_items(
                        media_kind="video", sort="title", limit=30, cursor=cursor
                    ),
                    timings,
                    errors,
                )
                if result is not None:
                    cursor = str(result.get("pagination", {}).get("next_cursor") or "")
                    with shared_lock:
                        last_page = result
                        if int(result.get("count") or 0) > int(
                            largest_page.get("count") or 0
                        ):
                            largest_page = result
                stop.wait(0.08)

        def playback_reader() -> None:
            nonlocal last_plan
            while not stop.is_set():
                result = _record_call(
                    "playback_plan",
                    lambda: catalog.playback_plan(
                        "mc-00000",
                        endpoint_id="browser-soak",
                        endpoint_node_id="node-controller",
                        endpoint_capabilities={
                            "codecs": ["h264", "aac"],
                            "max_video_height": 1080,
                        },
                    ),
                    timings,
                    errors,
                )
                if result is not None:
                    with shared_lock:
                        last_plan = result
                stop.wait(0.08)

        process = None
        cpu_count = max(1, os.cpu_count() or 1)
        rss_samples: list[float] = []
        steady_rss_samples: list[float] = []
        cpu_samples: list[float] = []
        warmup_seconds = _warmup_duration(
            requested_duration,
            acceptance=acceptance,
        )
        try:
            import psutil  # type: ignore[import-not-found]

            process = psutil.Process()
            process.cpu_percent(interval=None)
        except Exception:
            process = None

        workers = [
            threading.Thread(target=writer, name="media-soak-writer", daemon=True),
            threading.Thread(target=fts_reader, name="media-soak-fts", daemon=True),
            threading.Thread(target=page_reader, name="media-soak-page", daemon=True),
            threading.Thread(
                target=playback_reader, name="media-soak-playback", daemon=True
            ),
        ]
        started = time.monotonic()
        for worker in workers:
            worker.start()
        try:
            while time.monotonic() - started < requested_duration:
                stop.wait(min(1.0, requested_duration - (time.monotonic() - started)))
                if process is not None:
                    rss_mb = process.memory_info().rss / (1024 * 1024)
                    rss_samples.append(rss_mb)
                    if time.monotonic() - started >= warmup_seconds:
                        steady_rss_samples.append(rss_mb)
                    cpu_samples.append(process.cpu_percent(interval=None) / cpu_count)
        finally:
            stop.set()
            for worker in workers:
                worker.join(timeout=10)
        elapsed = time.monotonic() - started

        measured_page = largest_page or last_page
        encoded_page_bytes = len(
            json.dumps(measured_page, ensure_ascii=False, default=str).encode("utf-8")
        )
        wal_path = Path(f"{db_path}-wal")
        steady_rss = _rss_window_summary(steady_rss_samples)
        metrics: dict[str, Any] = {
            "schema": "adaos.media_center.soak.v1",
            "fixture_count": fixture_count,
            "requested_duration_seconds": requested_duration,
            "duration_seconds": round(elapsed, 3),
            "acceptance_mode": bool(acceptance),
            "workload": {
                "readers": 3,
                "writer": "media_library_agent_delta",
                "agent_delta_items": completed["agent_delta_items"],
            },
            "latency_ms": {
                name: _latency_summary(values) for name, values in timings.items()
            },
            "resource": {
                "rss_mb": {
                    "min": round(min(rss_samples), 3) if rss_samples else None,
                    "max": round(max(rss_samples), 3) if rss_samples else None,
                    "warmup_seconds": round(warmup_seconds, 3),
                    "steady_min": (
                        round(min(steady_rss_samples), 3)
                        if steady_rss_samples
                        else None
                    ),
                    "steady_max": (
                        round(max(steady_rss_samples), 3)
                        if steady_rss_samples
                        else None
                    ),
                    **steady_rss,
                    "samples": len(rss_samples),
                    "steady_samples": len(steady_rss_samples),
                },
                "aggregate_cpu_percent": {
                    "p50": _percentile(cpu_samples, 0.50),
                    "p95": _percentile(cpu_samples, 0.95),
                    "max": round(max(cpu_samples), 3) if cpu_samples else None,
                    "samples": len(cpu_samples),
                },
                "wal_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
            },
            "result": {
                "fts_count": int(last_search.get("count") or 0),
                "page_count": int(measured_page.get("count") or 0),
                "page_bytes": encoded_page_bytes,
                "playback_ok": bool(last_plan.get("ok")),
                "playback_source_id": str(last_plan.get("source_id") or ""),
            },
            "budgets": {
                "fts_p95_ms": 200,
                "catalog_page_p95_ms": 100,
                "playback_plan_p95_ms": 250,
                "agent_delta_p95_ms": 250,
                "page_bytes": 512 * 1024,
                "page_count": 30,
                "rss_max_mb": 350,
                "rss_sustained_growth_mb": 64,
                "aggregate_cpu_p95_percent": 50,
                "wal_bytes": 256 * 1024 * 1024,
            },
            "errors": errors,
        }
        failures: list[str] = []
        for operation, budget in (
            ("fts", "fts_p95_ms"),
            ("catalog_page", "catalog_page_p95_ms"),
            ("playback_plan", "playback_plan_p95_ms"),
            ("agent_delta", "agent_delta_p95_ms"),
        ):
            if not timings[operation]:
                failures.append(f"{operation}.samples")
            elif metrics["latency_ms"][operation]["p95"] > metrics["budgets"][budget]:
                failures.append(f"{operation}.p95")
        if errors:
            failures.append("operation_errors")
        if metrics["result"]["fts_count"] < 1:
            failures.append("fts_result")
        if metrics["result"]["page_count"] != min(30, fixture_count):
            failures.append("page_count")
        if encoded_page_bytes > metrics["budgets"]["page_bytes"]:
            failures.append("page_bytes")
        if not metrics["result"]["playback_ok"]:
            failures.append("playback_result")
        if completed["agent_delta_items"] < len(hot_indices):
            failures.append("agent_delta_progress")
        if rss_samples:
            if max(rss_samples) > metrics["budgets"]["rss_max_mb"]:
                failures.append("rss_max")
            sustained_growth = metrics["resource"]["rss_mb"]["sustained_growth"]
            if (
                sustained_growth is not None
                and sustained_growth
                > metrics["budgets"]["rss_sustained_growth_mb"]
            ):
                failures.append("rss_sustained_growth")
        if cpu_samples and _percentile(cpu_samples, 0.95) > metrics["budgets"][
            "aggregate_cpu_p95_percent"
        ]:
            failures.append("cpu_p95")
        if metrics["resource"]["wal_bytes"] > metrics["budgets"]["wal_bytes"]:
            failures.append("wal_bytes")
        if acceptance and elapsed < ACCEPTANCE_DURATION_SECONDS:
            failures.append("acceptance_duration")
        metrics["passed"] = not failures
        metrics["failures"] = failures
        if enforce and failures:
            raise SystemExit(json.dumps(metrics, ensure_ascii=False, indent=2))
        return metrics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=ACCEPTANCE_FIXTURE_COUNT)
    parser.add_argument(
        "--duration-seconds", type=float, default=ACCEPTANCE_DURATION_SECONDS
    )
    parser.add_argument("--acceptance", action="store_true")
    parser.add_argument("--enforce", action="store_true")
    arguments = parser.parse_args()
    result = run(
        count=arguments.count,
        duration_seconds=arguments.duration_seconds,
        acceptance=arguments.acceptance,
        enforce=arguments.enforce,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
