from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[1] / "handlers" / "main.py"
    spec = importlib.util.spec_from_file_location("test_release_validation_skill_handlers", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Service:
    def __init__(self) -> None:
        self.nodes = []
        self.suites = []
        self.campaigns = []

    def register_node(self, node):
        self.nodes.append(node)
        return node.to_dict(public=True)

    def register_suite(self, suite):
        self.suites.append(suite)
        return suite.to_dict()

    def create_campaign(self, campaign):
        value = campaign.to_dict()
        self.campaigns.insert(0, value)
        return {**value, "assignments": []}

    def run_campaign(self, campaign_id):
        campaign = next(item for item in self.campaigns if item["campaign_id"] == campaign_id)
        campaign["state"] = "passed"
        campaign["result"] = {
            "passed": 1,
            "failed": 0,
            "inconclusive": 0,
            "timed_out": 0,
        }
        return {**campaign, "assignments": []}

    def campaign(self, campaign_id):
        campaign = next(item for item in self.campaigns if item["campaign_id"] == campaign_id)
        return {**campaign, "assignments": []}

    def snapshot(self):
        return {
            "summary": {
                "nodes_enabled": len(self.nodes),
                "assignments_running": 0,
            },
            "nodes": [node.to_dict(public=True) for node in self.nodes],
            "campaigns": list(self.campaigns),
            "assignments": [],
            "events": [],
            "updated_at": "2026-07-23T00:00:00+00:00",
        }


class _Projection:
    def __init__(self) -> None:
        self.values = []

    def set(self, slot, value, **kwargs):
        self.values.append((slot, value, kwargs))


def test_skill_prepares_runs_notifies_and_projects(monkeypatch) -> None:
    module = _load_module()
    service = _Service()
    projection = _Projection()
    notifications = []
    monkeypatch.setattr(module, "_service", lambda: service)
    monkeypatch.setattr(module, "ctx_subnet", projection)
    monkeypatch.setattr(module, "publish_event", lambda *args, **kwargs: notifications.append((args, kwargs)))

    prepared = module.prepare_campaign("build-123", campaign_id="manual-test", webspace_id="ops")
    result = module.run_campaign("manual-test", webspace_id="ops")

    assert prepared["campaign"]["state"] == "pending"
    assert result["campaign"]["state"] == "passed"
    assert service.nodes[0].allowed_profiles == ("observe",)
    assert service.suites[0].checks == module.OBSERVE_CHECKS
    assert service.suites[0].timeout_s == module.DEFAULT_SUITE_TIMEOUT_S
    assert projection.values[-1][0] == "release_validation.snapshot"
    assert projection.values[-1][1]["summary"]["value"] == "PASSED"
    assert notifications[0][0][0] == "ui.notify"


def test_skill_treats_no_pending_campaign_as_idempotent_noop(monkeypatch) -> None:
    module = _load_module()
    service = _Service()
    projection = _Projection()
    monkeypatch.setattr(module, "_service", lambda: service)
    monkeypatch.setattr(module, "ctx_subnet", projection)

    result = module.run_latest_campaign()

    assert result["ok"] is True
    assert result["noop"] is True
    assert result["status"] == "no_pending_campaign"
    assert projection.values[-1][1]["summary"]["value"] == "IDLE"


def test_skill_retries_latest_terminal_campaign_without_rewriting_it(monkeypatch) -> None:
    module = _load_module()
    service = _Service()
    projection = _Projection()
    notifications = []
    monkeypatch.setattr(module, "_service", lambda: service)
    monkeypatch.setattr(module, "ctx_subnet", projection)
    monkeypatch.setattr(module, "publish_event", lambda *args, **kwargs: notifications.append((args, kwargs)))
    module.prepare_campaign("build-123", campaign_id="manual-original")
    module.run_campaign("manual-original")

    result = module.retry_latest_campaign(webspace_id="ops")

    assert result["ok"] is True
    assert result["campaign"]["state"] == "passed"
    assert result["campaign"]["target_build"] == "build-123"
    assert result["campaign"]["campaign_id"] != "manual-original"
    assert len(service.campaigns) == 2
    assert notifications[-1][0][0] == "ui.notify"


def test_skill_rehydrate_projects_durable_snapshot(monkeypatch) -> None:
    module = _load_module()
    service = _Service()
    projection = _Projection()
    monkeypatch.setattr(module, "_service", lambda: service)
    monkeypatch.setattr(module, "ctx_subnet", projection)

    result = module.rehydrate({"webspace_id": "ops"})

    assert result["ok"] is True
    assert projection.values[-1][0] == "release_validation.snapshot"
    assert projection.values[-1][2]["webspace_id"] == "ops"


def test_ui_snapshot_makes_machine_reason_readable(monkeypatch) -> None:
    module = _load_module()
    service = _Service()
    monkeypatch.setattr(module, "_service", lambda: service)
    raw = service.snapshot()
    raw["assignments"] = [
        {
            "assignment_id": "assignment-1",
            "node_id": "linux-exp-01",
            "state": "passed",
            "result": {
                "checks_passed": 5,
                "checks_total": 5,
                "reason": "all_observe_checks_passed",
            },
        }
    ]

    snapshot = module._ui_snapshot(raw)

    assert snapshot["assignments"]["items"][0]["reason_label"] == "all observe checks passed"
