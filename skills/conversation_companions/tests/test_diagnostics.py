from __future__ import annotations

import copy
import json
import pathlib
import sys

import yaml
from jsonschema import Draft202012Validator


SKILL_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = SKILL_ROOT.parents[3]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))


def _load_webui_schema() -> dict:
    path = REPO_ROOT / "src" / "adaos" / "abi" / "webui.v1.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _use_isolated_memory(monkeypatch, module) -> dict[str, object]:
    store: dict[str, object] = {}

    def mem_get(key: str, default: object = None) -> object:
        return copy.deepcopy(store.get(key, default))

    def mem_set(key: str, value: object) -> None:
        store[key] = copy.deepcopy(value)

    monkeypatch.setattr(module, "_mem_get", mem_get)
    monkeypatch.setattr(module, "_mem_set", mem_set)
    return store


def test_manifest_declares_diagnostics_tool() -> None:
    manifest = yaml.safe_load((SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8"))

    tools = {item["name"]: item for item in manifest["tools"]}
    assert "get_diagnostics" in tools
    assert "get_diagnostics" in manifest["exports"]["tools"]
    assert tools["get_diagnostics"]["entry"] == "handlers.main:get_diagnostics"
    assert "items" in tools["get_diagnostics"]["output_schema"]["required"]
    assert "privacy" in tools["get_diagnostics"]["output_schema"]["required"]


def test_webui_declares_diagnostics_modal_with_skill_sources() -> None:
    webui = json.loads((SKILL_ROOT / "webui.json").read_text(encoding="utf-8"))
    Draft202012Validator(_load_webui_schema()).validate(webui)

    app_ids = {item["id"] for item in webui["apps"]}
    assert "conversation_companions_diagnostics_app" in app_ids

    modal = webui["registry"]["modals"]["conversation_companions_diagnostics_modal"]["schema"]
    widgets = {item["id"]: item for item in modal["widgets"]}
    cards = widgets["conversation-companions-diagnostic-cards"]
    payload = widgets["conversation-companions-diagnostic-payload"]
    assert cards["type"] == "ui.list"
    assert cards["dataSource"] == {
        "kind": "skill",
        "name": "conversation_companions.get_diagnostics",
        "params": {"source": "webui.diagnostics"},
    }
    assert payload["type"] == "ui.jsonViewer"
    assert payload["dataSource"]["name"] == "conversation_companions.get_diagnostics"


def test_get_diagnostics_redacts_conversation_and_feedback_text(monkeypatch) -> None:
    from handlers import main

    _use_isolated_memory(monkeypatch, main)
    ws = "diagnostics-redaction"
    private_message = "private-user-message-93719"
    private_expectation = "private-expectation-92741"
    private_observation = "private-observation-91873"
    private_profile_note = "private-profile-note-94317"

    main.reset_session(webspace_id=ws)
    main.talk(private_message, preview=True, webspace_id=ws)
    main.update_profile(private_profile_note, webspace_id=ws)
    main.capture_feedback(
        rating=4,
        expectation=private_expectation,
        observation=private_observation,
        webspace_id=ws,
    )

    payload = main.get_diagnostics(webspace_id=ws)

    assert payload["ok"] is True
    assert payload["schema"] == "conversation_companions.diagnostics.v1"
    assert payload["privacy"] == {
        "conversation_text_redacted": True,
        "feedback_text_redacted": True,
    }
    assert payload["feedback"]["count"] == 1
    assert payload["profiles"]["changed_count"] >= 1
    assert {item["id"] for item in payload["items"]} >= {
        "conversation.session",
        "conversation.active_profile",
        "conversation.profile_overrides",
        "conversation.feedback",
        "conversation.safety_contract",
    }

    serialized = json.dumps(payload)
    assert private_message not in serialized
    assert private_expectation not in serialized
    assert private_observation not in serialized
    assert private_profile_note not in serialized
