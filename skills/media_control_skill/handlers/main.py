from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data.i18n import _


_SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from media_control.repository import SCHEMA_VERSION, MediaControlRepository, text  # noqa: E402


def _repository() -> MediaControlRepository:
    return MediaControlRepository()


def _error(code: str, fallback: str, **extra: Any) -> dict[str, Any]:
    key = f"runtime.media_control.error.{code}"
    try:
        translated = text(_(key))
    except Exception:
        translated = ""
    return {
        "ok": False,
        "schema": SCHEMA_VERSION,
        "error": code,
        "human_message": translated if translated and translated != key else fallback,
        "human_message_i18n": {"key": key},
        **extra,
    }


def _publish_snapshot(repository: MediaControlRepository, *, webspace_id: str = "") -> None:
    try:
        from adaos.sdk.io import stream_variable_publish

        snapshot = repository.now_playing(limit=20)
        stream_variable_publish(
            "media_control.now_playing",
            snapshot,
            var_id="media_control.sessions",
            ttl_ms=120000,
            _meta={"webspace_id": webspace_id} if webspace_id else None,
        )
    except Exception:
        return


def _result_or_error(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("ok"):
        return result
    code = text(result.get("error")) or "media_control_error"
    fallbacks = {
        "endpoint_id_required": "The playback endpoint identity is missing.",
        "invalid_target_kind": "The playback endpoint type is not supported.",
        "playback_target_unavailable": "The selected playback target is unavailable.",
        "playback_target_not_found": "The playback target is no longer available.",
        "playback_queue_empty": "Choose at least one playable item.",
        "playback_session_not_found": "The playback session is no longer available.",
        "playback_revision_conflict": "Playback changed on another controller. Refresh and retry.",
        "playback_queue_revision_conflict": "The queue changed on another controller. Refresh and retry.",
        "playback_control_lease_conflict": "Another controller currently owns this playback session.",
        "idempotency_key_required": "The command could not be sent safely. Retry from the current state.",
        "unsupported_playback_command": "This playback command is not supported.",
        "invalid_media_control_cursor": "The playback list changed. Refresh it.",
        "endpoint_revision_required": "The playback endpoint revision is missing.",
        "stale_endpoint_observation": "A newer playback endpoint state is already known.",
        "invalid_acknowledged_command_revision": "The endpoint acknowledged an unknown playback command.",
        "playback_target_session_mismatch": "This playback session belongs to another endpoint.",
        "invalid_reconciliation_authority": "The playback recovery policy is invalid.",
    }
    return {**result, **_error(code, fallbacks.get(code, "The media control operation failed."))}


def _event_payload(event: Any) -> dict[str, Any]:
    if isinstance(event, Mapping):
        payload = event.get("payload")
        return dict(payload) if isinstance(payload, Mapping) else dict(event)
    payload = getattr(event, "payload", None)
    return dict(payload) if isinstance(payload, Mapping) else {}


@subscribe("sys.ready")
def on_sys_ready(_: Any) -> None:
    repository = _repository()
    repository.apply_due_sleep_timers()
    _publish_snapshot(repository)


@subscribe(
    "webio.stream.snapshot.requested",
    receivers=("media_control.now_playing",),
)
def on_now_playing_snapshot_requested(event: Any) -> None:
    payload = _event_payload(event)
    if text(payload.get("receiver")) != "media_control.now_playing":
        return
    _publish_snapshot(
        _repository(), webspace_id=text(payload.get("webspace_id"))
    )


@tool(summary="Ensure the durable Media Center control-plane schema.", side_effects="local_write")
def ensure_schema(**_: Any) -> dict[str, Any]:
    return _repository().ensure_schema()


@tool(summary="Rehydrate bounded now-playing state after activation.", side_effects="none")
def rehydrate(webspace_id: str = "", **_: Any) -> dict[str, Any]:
    repository = _repository()
    _publish_snapshot(repository, webspace_id=webspace_id)
    return repository.diagnostics()


@tool(summary="Register a browser, TV, phone, speaker, or native playback target.", side_effects="local_write")
def register_target(
    endpoint_id: str = "",
    webspace_id: str = "",
    label: str = "",
    kind: str = "browser",
    node_id: str = "",
    capabilities: Mapping[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    result = _repository().register_target(
        endpoint_id,
        webspace_id=webspace_id,
        label=label,
        kind=kind,
        node_id=node_id,
        capabilities=capabilities,
    )
    return _result_or_error(result)


@tool(summary="List bounded playback targets visible to a controller.", side_effects="none")
def list_targets(include_unavailable: bool = False, limit: int = 50, **_: Any) -> dict[str, Any]:
    return _repository().list_targets(include_unavailable=bool(include_unavailable), limit=limit)


@tool(summary="Create a persistent playback session with a bounded queue.", side_effects="local_write")
def create_session(
    target_id: str = "",
    profile_id: str = "default",
    actor_ref: str = "",
    queue: list[Mapping[str, Any]] | None = None,
    active_index: int = 0,
    route: Mapping[str, Any] | None = None,
    queue_source: Mapping[str, Any] | None = None,
    lease_seconds: int = 120,
    webspace_id: str = "",
    **_: Any,
) -> dict[str, Any]:
    repository = _repository()
    result = repository.create_session(
        profile_id=profile_id,
        target_id=target_id,
        actor_ref=actor_ref,
        queue=queue or [],
        active_index=active_index,
        route=route,
        queue_source=queue_source,
        lease_seconds=lease_seconds,
    )
    if result.get("ok"):
        _publish_snapshot(repository, webspace_id=webspace_id)
    return _result_or_error(result)


@tool(summary="Read one playback session and a bounded queue page.", side_effects="none")
def get_session(session_id: str = "", queue_limit: int = 10, queue_cursor: str = "", **_: Any) -> dict[str, Any]:
    try:
        return _result_or_error(_repository().get_session(session_id, queue_limit=queue_limit, queue_cursor=queue_cursor))
    except ValueError:
        return _error("invalid_media_control_cursor", "The playback list changed. Refresh it.")


@tool(summary="Send one revision-safe idempotent playback command.", side_effects="local_write")
def command(
    session_id: str = "",
    command: str = "",
    arguments: Mapping[str, Any] | None = None,
    actor_ref: str = "",
    expected_revision: int = 0,
    idempotency_key: str = "",
    lease_seconds: int = 120,
    webspace_id: str = "",
    **_: Any,
) -> dict[str, Any]:
    repository = _repository()
    try:
        result = repository.command(
            session_id,
            command=command,
            arguments=arguments,
            actor_ref=actor_ref,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            lease_seconds=lease_seconds,
        )
    except ValueError as exc:
        result = {"ok": False, "error": str(exc)}
    if result.get("ok"):
        _publish_snapshot(repository, webspace_id=webspace_id)
    return _result_or_error(result)


@tool(summary="Replace a playback queue with optimistic revision control.", side_effects="local_write")
def update_queue(
    session_id: str = "",
    queue: list[Mapping[str, Any]] | None = None,
    expected_queue_revision: int = 0,
    actor_ref: str = "",
    webspace_id: str = "",
    **_: Any,
) -> dict[str, Any]:
    repository = _repository()
    result = repository.update_queue(
        session_id,
        queue=queue or [],
        expected_queue_revision=expected_queue_revision,
        actor_ref=actor_ref,
    )
    if result.get("ok"):
        _publish_snapshot(repository, webspace_id=webspace_id)
    return _result_or_error(result)


@tool(summary="Persist an endpoint playback checkpoint for interruption recovery.", side_effects="local_write")
def checkpoint(
    session_id: str = "",
    position_ms: int = 0,
    duration_ms: int = 0,
    state: str = "paused",
    source: str = "endpoint",
    expected_revision: int = 0,
    webspace_id: str = "",
    **_: Any,
) -> dict[str, Any]:
    repository = _repository()
    result = repository.checkpoint(
        session_id,
        position_ms=position_ms,
        duration_ms=duration_ms,
        state=state,
        source=source,
        expected_revision=expected_revision,
    )
    if result.get("ok"):
        _publish_snapshot(repository, webspace_id=webspace_id)
    return _result_or_error(result)


@tool(summary="Pull ordered target commands through an opaque cursor.", side_effects="none")
def pull_commands(target_id: str = "", cursor: str = "", limit: int = 50, **_: Any) -> dict[str, Any]:
    try:
        return _result_or_error(_repository().pull_commands(target_id, cursor=cursor, limit=limit))
    except ValueError:
        return _error("invalid_media_control_cursor", "The command cursor changed. Refresh it.")


@tool(summary="Acknowledge endpoint application of one playback command.", side_effects="local_write")
def acknowledge_command(command_id: str = "", status: str = "applied", result: Mapping[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    return _result_or_error(_repository().acknowledge_command(command_id, status=status, result=result))


@tool(summary="Reconcile one endpoint after reconnect without duplicate actions.", side_effects="local_write")
def reconcile_endpoint(
    session_id: str = "",
    target_id: str = "",
    endpoint_revision: int = 0,
    acknowledged_command_revision: int = 0,
    observed: Mapping[str, Any] | None = None,
    authority: str = "endpoint_preferred",
    webspace_id: str = "",
    **_: Any,
) -> dict[str, Any]:
    repository = _repository()
    result = repository.reconcile_endpoint(
        session_id,
        target_id=target_id,
        endpoint_revision=endpoint_revision,
        acknowledged_command_revision=acknowledged_command_revision,
        observed=observed,
        authority=authority,
    )
    if result.get("ok"):
        _publish_snapshot(repository, webspace_id=webspace_id)
    return _result_or_error(result)


@tool(summary="Read effective profile and target playback settings.", side_effects="none")
def get_settings(profile_id: str = "default", target_id: str = "", **_: Any) -> dict[str, Any]:
    return _repository().get_settings(profile_id=profile_id, target_id=target_id)


@tool(summary="Update profile and target playback settings.", side_effects="local_write")
def set_settings(profile_id: str = "default", target_id: str = "", values: Mapping[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    return _result_or_error(_repository().set_settings(profile_id=profile_id, target_id=target_id, values=dict(values or {})))


@tool(summary="Return bounded now-playing sessions for remote controls.", side_effects="none")
def now_playing(profile_id: str = "", target_id: str = "", limit: int = 20, **_: Any) -> dict[str, Any]:
    return _result_or_error(_repository().now_playing(profile_id=profile_id, target_id=target_id, limit=limit))


@tool(summary="Record one bounded playback quality metric.", side_effects="local_write")
def record_qoe(session_id: str = "", metric: str = "", value: float = 0, dimensions: Mapping[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    return _result_or_error(_repository().record_qoe(session_id, metric=metric, value=value, dimensions=dimensions))


@tool(summary="Return bounded aggregate and recent playback QoE evidence.", side_effects="none")
def qoe_summary(
    session_id: str = "",
    target_id: str = "",
    limit: int = 30,
    **_: Any,
) -> dict[str, Any]:
    return _result_or_error(
        _repository().qoe_summary(
            session_id=session_id, target_id=target_id, limit=limit
        )
    )


@tool(summary="Resolve a voice transport command against current playback context.", side_effects="local_write")
def voice_command(
    action: str = "",
    profile_id: str = "default",
    target_id: str = "",
    session_id: str = "",
    actor_ref: str = "",
    arguments: Mapping[str, Any] | None = None,
    idempotency_key: str = "",
    **_: Any,
) -> dict[str, Any]:
    repository = _repository()
    resolved_session = text(session_id)
    if not resolved_session:
        candidates = repository.now_playing(profile_id=profile_id, target_id=target_id, limit=5)
        items = candidates.get("items") or []
        if not items:
            return _error("no_active_playback", "There is no active playback session.")
        if len(items) > 1:
            return {
                "ok": False,
                "schema": SCHEMA_VERSION,
                "error": "playback_target_ambiguous",
                "clarification": {
                    "prompt": "Which playback target should I control?",
                    "options": [{"session_id": item["id"], "target_id": item["target_id"]} for item in items],
                },
            }
        resolved_session = items[0]["id"]
    current = repository.get_session(resolved_session)
    if not current.get("ok"):
        return _result_or_error(current)
    return command(
        session_id=resolved_session,
        command=action,
        arguments=arguments,
        actor_ref=actor_ref or f"profile:{profile_id}",
        expected_revision=current["session"]["revision"],
        idempotency_key=idempotency_key or f"voice:{resolved_session}:{action}:{current['session']['revision']}",
    )


@tool(summary="Return compact control-plane diagnostics.", side_effects="none")
def status(**_: Any) -> dict[str, Any]:
    return _repository().diagnostics()
