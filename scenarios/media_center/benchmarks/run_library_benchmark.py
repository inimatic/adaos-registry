from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable


SCENARIO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_ROOT = SCENARIO_ROOT.parents[1]
SKILL_ROOT = REGISTRY_ROOT / "skills" / "media_center_skill"
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from media_center.catalog import MediaCenterRepository  # noqa: E402
from media_center.coordinator import MediaCatalogCoordinator  # noqa: E402


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
    return round(ordered[index], 3)


def _measure(call: Callable[[], Any], *, samples: int) -> tuple[list[float], Any]:
    timings: list[float] = []
    value: Any = None
    for _ in range(samples):
        started = time.perf_counter()
        value = call()
        timings.append((time.perf_counter() - started) * 1000)
    return timings, value


def _rss_mb() -> float | None:
    try:
        import psutil  # type: ignore[import-not-found]

        return round(psutil.Process().memory_info().rss / (1024 * 1024), 3)
    except Exception:
        return None


def _seed(repository: MediaCenterRepository, count: int) -> None:
    rows = []
    for index in range(count):
        name = f"movie-{index:05d}.mp4"
        rows.append(
            (
                f"mc-{index:05d}",
                "media_library_agent",
                f"resource-{index:05d}",
                name,
                f"Movie {index:05d}",
                "video",
                "video/mp4",
                1_000_000 + index,
                "2026-08-20T00:00:00+00:00",
                f"/api/node/media/resources/content/resource-{index:05d}",
                f"/media/resources/content/resource-{index:05d}",
                "",
                f"/mnt/library/Movies/Year {2000 + index % 25}/{name}",
                "{}",
                json.dumps(
                    {
                        "folder_segments": [
                            "Movies",
                            f"Year {2000 + index % 25}",
                        ],
                        "technical": {"codec": "h264", "height": 1080},
                    },
                    separators=(",", ":"),
                ),
                f"fingerprint-{index:05d}",
                "2026-08-20T00:00:00+00:00",
                "2026-08-20T00:00:00+00:00",
                0,
                0,
                0,
                "[]",
                "agent-benchmark",
                "node-benchmark",
                "root-benchmark",
                f"source-{index:05d}",
                1,
                f"Movies/Year {2000 + index % 25}",
                "",
                index + 1,
                f"work-{index:05d}",
                f"variant-{index:05d}",
                "collection-movies",
                '{"codec":"h264","height":1080}',
            )
        )
    connection = repository.connect()
    try:
        connection.executemany(
            """
            INSERT INTO catalog_items(
                id,source,resource_id,name,title,media_kind,mime_type,size_bytes,
                modified_at,content_path,routed_content_path,playback_id,
                source_path,descriptor_json,metadata_json,fingerprint,indexed_at,
                last_seen_at,missing,favorite,play_count,tags_json,agent_id,
                node_id,root_id,source_id,source_revision,folder_path,search_text,
                catalog_revision,work_id,variant_id,collection_id,quality_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        connection.commit()
    finally:
        connection.close()


def run(*, count: int = 20_000, enforce: bool = False) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(
        prefix="adaos-media-benchmark-", ignore_cleanup_errors=True
    ) as temporary:
        db_path = Path(temporary) / "media-center.sqlite3"
        os.environ["MEDIA_CENTER_DB_PATH"] = str(db_path)
        os.environ.setdefault("MEDIA_CENTER_DISCOVERY_MAX_CANDIDATES", "5000")
        repository = MediaCenterRepository(db_path)
        catalog = MediaCatalogCoordinator(repository)
        _seed(repository, count)
        started = time.perf_counter()
        catalog.ensure_schema()
        backfill_ms = (time.perf_counter() - started) * 1000

        search_queries = [
            "Movie 00042",
            "Movie 01999",
            "Year 2012",
            "Movie 09999",
            "h264",
        ]
        search_timings, search_result = _measure(
            lambda: catalog.list_items(
                query=search_queries[int(time.perf_counter_ns()) % len(search_queries)],
                media_kind="video",
                sort="title",
                limit=30,
            ),
            samples=40,
        )
        cursor = ""

        def page() -> dict[str, Any]:
            nonlocal cursor
            result = catalog.list_items(
                media_kind="video", sort="title", limit=30, cursor=cursor
            )
            cursor = str(result["pagination"].get("next_cursor") or "")
            if not cursor:
                cursor = ""
            return result

        page_timings, page_result = _measure(page, samples=40)
        discovery_timings, discovery_result = _measure(
            lambda: catalog.discovery_search(
                "Moovie 00042", profile_id="default", media_kind="video", limit=30
            ),
            samples=12,
        )
        encoded_page_bytes = len(
            json.dumps(page_result, ensure_ascii=False, default=str).encode("utf-8")
        )
        metrics = {
            "schema": "adaos.media_center.benchmark.v1",
            "fixture_count": count,
            "catalog_backfill_ms": round(backfill_ms, 3),
            "fts_ms": {
                "p50": _percentile(search_timings, 0.5),
                "p95": _percentile(search_timings, 0.95),
                "max": round(max(search_timings), 3),
                "samples": len(search_timings),
            },
            "catalog_page_ms": {
                "p50": _percentile(page_timings, 0.5),
                "p95": _percentile(page_timings, 0.95),
                "max": round(max(page_timings), 3),
                "samples": len(page_timings),
            },
            "local_discovery_ms": {
                "p50": _percentile(discovery_timings, 0.5),
                "p95": _percentile(discovery_timings, 0.95),
                "max": round(max(discovery_timings), 3),
                "samples": len(discovery_timings),
                "candidate_count": discovery_result["candidate_count"],
                "candidate_limit": discovery_result["candidate_limit"],
            },
            "encoded_page_bytes": encoded_page_bytes,
            "process_rss_mb": _rss_mb(),
            "result_counts": {
                "fts": search_result["count"],
                "page": page_result["count"],
                "discovery": discovery_result["count"],
            },
            "budgets": {
                "fts_p95_ms": 150,
                "catalog_page_p95_ms": 100,
                "local_discovery_p95_ms": 500,
                "encoded_page_bytes": 512 * 1024,
            },
        }
        failures = []
        for metric, budget in (
            ("fts_ms", "fts_p95_ms"),
            ("catalog_page_ms", "catalog_page_p95_ms"),
            ("local_discovery_ms", "local_discovery_p95_ms"),
        ):
            if metrics[metric]["p95"] > metrics["budgets"][budget]:
                failures.append(f"{metric}.p95")
        if encoded_page_bytes > metrics["budgets"]["encoded_page_bytes"]:
            failures.append("encoded_page_bytes")
        metrics["passed"] = not failures
        metrics["failures"] = failures
        if enforce and failures:
            raise SystemExit(json.dumps(metrics, ensure_ascii=False, indent=2))
        return metrics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=20_000)
    parser.add_argument("--enforce", action="store_true")
    arguments = parser.parse_args()
    result = run(count=max(100, min(100_000, arguments.count)), enforce=arguments.enforce)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
