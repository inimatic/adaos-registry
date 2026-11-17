"""Hermetic consumer tests. SDK doubles do not certify Core behavior."""
import ast
import hashlib
import logging
import sys
from contextvars import copy_context
from pathlib import Path
from typing import Any, Mapping, Sequence
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def _compile_consumer_code():
    path = Path(__file__).resolve().parents[1] / "handlers/main.py"
    names = {"_capture_communication_binding", "_communication_context", "get_output_routes", "request_output_routes",
             "select_output_routes", "_builder_publication_context", "_safe_emit_chat", "_handle_builder_conversation_interaction_response"}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    for function in functions:
        function.decorator_list = []
    return compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec")


_CONSUMER_CODE = _compile_consumer_code()


def _new_consumer():
    sdk = SimpleNamespace(
        communication_context=Mock(return_value={"status": "ready", "conversation_id": "owned",
                                                "owner": "skill:builder_skill", "thread_id": "thread"}),
        output_routes=Mock(return_value={"items": [{"route_id": "opaque", "transport": "telegram",
                                                   "status": "active", "selected": False, "expires_at": None}],
                                       "conversation_id": "owned", "policy_revision": 7,
                                       "automatic_external_outputs_default": "off"}),
        select_output_routes=Mock(), publish=Mock(return_value={"status": "committed"}))
    env = dict(Any=Any, Mapping=Mapping, Sequence=Sequence, hashlib=hashlib,
               logging=logging, copy_context=copy_context, CHAT_APPEND_TIMEOUT_S=0.2,
               _save_session=Mock(), sdk_builder_observability=SimpleNamespace(usd_budget=Mock(return_value={"status": "not_configured"})),
               _reply_webspace_id=lambda webspace_id, meta: webspace_id,
               _chat_meta=lambda meta, **kwargs: dict(meta or {}),
               _is_api_tool_call=lambda meta: False, sdk_conversation=sdk, sdk_access=SimpleNamespace(require=Mock(), invocation=Mock(
                   return_value={"request_id": "request"})), sdk_chat=SimpleNamespace(request=Mock()),
               DIALOG_CHANNEL_ID="builder", _LOG=logging.getLogger(__name__), _output_routes_messages=lambda locale: {"builder_output_routes.prompt": "localized prompt", "builder_output_routes.enable": "Enable {transport} ({route_id})", "builder_output_routes.disable": "Disable"})
    exec(_CONSUMER_CODE, env)
    return SimpleNamespace(**env)


@pytest.fixture
def consumer():
    return _new_consumer()


def test_consumers_have_independent_namespaces_and_mocks():
    first, second = _new_consumer(), _new_consumer()
    assert first.get_output_routes.__code__ is second.get_output_routes.__code__
    assert first.get_output_routes.__globals__ is not second.get_output_routes.__globals__
    first.get_output_routes.__globals__["DIALOG_CHANNEL_ID"] = "changed"
    assert second.get_output_routes.__globals__["DIALOG_CHANNEL_ID"] == "builder"
    first.sdk_conversation.output_routes.return_value["items"].clear()
    first.sdk_access.require.side_effect = PermissionError("first only")
    first.sdk_chat.request()
    assert second.sdk_conversation.output_routes.return_value["items"]
    assert second.get_output_routes()["status"] != "unavailable"
    second.sdk_chat.request.assert_not_called()
    first.sdk_conversation.communication_context.assert_not_called()


def test_unavailable_has_no_identity_fallback(consumer):
    consumer.sdk_conversation.communication_context.return_value = {"status": "unavailable", "reason": "no_chat"}
    assert consumer.get_output_routes("web")["status"] == "unavailable"
    assert consumer.request_output_routes("web")["status"] == "unavailable"
    assert consumer.select_output_routes([], "stored", "web")["status"] == "unavailable"
    consumer.sdk_conversation.output_routes.assert_not_called()
    consumer.sdk_conversation.select_output_routes.assert_not_called()


def test_decision_captures_policy_and_owned_chat(consumer):
    consumer.request_output_routes("web")
    call = consumer.sdk_chat.request.call_args
    assert call.kwargs["meta"] == {"purpose": "conversation.output_routes", "output_policy_revision": 7, "output_route_choices": {"disabled": [], "route_0": ["opaque"]}}
    assert call.kwargs["conversation_id"] == "owned"
    assert call.kwargs["thread_id"] == "thread"
    consumer.sdk_conversation.select_output_routes.assert_not_called()


def test_empty_routes_explicitly_unavailable(consumer):
    consumer.sdk_conversation.output_routes.return_value["items"] = []
    assert consumer.get_output_routes()["status"] == "unavailable"
    assert consumer.request_output_routes()["status"] == "unavailable"
    consumer.sdk_chat.request.assert_not_called()


@pytest.mark.parametrize("routes", [["opaque"], []])
def test_selection_passes_real_response_to_core(consumer, routes):
    consumer.select_output_routes(routes, "stored-human-response", "web")
    consumer.sdk_conversation.select_output_routes.assert_called_once_with(
        "owned", routes, response_id="stored-human-response")


def test_core_rejection_is_not_bypassed(consumer):
    consumer.sdk_conversation.select_output_routes.side_effect = PermissionError("stale/foreign/replayed")
    with pytest.raises(PermissionError):
        consumer.select_output_routes(["foreign"], "replayed")
    consumer.sdk_conversation.publish.assert_not_called()


def test_publish_retries_same_semantic_identity_without_secondary_sender(consumer):
    for _ in range(2):
        assert consumer._safe_emit_chat("Привет", webspace_id="web")
    calls = consumer.sdk_conversation.publish.call_args_list
    assert calls[0] == calls[1]
    assert calls[0].kwargs["conversation_id"] == "owned"
    assert calls[0].kwargs["text"] == "Привет"
    consumer._safe_emit_chat("revision", webspace_id="web")
    assert consumer.sdk_conversation.publish.call_args != calls[0]


def test_publish_unavailable_and_unknown_ack_do_not_fallback(consumer):
    consumer.sdk_conversation.communication_context.return_value = {"status": "unavailable"}
    assert not consumer._safe_emit_chat("output", webspace_id="web")
    consumer.sdk_conversation.publish.assert_not_called()
    consumer.sdk_conversation.communication_context.return_value = {"status": "ready", "conversation_id": "owned"}
    consumer.sdk_conversation.publish.return_value = {"status": "accepted"}
    assert not consumer._safe_emit_chat("output", webspace_id="web")


def test_legacy_actions_are_not_reinterpreted_as_interactions(consumer, monkeypatch):
    actions = [{"action_id": "review", "label": "Review"}]
    append = Mock(return_value={"ok": True, "persisted": True})
    monkeypatch.setitem(sys.modules, "adaos.sdk.io.out", SimpleNamespace(chat_append=append))
    assert consumer._safe_emit_chat("decision", webspace_id="web", actions=actions)
    assert append.call_args.kwargs["actions"] == actions
    assert append.call_args.kwargs["_meta"]["conversation_id"] == "owned"
    assert append.call_args.kwargs["_meta"]["thread_id"] == "thread"
    consumer.sdk_chat.request.assert_not_called()
    # Keep the visible action contract in its original row, and attach its
    # selected durable outputs to that same stored row without a second append.
    publication = consumer.sdk_conversation.publish.call_args.kwargs
    assert publication['conversation_id'] == 'owned'
    assert publication['existing_message_id'] == append.call_args.kwargs['msg_id']
    assert publication['publication_key'] == 'builder-stored:' + publication['existing_message_id']


def test_ambiguous_publish_failure_has_no_fallback(consumer):
    consumer.sdk_conversation.publish.side_effect = TimeoutError("unknown commit")
    assert not consumer._safe_emit_chat("output", webspace_id="web")
    consumer.sdk_chat.request.assert_not_called()
    consumer.sdk_conversation.publish.assert_called_once()


@pytest.mark.parametrize("channel", ["web", "telegram", "voice"])
@pytest.mark.parametrize("choice,expected", [("route_0", ["opaque"]), ("disabled", [])])
def test_canonical_choice_dispatches_frozen_selection(consumer, channel, choice, expected):
    consumer._handle_builder_conversation_interaction_response({
        "interaction": {"metadata": {"purpose": "conversation.output_routes",
            "output_policy_revision": 7, "output_route_choices": {"route_0": ["opaque"], "disabled": []}}},
        "response": {"response_id": "stored-human-response", "values": {"choice": choice}},
        "delivery_meta": {"webspace_id": "web", "transport": channel},
    })
    consumer.sdk_conversation.select_output_routes.assert_called_once_with(
        "owned", expected, response_id="stored-human-response")


def test_missing_or_unknown_choice_cannot_disable_routes(consumer):
    for values in ({}, {"choice": "invented"}):
        with pytest.raises(ValueError):
            consumer._handle_builder_conversation_interaction_response({
                "interaction": {"metadata": {"purpose": "conversation.output_routes", "output_route_choices": {"disabled": []}}},
                "response": {"response_id": "stored", "values": values}})
    consumer.sdk_conversation.select_output_routes.assert_not_called()


def test_decision_has_explicit_disable_and_exact_route_label(consumer):
    consumer.request_output_routes("web", locale="en")
    specification = consumer.sdk_chat.request.call_args.args[0]
    assert specification["input_spec"]["required_fields"] == ["choice"]
    assert specification["input_spec"]["choices"] == [
        {"value": "route_0", "label": "Enable telegram (opaque)"}, {"value": "disabled", "label": "Disable"}]
    assert specification["actions"][-1]["value"] == "disabled"
    assert all(action["confirmation_required"] for action in specification["actions"])


def test_ordinary_tool_replies_have_no_second_publication():
    path = Path(__file__).resolve().parents[1] / "handlers/main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for function in tree.body:
        if isinstance(function, ast.FunctionDef) and function.name in {"chat", "update_current_scenario", "set_ui_revision_current", "_builder_command_response"}:
            assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                           and node.func.id in {"_safe_emit_chat", "_schedule_safe_emit_chat"}
                           for node in ast.walk(function))


def test_late_output_uses_persisted_ingress_after_focus_changes(consumer):
    session = {"id": "task-one"}
    consumer._capture_communication_binding("web", session, {"message_id": "task-one.output"})
    consumer.sdk_conversation.communication_context.return_value = {
        "status": "ready", "conversation_id": "other", "thread_id": "other-thread",
        "owner": "skill:builder_skill"}
    consumer.sdk_access.invocation.return_value = {"request_id": "later-poll"}
    for _ in range(2):
        assert consumer._safe_emit_chat("complete", webspace_id="web", session=session)
    calls = consumer.sdk_conversation.publish.call_args_list
    assert calls[0] == calls[1]
    assert calls[0].kwargs["conversation_id"] == "owned"
    assert calls[0].kwargs["thread_id"] == "thread"
    assert "task-one.output" in calls[0].kwargs["publication_key"]
    assert "later-poll" not in calls[0].kwargs["publication_key"]
    assert consumer.sdk_conversation.communication_context.call_count == 1


def test_background_missing_or_foreign_binding_fails_closed(consumer):
    for session in ({"id": "task"}, {"communication_binding": {
            "conversation_id": "foreign", "thread_id": "thread", "owner": "skill:other"}}):
        assert not consumer._safe_emit_chat("complete", webspace_id="web", session=session)
    consumer.sdk_conversation.communication_context.assert_not_called()
    consumer.sdk_conversation.publish.assert_not_called()


def test_core_still_rejects_captured_owner_and_no_second_sender(consumer):
    session = {}
    consumer._capture_communication_binding("web", session, {"message_id": "task"})
    consumer.sdk_conversation.publish.side_effect = PermissionError("foreign owner")
    assert not consumer._safe_emit_chat("complete", webspace_id="web", session=session)
    consumer.sdk_conversation.publish.assert_called_once()


def test_background_retains_usd_budget_projection(consumer):
    session = {"scenario_id": "one"}
    consumer._capture_communication_binding("web", session, {"message_id": "task"})
    consumer.sdk_builder_observability.usd_budget.return_value = {
        "status": "ready", "committed_usd": 1, "limit_usd": 3, "available_usd": 2}
    assert consumer._safe_emit_chat("complete", webspace_id="web", session=session)
    assert "$1" in consumer.sdk_conversation.publish.call_args.kwargs["text"]
    assert "$3" in consumer.sdk_conversation.publish.call_args.kwargs["text"]


def test_nested_ingress_does_not_recapture_after_focus_changes(consumer):
    session = {"id": "task"}
    consumer._capture_communication_binding("web", session, {"message_id": "task"})
    original = dict(session["communication_binding"])
    consumer.sdk_conversation.communication_context.return_value = {
        "status": "ready", "conversation_id": "later", "thread_id": "later-thread",
        "owner": "skill:builder_skill"}
    consumer._capture_communication_binding("web", session, {"message_id": "later"})
    assert session["communication_binding"] == original
    assert consumer.sdk_conversation.communication_context.call_count == 1
