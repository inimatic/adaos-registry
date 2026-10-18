"""Pure health projection: availability, freshness and failures are not one bit."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping, Any


def health_projection(*, read_ready: bool, sync: Mapping[str, Any] | None = None,
                      job_counts: Mapping[str, int] | None = None,
                      now: datetime | None = None) -> dict:
    sync = dict(sync or {})
    observed = now or datetime.now(timezone.utc)
    freshness_budget = max(180.0, min(900.0, float(sync.get("poll_seconds") or 30) * 3))
    last_success = str(sync.get("last_success_at") or "")
    age = None
    if last_success:
        try:
            timestamp = datetime.fromisoformat(last_success.replace("Z", "+00:00"))
            if timestamp.tzinfo is not None:
                age = max(0.0, (observed - timestamp).total_seconds())
        except (ValueError, TypeError):
            pass
    if not sync:
        ingest_state = "not_observed"
    elif not sync.get("running"):
        ingest_state = "stopped"
    elif sync.get("consecutive_failures", 0) or sync.get("state") == "retry_wait":
        ingest_state = "retry_wait"
    elif age is None:
        ingest_state = "initializing"
    elif age > freshness_budget:
        ingest_state = "stale"
    elif sync.get("state") in {"running", "catching_up"}:
        ingest_state = "catching_up"
    else:
        ingest_state = "current"
    failures = None if job_counts is None else max(0, int(job_counts.get("failed", 0)))
    return {
        "schema": "adaos.media_center.health_projection.v1",
        "liveness": {"state": "alive"},
        "readiness": {"state": "ready" if read_ready else "loading", "ready": read_ready},
        "ingest": {"state": ingest_state, "last_success_at": last_success,
                   "last_progress_at": str(sync.get("last_progress_at") or ""),
                   "freshness_budget_seconds": freshness_budget,
                   "success_age_seconds": None if age is None else round(age, 3)},
        # Catalog database size is not evidence of free-space or endpoint health.
        # Neither dimension performs an RPC or filesystem capacity probe here.
        "storage": {"state": "not_observed", "admission": "per_job_fail_closed"},
        "endpoint": {"state": "not_observed", "observer": "playback_endpoint"},
        "history": {"failed_jobs": failures, "gates_readiness": False,
                    "state": "not_observed" if failures is None else
                             "retained_failures" if failures else "no_retained_failures"},
    }


__all__ = ["health_projection"]
