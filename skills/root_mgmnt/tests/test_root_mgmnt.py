from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _load_module():
    path = Path(__file__).resolve().parents[1] / "handlers" / "main.py"
    spec = importlib.util.spec_from_file_location("test_root_mgmnt_handlers", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Projection:
    def __init__(self) -> None:
        self.values = []

    def set(self, slot, value, **kwargs):
        self.values.append((slot, value, kwargs))


def _snapshot(*, generated_at: str, score: int = 10):
    return {
        "ok": True,
        "generated_at": generated_at,
        "overview": {"total_subnets": 1, "live_subnets": 1},
        "policy": {"access_mode": "open"},
        "fleet": [
            {
                "subnet_id": "test-root",
                "live_now": "yes",
                "activity_score": score,
                "lifecycle_state": "active",
            }
        ],
        "lifecycle_candidates": [],
        "audit": [],
    }


def test_projection_ignores_timestamp_only_snapshot_rebuild(monkeypatch) -> None:
    module = _load_module()
    projection = _Projection()
    snapshots = iter(
        [
            _snapshot(generated_at="2026-07-24T08:00:00Z"),
            _snapshot(generated_at="2026-07-24T08:01:00Z"),
        ]
    )
    monkeypatch.setattr(module, "ctx_subnet", projection)
    monkeypatch.setattr(module, "_snapshot_or_fallback", lambda force=False: next(snapshots))

    first = module._refresh_projection(webspace_id="ops", force=True)
    second = module._refresh_projection(webspace_id="ops", force=True)

    assert first["projection_changed"] is True
    assert second["projection_changed"] is False
    assert len(projection.values) == 1
    assert projection.values[0][0] == "root_mgmnt.snapshot"
    assert projection.values[0][2]["webspace_id"] == "ops"


def test_projection_updates_when_fleet_changes(monkeypatch) -> None:
    module = _load_module()
    projection = _Projection()
    snapshots = iter(
        [
            _snapshot(generated_at="2026-07-24T08:00:00Z", score=10),
            _snapshot(generated_at="2026-07-24T08:01:00Z", score=20),
        ]
    )
    monkeypatch.setattr(module, "ctx_subnet", projection)
    monkeypatch.setattr(module, "_snapshot_or_fallback", lambda force=False: next(snapshots))

    module._refresh_projection(webspace_id="ops", force=True)
    changed = module._refresh_projection(webspace_id="ops", force=True)

    assert changed["projection_changed"] is True
    assert len(projection.values) == 2
    assert projection.values[-1][1]["fleet"]["items"][0]["activity_score"] == 20


def test_sse_change_is_forwarded_to_local_event_bus(monkeypatch) -> None:
    module = _load_module()
    events = []
    monkeypatch.setattr(module, "get_ctx", lambda: SimpleNamespace(bus=SimpleNamespace(publish=events.append)))

    module._publish_snapshot_changed({"revision": 3, "reason": "control.reported"})

    assert len(events) == 1
    assert events[0].type == "root.mgmnt.snapshot.changed"
    assert events[0].payload["revision"] == 3
