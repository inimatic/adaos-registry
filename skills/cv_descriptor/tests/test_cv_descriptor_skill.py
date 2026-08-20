from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]


def _load_module(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CV_DESCRIPTOR_STATE_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "cv_descriptor_handlers_main_test",
        SKILL_ROOT / "handlers" / "main.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module._project = lambda *args, **kwargs: None
    module._publish_event = lambda *args, **kwargs: None
    module._publish_stream_event = lambda *args, **kwargs: None
    return module


def test_status_projects_public_empty_state(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module(tmp_path, monkeypatch)

    result = mod.cv_descriptor_status()

    assert result["ok"] is True
    assert result["current"]["status"] == "init"
    assert result["current"]["model"]["id"] == "tfjs_mobilenet_v2_100_embedding"
    assert result["current"]["model"]["runtime"] == "tfjs-mobilenet"
    assert result["current"]["model"]["modelUrl"].startswith("https://storage.googleapis.com/")
    assert any(item["id"] == "browser_embedding_placeholder" for item in result["current"]["model_options"])
    assert result["current"]["stats"]["descriptor_count"] == 0
    assert result["descriptors"]["items"] == []
    assert result["runtime"]["client_runtime_required"] is True


def test_sessions_define_low_power_skill_use_cases(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module(tmp_path, monkeypatch)

    result = mod.cv_descriptor_status()
    setup = result["runtime"]["sessions"]["setup"]
    work = result["runtime"]["sessions"]["work"]

    assert setup["useCase"]["kind"] == "descriptor_capture"
    assert work["useCase"]["kind"] == "descriptor_identification"
    assert work["useCase"]["profile"] == "low_power"
    assert work["camera"]["width"] == {"ideal": 320, "max": 320}
    assert work["camera"]["height"] == {"ideal": 240, "max": 240}
    assert work["camera"]["frameRate"] == {"ideal": 5, "max": 5}
    assert work["pipeline"]["targetFps"] == 4
    assert work["pipeline"]["tasks"] == ["embed", "identify"]


def test_old_empty_placeholder_state_migrates_to_trial_model(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        '{"model":{"id":"browser_embedding_placeholder","status":"pending_client_runtime"}, "descriptors":[]}',
        encoding="utf-8",
    )
    mod = _load_module(tmp_path, monkeypatch)

    result = mod.cv_descriptor_status()

    assert result["current"]["model"]["id"] == "tfjs_mobilenet_v2_100_embedding"


def test_save_descriptor_keeps_vectors_out_of_public_projection(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module(tmp_path, monkeypatch)

    saved = mod.cv_descriptor_save_descriptor(
        vector=[0.1, 0.2, 0.3],
        title="Desk mug",
        description="White mug on the desk",
        thumbnail="data:image/jpeg;base64,abc",
    )
    descriptor = saved["descriptor"]
    public_item = saved["descriptors"]["items"][0]
    targets = mod.cv_descriptor_get_targets()

    assert saved["ok"] is True
    assert descriptor["title"] == "Desk mug"
    assert public_item["vector_dim"] == 3
    assert "vector" not in public_item
    assert targets["targets"][0]["vector"] == [0.1, 0.2, 0.3]
    assert targets["targets"][0]["label"] == "Desk mug"


def test_targets_are_filtered_by_active_model_signature(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module(tmp_path, monkeypatch)

    first_model = mod.cv_descriptor_configure_model(
        model={
            "id": "embedder-a",
            "runtime": "client-cv",
            "task": "embed",
            "modelAssetPath": "resource:a",
        }
    )["model"]
    mod.cv_descriptor_save_descriptor(vector=[1.0, 0.0], title="Key")
    assert mod.cv_descriptor_get_targets()["targets"][0]["model_signature"] == first_model["model_signature"]

    mod.cv_descriptor_configure_model(
        model={
            "id": "embedder-b",
            "runtime": "client-cv",
            "task": "embed",
            "modelAssetPath": "resource:b",
        }
    )

    assert mod.cv_descriptor_get_targets()["targets"] == []


def test_update_delete_and_runtime_event_flow(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module(tmp_path, monkeypatch)

    saved = mod.cv_descriptor_save_descriptor(vector=[0.4, 0.5], title="Badge")
    descriptor_id = saved["descriptor"]["id"]
    updated = mod.cv_descriptor_update_descriptor(
        id=descriptor_id,
        description="Visitor badge",
        threshold=0.9,
    )
    command = mod.cv_descriptor_runtime_command(action="start", mode="work")
    event = mod.cv_descriptor_record_runtime_event(
        kind="match.enter",
        match={"id": descriptor_id, "label": "Badge", "score": 0.93},
    )
    deleted = mod.cv_descriptor_delete_descriptor(id=descriptor_id)

    assert updated["descriptor"]["description"] == "Visitor badge"
    assert updated["descriptor"]["threshold"] == 0.9
    assert command["command"]["sessionId"] == "cv_descriptor.work"
    assert command["command"]["session"]["targets"]["tool"] == "cv_descriptor.cv_descriptor_get_targets"
    assert event["current"]["current_match"]["id"] == descriptor_id
    assert deleted["deleted"] is True
    assert deleted["descriptors"]["items"] == []
