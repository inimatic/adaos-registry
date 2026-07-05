from __future__ import annotations

import copy
import json
import pathlib
import re
import sys

import yaml
from jsonschema import Draft202012Validator


SKILL_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))


def _find_repo_root() -> pathlib.Path:
    marker = pathlib.Path("src") / "adaos" / "abi" / "webui.v1.schema.json"
    candidates = [
        pathlib.Path.cwd(),
        SKILL_ROOT.parents[3],
        SKILL_ROOT.parents[3] / "adaos",
        *SKILL_ROOT.parents,
    ]
    for root in candidates:
        if (root / marker).exists():
            return root
    raise FileNotFoundError(f"Cannot find repo root containing {marker}")


REPO_ROOT = _find_repo_root()


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
    assert "publish_diagnostics" in tools
    assert "get_diagnostics" in manifest["exports"]["tools"]
    assert "publish_diagnostics" in manifest["exports"]["tools"]
    assert tools["get_diagnostics"]["entry"] == "handlers.main:get_diagnostics"
    assert tools["publish_diagnostics"]["entry"] == "handlers.main:publish_diagnostics"
    assert "items" in tools["get_diagnostics"]["output_schema"]["required"]
    assert "privacy" in tools["get_diagnostics"]["output_schema"]["required"]
    assert "webio.stream.snapshot.requested" in manifest["events"]["subscribe"]


def test_manifest_declares_deterministic_conversation_regex_rules() -> None:
    manifest = yaml.safe_load((SKILL_ROOT / "skill.yaml").read_text(encoding="utf-8"))

    rules = {item["id"]: item for item in manifest["nlu"]["regex_rules"]}
    assert rules["conversation.start.ru"]["intent"] == "conversation.start"
    assert rules["conversation.switch_character.ru"]["intent"] == "conversation.switch_character"
    assert rules["conversation.update_profile.ru"]["intent"] == "conversation.update_profile"
    assert rules["conversation.talk.ru"]["intent"] == "conversation.talk"

    compiled = {rule_id: re.compile(item["pattern"], re.IGNORECASE | re.UNICODE) for rule_id, item in rules.items()}
    assert compiled["conversation.start.ru"].search("поговорим")
    assert compiled["conversation.start.ru"].search("давай поговорим")
    assert compiled["conversation.switch_character.ru"].search("позови Нику").groupdict()["character_id"] == "Нику"
    assert compiled["conversation.update_profile.ru"].search("говори короче")
    assert compiled["conversation.talk.ru"].search("дай совет")


def test_webui_declares_diagnostics_modal_with_skill_sources() -> None:
    webui = json.loads((SKILL_ROOT / "webui.json").read_text(encoding="utf-8"))
    Draft202012Validator(_load_webui_schema()).validate(webui)

    app_ids = {item["id"] for item in webui["apps"]}
    assert "conversation_companions_diagnostics_app" in app_ids
    assert webui["webio"]["receivers"]["conversation_companions.diagnostics"]["snapshotPolicy"] == "on_subscribe"

    modal = webui["registry"]["modals"]["conversation_companions_diagnostics_modal"]["schema"]
    widgets = {item["id"]: item for item in modal["widgets"]}
    actions = widgets["conversation-companions-diagnostic-actions"]
    cards = widgets["conversation-companions-diagnostic-cards"]
    payload = widgets["conversation-companions-diagnostic-payload"]
    assert actions["type"] == "ui.actions"
    assert actions["actions"][0]["type"] == "callSkill"
    assert actions["actions"][0]["target"] == "conversation_companions.publish_diagnostics"
    assert cards["type"] == "ui.list"
    assert cards["dataSource"] == {
        "kind": "stream",
        "receiver": "conversation_companions.diagnostics",
    }
    assert payload["type"] == "ui.jsonViewer"
    assert payload["dataSource"] == {
        "kind": "stream",
        "receiver": "conversation_companions.diagnostics",
    }
    assert '"kind": "skill"' not in json.dumps(modal)


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

    payload = main.publish_diagnostics(webspace_id=ws)

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


def test_webio_snapshot_subscription_publishes_matching_receiver(monkeypatch) -> None:
    from handlers import main

    calls: list[tuple[str, object]] = []

    def publish_snapshot(webspace_id: str, _meta: object = None) -> dict[str, object]:
        calls.append((webspace_id, _meta))
        return {"ok": True, "items": []}

    monkeypatch.setattr(main, "_publish_diagnostics_snapshot", publish_snapshot)

    main.on_webio_stream_snapshot_requested(
        {
            "receiver": "conversation_companions.diagnostics",
            "webspace_id": "operator-desktop",
            "_meta": {"webspace_id": "operator-desktop"},
        }
    )
    main.on_webio_stream_snapshot_requested({"receiver": "other.receiver", "webspace_id": "ignored"})

    assert calls == [("operator-desktop", {"webspace_id": "operator-desktop"})]


def test_webio_snapshot_skips_unchanged_diagnostics_publish(monkeypatch) -> None:
    from handlers import main
    import adaos.sdk.io as sdk_io

    _use_isolated_memory(monkeypatch, main)
    main._DIAGNOSTICS_CACHE.clear()
    main._DIAGNOSTICS_STREAM_FINGERPRINTS.clear()

    calls: list[tuple[str, object, object]] = []
    monkeypatch.setattr(
        sdk_io,
        "stream_publish",
        lambda receiver, data, _meta=None: calls.append((receiver, data, _meta)) or {"ok": True},
    )

    event = {
        "receiver": "conversation_companions.diagnostics",
        "webspace_id": "operator-desktop",
        "_meta": {"webspace_id": "operator-desktop"},
    }

    main.on_webio_stream_subscription_changed(event)
    main.on_webio_stream_snapshot_requested(event)
    main.on_webio_stream_snapshot_requested(event)

    assert [item[0] for item in calls] == ["conversation_companions.diagnostics"]

    main.publish_diagnostics(webspace_id="operator-desktop")

    assert [item[0] for item in calls] == [
        "conversation_companions.diagnostics",
        "conversation_companions.diagnostics",
    ]
