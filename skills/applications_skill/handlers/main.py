from __future__ import annotations

import json
import os
from typing import Any, Mapping

from adaos.sdk import chat as sdk_chat
from adaos.sdk import conversation as sdk_conversation
from adaos.sdk import memory as sdk_memory
from adaos.sdk import access, applications, development_tickets
from adaos.sdk.core.decorators import tool
from adaos.sdk.data.i18n import _


@tool(summary='Read Core dashboard positions for an Application.', side_effects='read_only', permissions=['applications.read'])
def list_dashboard_positions(application_id: str) -> dict[str, Any]:
    access.require('applications.read')
    model = applications.get_application(application_id)
    if not isinstance(model, Mapping) or (model.get('application') or {}).get('application_id') != application_id:
        raise ValueError('Application is unavailable')
    dashboard = model.get('dashboard')
    positions = dashboard.get('positions') if isinstance(dashboard, Mapping) else None
    if not isinstance(positions, list) or len(positions) > 5 or any(not isinstance(row, Mapping) for row in positions):
        raise ValueError('Application dashboard positions are unavailable')
    result = {'items': positions}
    if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > 65536:
        raise ValueError('Application dashboard response exceeds the allowed size')
    return result


def _ticket_scope(application_id: str) -> tuple[set[tuple[str, str]], set[str]]:
    """Resolve only canonical application-owned ticket targets."""
    access.require('applications.read')
    model = applications.get_application(application_id)
    app = model.get('application') if isinstance(model, Mapping) else None
    if not isinstance(app, Mapping) or app.get('application_id') != application_id:
        raise ValueError('Application composition is unavailable')
    project = app.get('legacy_project_id')
    if not isinstance(project, str) or not project.strip():
        raise ValueError('Application project identity is unavailable')
    project = project.removeprefix('project:')
    targets = {('application', application_id.removeprefix('application:')), ('project', project)}
    inventory = model.get('component_inventory')
    if not isinstance(inventory, list):
        raise ValueError('Application component inventory is unavailable')
    components: set[str] = set()
    for component in inventory:
        if not isinstance(component, Mapping):
            raise ValueError('Invalid Application component inventory')
        if component.get('ownership') != 'owned':
            continue
        ref = component.get('component_ref')
        if not isinstance(ref, str) or ':' not in ref:
            raise ValueError('Invalid owned component identity')
        kind, identifier = ref.split(':', 1)
        if kind not in ('scenario', 'skill') or not identifier:
            raise ValueError('Invalid owned component identity')
        components.add(ref)
        targets.add((kind, identifier))
    if not components:
        raise ValueError('Application owned components are unavailable')
    return targets, components


def _scoped_ticket(ticket: Any, targets: set[tuple[str, str]], components: set[str]) -> bool:
    if not isinstance(ticket, Mapping) or not isinstance(ticket.get('ticket_id'), str):
        return False
    target = ticket.get('target_scope')
    if not isinstance(target, Mapping):
        return False
    kind, identifier = target.get('type'), target.get('id')
    if not isinstance(kind, str) or not isinstance(identifier, str):
        return False
    identifier = identifier.removeprefix(kind + ':')
    if (kind, identifier) not in targets:
        return False
    ref = ticket.get('component_ref')
    # Application/project tickets may identify their aggregate as component_ref.
    return not ref or ref in components or (kind in ('application', 'project') and ref == kind + ':' + identifier)


def _ticket_actor() -> str:
    subject = access.caller()
    if not isinstance(subject, Mapping):
        raise PermissionError('Authenticated caller is required')
    identifier = subject.get('id') or subject.get('subject_id')
    if not isinstance(identifier, str) or not identifier.strip():
        raise PermissionError('Authenticated caller identity is unavailable')
    return identifier


def _get_scoped_ticket(application_id: str, ticket_id: str) -> dict[str, Any]:
    targets, components = _ticket_scope(application_id)
    ticket = development_tickets.get_ticket(ticket_id)
    if not _scoped_ticket(ticket, targets, components) or ticket.get('ticket_id') != ticket_id:
        raise ValueError('Ticket is unavailable in this Application')
    return dict(ticket)


def _bounded_ticket_projection(value: dict[str, Any]) -> dict[str, Any]:
    if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > 65536:
        raise ValueError('Ticket response exceeds the allowed size')
    return value


@tool(summary='List development tickets scoped to an Application.', side_effects='read_only', permissions=['applications.read', 'development.read.tickets'])
def list_application_tickets(application_id: str, search: str = '', status: str = '') -> dict[str, Any]:
    access.require('development.read.tickets')
    targets, components = _ticket_scope(application_id)
    found: dict[str, dict[str, Any]] = {}
    filters = {'limit': 100}
    if search.strip():
        filters['search'] = search.strip()
    if status and status != 'all':
        filters['status'] = status
    queries = [{'target_id': identifier} for _, identifier in sorted(targets)]
    queries.extend({'component_ref': ref} for ref in sorted(components))
    for query in queries:
        tickets = development_tickets.list_tickets(**query, **filters)
        if not isinstance(tickets, list):
            raise ValueError('Ticket list is unavailable')
        for ticket in tickets:
            if _scoped_ticket(ticket, targets, components):
                found[ticket['ticket_id']] = {
                    key: (str(ticket.get(key) or '')[:500] if key == 'summary' else ticket.get(key))
                    for key in ('ticket_id', 'summary', 'status', 'revision', 'updated_at')
                }
    items = sorted(found.values(), key=lambda ticket: str(ticket.get('updated_at') or ''), reverse=True)
    return _bounded_ticket_projection({'items': items[:100]})


@tool(summary='Read an Application ticket and its history.', side_effects='read_only', permissions=['applications.read', 'development.read.tickets'])
def show_application_ticket(application_id: str, ticket_id: str) -> dict[str, Any]:
    access.require('development.read.tickets')
    ticket = _get_scoped_ticket(application_id, ticket_id)
    events = development_tickets.list_events(ticket_id=ticket_id, limit=20)
    if not isinstance(events, list):
        raise ValueError('Ticket history is unavailable')
    comments = {comment['id']: comment.get('body', '') for comment in ticket.get('comments', [])
                if isinstance(comment, Mapping) and isinstance(comment.get('id'), str)}
    history = []
    for event in events[-20:]:
        if not isinstance(event, Mapping) or event.get('ticket_id') != ticket_id or not isinstance(event.get('payload'), Mapping):
            continue
        payload = event['payload']
        body = comments.get(payload.get('comment_id'), '') if payload.get('kind') == 'commented' else payload.get('body', '')
        history.append({'event_id': event.get('event_id'), 'kind': payload.get('kind'),
                        'body': str(body or '')[:2000], 'recorded_at': event.get('recorded_at')})
    return _bounded_ticket_projection({
        'ticket': {key: ticket.get(key) for key in ('ticket_id', 'summary', 'status', 'revision', 'updated_at')},
        'events': history,
    })


@tool(summary='Submit an Application development request.', side_effects='runtime_write', permissions=['applications.read', 'development.write.tickets'])
def create_application_ticket(application_id: str, summary: str) -> dict[str, Any]:
    access.require('development.write.tickets')
    actor = _ticket_actor()
    targets, components = _ticket_scope(application_id)
    project = next(identifier for kind, identifier in targets if kind == 'project')
    if not summary.strip() or len(summary) > 2000:
        raise ValueError('A ticket summary is required')
    result = development_tickets.create_ticket(summary.strip(), kind='development_request', target_scope={'type': 'project', 'id': project}, actor=actor)
    if not isinstance(result, Mapping) or result.get('ok') is not True or not _scoped_ticket(result.get('ticket'), targets, components) or not isinstance(result.get('ticket_duplicate'), bool):
        raise ValueError('Ticket creation was not acknowledged')
    return {'ok': True, 'ticket_id': result['ticket']['ticket_id'], 'duplicate': result['ticket_duplicate']}


@tool(summary='Comment on an Application development ticket.', side_effects='runtime_write', permissions=['applications.read', 'development.read.tickets', 'development.write.tickets'])
def comment_application_ticket(application_id: str, ticket_id: str, body: str, expected_revision: int) -> dict[str, Any]:
    access.require('development.write.tickets')
    access.require('development.read.tickets')
    actor = _ticket_actor()
    ticket = _get_scoped_ticket(application_id, ticket_id)
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1 or ticket.get('revision') != expected_revision:
        raise ValueError('Ticket revision changed; refresh before commenting')
    if not body.strip() or len(body) > 10000:
        raise ValueError('A comment is required')
    result = development_tickets.operate_ticket(ticket_id, 'comment', actor=actor, payload={'body': body.strip()}, expected_revision=expected_revision)
    updated = result.get('ticket') if isinstance(result, Mapping) else None
    if not isinstance(updated, Mapping) or updated.get('ticket_id') != ticket_id or not isinstance(updated.get('revision'), int) or updated['revision'] <= expected_revision:
        raise ValueError('Comment was not acknowledged')
    return {'ok': True, 'ticket_id': ticket_id, 'revision': updated['revision']}


def lang_res() -> dict[str, str]:
    return {}


def _skill_id() -> str:
    return str(os.getenv("ADAOS_SKILL_NAME") or "applications_skill").strip() or "applications_skill"


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


@tool(summary="Handle one skill-owned dialog turn.", side_effects="local_write", permissions=['applications.develop'])
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


@tool(summary="Ask a bounded follow-up question.", side_effects="local_write", permissions=['applications.develop'])
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


@tool(summary="Propose a consent-gated skill preference memory write.", side_effects="local_write", permissions=['applications.develop'])
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
