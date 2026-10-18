import sys
from datetime import datetime, timezone
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from media_center.health import health_projection

NOW = datetime(2026, 10, 18, 17, 0, tzinfo=timezone.utc)


def test_historical_failures_do_not_fail_readiness_or_imply_ingest_success():
    health = health_projection(read_ready=True, job_counts={'failed': 5000}, now=NOW)
    assert health['readiness'] == {'state': 'ready', 'ready': True}
    assert health['history']['state'] == 'retained_failures'
    assert health['history']['gates_readiness'] is False
    assert health['ingest']['state'] == 'not_observed'
    assert health['storage']['state'] == health['endpoint']['state'] == 'not_observed'


@pytest.mark.parametrize('sync,state', [
    ({'running': False, 'last_success_at': '2026-10-18T16:59:55Z'}, 'stopped'),
    ({'running': True}, 'initializing'),
    ({'running': True, 'consecutive_failures': 2}, 'retry_wait'),
    ({'running': True, 'last_success_at': '2026-10-18T16:50:00Z'}, 'stale'),
    ({'running': True, 'last_success_at': '2026-10-18T16:59:55Z'}, 'current'),
    ({'running': True, 'state': 'catching_up',
      'last_success_at': '2026-10-18T16:59:55Z'}, 'catching_up'),
    ({'running': True, 'last_success_at': 'unparseable'}, 'initializing'),
])
def test_ingest_is_independent_from_cached_catalog_readiness(sync, state):
    health = health_projection(read_ready=True, sync=sync, now=NOW)
    assert health['readiness']['ready'] is True
    assert health['ingest']['state'] == state


def test_cold_loading_does_not_fabricate_history_or_probe_availability():
    health = health_projection(read_ready=False, now=NOW)
    assert health['liveness']['state'] == 'alive'
    assert health['readiness']['state'] == 'loading'
    assert health['history']['failed_jobs'] is None
    assert health['storage']['state'] == 'not_observed'


def test_freshness_budget_respects_the_actual_poll_interval():
    health = health_projection(read_ready=True, now=NOW, sync={
        'running': True, 'poll_seconds': 300,
        'last_success_at': '2026-10-18T16:55:00Z'})
    assert health['ingest']['state'] == 'current'
    assert health['ingest']['freshness_budget_seconds'] == 900
