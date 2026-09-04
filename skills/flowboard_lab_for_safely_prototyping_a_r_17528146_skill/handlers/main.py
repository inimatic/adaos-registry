from __future__ import annotations

import os
from typing import Any, Mapping

from adaos.sdk import chat as sdk_chat
from adaos.sdk import conversation as sdk_conversation
from adaos.sdk import memory as sdk_memory
from adaos.sdk.core.decorators import tool
from adaos.sdk.data.i18n import _


def lang_res() -> dict[str, str]:
    return {}


def _skill_id() -> str:
    return str(os.getenv("ADAOS_SKILL_NAME") or "flowboard_lab_for_safely_prototyping_a_r_17528146_skill").strip() or "flowboard_lab_for_safely_prototyping_a_r_17528146_skill"


def _owner() -> str:
    return f"skill:{_skill_id()}"


def _channel_id() -> str:
    return str(os.getenv("ADAOS_SKILL_CHANNEL_ID") or _skill_id()).strip() or _skill_id()


def _agent_id(agent_id: str | None = None) -> str:
    clean = str(agent_id or "").strip()
    if clean.startswith("agent:"):
        return clean
    if clean:
        return f"agent:{_skill_id()}:{clean}"
    return f"agent:{_skill_id()}:assistant"


def _conversation_id(webspace_id: str | None = None) -> str:
    ws = str(webspace_id or "default").strip() or "default"
    return f"conv.skill.{_skill_id()}.default.{ws}"


def _dialog_context(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    data = dict(payload or {})
    meta = data.get("_meta") if isinstance(data.get("_meta"), Mapping) else {}
    runtime_context = data.get("conversation_context") if isinstance(data.get("conversation_context"), Mapping) else {}
    webspace_id = str(
        data.get("webspace_id")
        or meta.get("webspace_id")
        or runtime_context.get("webspace_id")
        or "default"
    ).strip() or "default"
    channel_id = str(
        data.get("dialog_channel_id")
        or meta.get("dialog_channel_id")
        or runtime_context.get("channel_id")
        or _channel_id()
    ).strip() or _channel_id()
    conversation_id = str(
        data.get("conversation_id")
        or meta.get("conversation_id")
        or runtime_context.get("conversation_id")
        or _conversation_id(webspace_id)
    ).strip() or _conversation_id(webspace_id)
    thread_id = str(
        data.get("thread_id")
        or data.get("conversation_topic_id")
        or meta.get("thread_id")
        or meta.get("conversation_topic_id")
        or runtime_context.get("thread_id")
        or ""
    ).strip()
    agent_id = _agent_id(str(data.get("agent_id") or meta.get("active_agent_id") or runtime_context.get("agent_id") or ""))
    sdk_conversation.open(
        conversation_id=conversation_id,
        owner=_owner(),
        webspace_id=webspace_id,
        channel_id=channel_id,
        title=_skill_id(),
        active_agent_id=agent_id,
        policy={"history": "node_ledger", "retrieval": "budgeted_context_packet"},
        meta={"template": "skill_default", "default_tool": f"{_skill_id()}.chat"},
    )
    return {
        "webspace_id": webspace_id,
        "conversation_id": conversation_id,
        "channel_id": channel_id,
        "thread_id": thread_id or None,
        "agent_id": agent_id,
        "agent_label": str(data.get("agent_label") or "Assistant"),
        "turn_trace_id": str(meta.get("turn_trace_id") or data.get("turn_trace_id") or "").strip() or None,
        "request_id": str(meta.get("request_id") or data.get("request_id") or "").strip() or None,
    }


@tool(summary="Handle one skill-owned dialog turn.", side_effects="local_write")
def chat(text: str = "", **payload: Any) -> dict[str, Any]:
    dialog = _dialog_context(payload)
    user_text = str(text or payload.get("message") or payload.get("utterance") or "").strip()
    if not user_text:
        return ask_for_details(
            question="What should I help with next?",
            required_slot="user_goal",
            max_turns=3,
            **payload,
        )
    context_packet = sdk_chat.context(
        dialog["conversation_id"],
        requester_owner=_owner(),
        channel_id=dialog["channel_id"],
        agent_id=dialog["agent_id"],
        budgets={"max_messages": 12, "max_segments": 2, "max_memory_items": 4, "max_tokens": 1200},
    )
    message = f"I captured your request: {user_text}"
    materialized = sdk_chat.send(
        message,
        conversation_id=dialog["conversation_id"],
        webspace_id=dialog["webspace_id"],
        channel_id=dialog["channel_id"],
        owner=_owner(),
        route_id="voice_chat",
        actor_id=dialog["agent_id"],
        actor_label=dialog["agent_label"],
        request_id=dialog["request_id"],
        turn_trace_id=dialog["turn_trace_id"],
        thread_id=dialog["thread_id"],
        meta={
            "response_policy": "text_tail_first",
            "context_packet": {
                "schema": context_packet.get("schema"),
                "token_estimate": context_packet.get("token_estimate"),
                "selected_sources": context_packet.get("diagnostics", {}).get("selected_sources", []),
            },
        },
    )
    return {
        "ok": True,
        "message": message,
        "dialog": dialog,
        "context_packet": context_packet,
        "response": materialized,
    }


@tool(summary="Ask a bounded follow-up question.", side_effects="local_write")
def ask_for_details(
    question: str = "Please add one concrete detail.",
    required_slot: str = "details",
    max_turns: int = 3,
    **payload: Any,
) -> dict[str, Any]:
    dialog = _dialog_context(payload)
    safe_max_turns = max(1, min(int(max_turns or 3), 5))
    materialized = sdk_chat.ask(
        str(question or "Please add one concrete detail."),
        conversation_id=dialog["conversation_id"],
        webspace_id=dialog["webspace_id"],
        channel_id=dialog["channel_id"],
        owner=_owner(),
        route_id="voice_chat",
        actor_id=dialog["agent_id"],
        actor_label=dialog["agent_label"],
        request_id=dialog["request_id"],
        turn_trace_id=dialog["turn_trace_id"],
        thread_id=dialog["thread_id"],
        meta={
            "dialog_frame": {
                "kind": "slot_collection",
                "required_slot": str(required_slot or "details"),
                "max_turns": safe_max_turns,
            },
            "answer_budget": {"max_turns": safe_max_turns},
        },
    )
    return {
        "ok": True,
        "message": str(question or "Please add one concrete detail."),
        "dialog": dialog,
        "response": materialized,
    }


@tool(summary="Propose a consent-gated skill preference memory write.", side_effects="local_write")
def remember_preference(
    key: str,
    text: str,
    confidence: float = 0.7,
    **payload: Any,
) -> dict[str, Any]:
    dialog = _dialog_context(payload)
    proposal = sdk_memory.propose_write(
        "skill_preference",
        owner=_owner(),
        key=str(key or "").strip() or "preference",
        text=str(text or "").strip(),
        confidence=max(0.0, min(float(confidence or 0.7), 1.0)),
        conversation_id=dialog["conversation_id"],
        agent_id=dialog["agent_id"],
        webspace_id=dialog["webspace_id"],
        source_ref={
            "type": "conversation",
            "conversation_id": dialog["conversation_id"],
            "thread_id": dialog["thread_id"],
        },
        reason="generated_skill_preference",
    )
    return {
        "ok": True,
        "message": "I prepared a memory proposal for review.",
        "dialog": dialog,
        "pending_action": proposal,
    }
