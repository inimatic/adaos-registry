from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Mapping

from adaos.sdk import chat as sdk_chat
from adaos.sdk import conversation as sdk_conversation
from adaos.sdk import workflow as sdk_workflow
from adaos.sdk.core.decorators import tool


SKILL_ID = "conversational_workflow_lab_skill"


def lang_res() -> dict[str, str]:
    return {}


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _context(payload: Mapping[str, Any]) -> dict[str, Any]:
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), Mapping) else {}
    nested = payload.get("conversation_context") if isinstance(payload.get("conversation_context"), Mapping) else {}
    webspace_id = str(payload.get("webspace_id") or meta.get("webspace_id") or nested.get("webspace_id") or "default").strip()
    conversation_id = str(payload.get("conversation_id") or meta.get("conversation_id") or nested.get("conversation_id") or f"conv.skill.{SKILL_ID}.{webspace_id}").strip()
    channel_id = str(payload.get("dialog_channel_id") or meta.get("dialog_channel_id") or nested.get("channel_id") or SKILL_ID).strip()
    actor_id = str(payload.get("actor_id") or meta.get("actor_id") or "user:local").strip()
    locale = str(payload.get("locale") or meta.get("locale") or "en").split("-", 1)[0].lower()
    if locale not in {"en", "ru"}:
        locale = "en"
    instance_id = f"{SKILL_ID}:{webspace_id}:{actor_id}"
    sdk_conversation.open(
        conversation_id=conversation_id,
        owner=f"skill:{SKILL_ID}",
        webspace_id=webspace_id,
        channel_id=channel_id,
        title="Workflow Lab",
        active_agent_id=f"agent:{SKILL_ID}:assistant",
        policy={"history": "node_ledger", "workflow": "governed_definition"},
        meta={"workflow_instance_id": instance_id},
    )
    return {
        "webspace_id": webspace_id,
        "conversation_id": conversation_id,
        "channel_id": channel_id,
        "actor_id": actor_id,
        "locale": locale,
        "instance_id": instance_id,
        "thread_id": str(payload.get("thread_id") or meta.get("thread_id") or "").strip() or None,
        "request_id": str(payload.get("request_id") or meta.get("request_id") or "").strip() or None,
        "turn_trace_id": str(payload.get("turn_trace_id") or meta.get("turn_trace_id") or "").strip() or None,
        "route_id": "telegram" if str(meta.get("io_type") or "").lower() == "telegram" else "dialog",
        "io_type": str(meta.get("io_type") or "web").lower(),
    }


def _message(state: str, locale: str) -> str:
    labels = {
        "en": {
            "collecting": "The request is being collected.",
            "review": "The request is ready for review.",
            "completed": "The request is approved.",
            "cancelled": "The request is cancelled.",
        },
        "ru": {
            "collecting": "Запрос находится на этапе сбора.",
            "review": "Запрос готов к проверке.",
            "completed": "Запрос одобрен.",
            "cancelled": "Запрос отменён.",
        },
    }
    return labels[locale].get(state, state)


def _present(dialog: Mapping[str, Any]) -> dict[str, Any]:
    description = sdk_workflow.describe(
        _root() / "workflow.json",
        dialog["instance_id"],
        actor_id=dialog["actor_id"],
    )
    interaction = sdk_workflow.create_interaction(
        _root() / "workflow.json",
        dialog["instance_id"],
        actor_id=dialog["actor_id"],
        conversation_id=dialog["conversation_id"],
        owner=f"skill:{SKILL_ID}",
        command_context_id=f"{SKILL_ID}:{dialog['webspace_id']}",
        prompt=_message(str(description["state"]), str(dialog["locale"])),
        thread_id=dialog["thread_id"],
        metadata={"locale": dialog["locale"], "source": "package_workflow"},
    )
    if description["terminal"]:
        sent = sdk_chat.send(
            _message(str(description["state"]), str(dialog["locale"])),
            conversation_id=dialog["conversation_id"],
            webspace_id=dialog["webspace_id"],
            channel_id=dialog["channel_id"],
            owner=f"skill:{SKILL_ID}",
            route_id=dialog["route_id"],
            actor_id=f"agent:{SKILL_ID}:assistant",
            actor_label="Workflow Lab",
            request_id=dialog["request_id"],
            turn_trace_id=dialog["turn_trace_id"],
            thread_id=dialog["thread_id"],
            meta={"io_type": dialog["io_type"], "workflow_state": description["state"]},
        )
        return {"description": description, "interaction": interaction, "materialization": sent}
    presented = sdk_chat.present(
        interaction,
        conversation_id=dialog["conversation_id"],
        owner=f"skill:{SKILL_ID}",
        webspace_id=dialog["webspace_id"],
        channel_id=dialog["channel_id"],
        route_id=dialog["route_id"],
        thread_id=dialog["thread_id"],
        actor_id=f"agent:{SKILL_ID}:assistant",
        actor_label="Workflow Lab",
        request_id=dialog["request_id"],
        turn_trace_id=dialog["turn_trace_id"],
        meta={"io_type": dialog["io_type"], "locale": dialog["locale"]},
    )
    return {"description": description, **presented}


@tool(summary="Show the governed workflow state and available actions.", side_effects="local_write")
def workflow_state(**payload: Any) -> dict[str, Any]:
    dialog = _context(payload)
    projected = _present(dialog)
    description = projected["description"]
    return {
        "ok": True,
        "message": _message(str(description["state"]), str(dialog["locale"])),
        "dialog": dialog,
        "workflow": description,
        "interaction": projected.get("interaction"),
        "presentation": projected.get("presentation"),
        "response": projected.get("materialization"),
    }


@tool(summary="Invoke one command through the governed workflow SDK.", side_effects="local_write")
def workflow_action(
    command: str,
    idempotency_key: str = "",
    confirmed: bool = False,
    **payload: Any,
) -> dict[str, Any]:
    dialog = _context(payload)
    command_id = str(command or "").strip()
    input_value = {"confirmed": True} if command_id in {"approve", "cancel"} and confirmed else {}
    result = sdk_workflow.invoke(
        _root() / "workflow.json",
        dialog["instance_id"],
        command_id,
        actor_id=dialog["actor_id"],
        idempotency_key=str(idempotency_key or f"{SKILL_ID}:{command_id}:{uuid.uuid4().hex}"),
        input_value=input_value,
        command_context_id=f"{SKILL_ID}:{dialog['webspace_id']}",
    )
    projected = _present(dialog) if result.get("accepted") else None
    description = (projected or {}).get("description") or sdk_workflow.describe(
        _root() / "workflow.json", dialog["instance_id"], actor_id=dialog["actor_id"]
    )
    return {
        "ok": bool(result.get("accepted")),
        "message": _message(str(description["state"]), str(dialog["locale"])),
        "dialog": dialog,
        "workflow": description,
        "execution": result,
        "interaction": (projected or {}).get("interaction"),
        "presentation": (projected or {}).get("presentation"),
    }


@tool(summary="Handle one package-owned conversational workflow turn.", side_effects="local_write")
def chat(text: str = "", **payload: Any) -> dict[str, Any]:
    proposal = payload.get("intent_proposal") if isinstance(payload.get("intent_proposal"), Mapping) else {}
    semantic_acts = proposal.get("semantic_acts") if isinstance(proposal, Mapping) else []
    act = next((dict(item) for item in semantic_acts or [] if isinstance(item, Mapping) and item.get("kind") == "workflow_command"), None)
    if act:
        return workflow_action(
            str(act.get("command") or ""),
            idempotency_key=str(proposal.get("proposal_id") or ""),
            confirmed=bool(payload.get("confirmed")),
            **payload,
        )
    return workflow_state(**payload)


__all__ = ["chat", "lang_res", "workflow_action", "workflow_state"]
