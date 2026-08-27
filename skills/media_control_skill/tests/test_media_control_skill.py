from __future__ import annotations

import sys
import importlib.util
import json
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from media_control.repository import MediaControlRepository  # noqa: E402


_HANDLER_SPEC = importlib.util.spec_from_file_location(
    "media_control_skill_handlers_main", SKILL_ROOT / "handlers" / "main.py"
)
assert _HANDLER_SPEC and _HANDLER_SPEC.loader
main = importlib.util.module_from_spec(_HANDLER_SPEC)
_HANDLER_SPEC.loader.exec_module(main)


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CONTROL_DB_PATH", str(tmp_path / "media_control.sqlite3"))
    main._ACTIVE_NOW_PLAYING_PROJECTIONS.clear()
    yield
    main._ACTIVE_NOW_PLAYING_PROJECTIONS.clear()


def _queue(count: int = 3) -> list[dict]:
    return [
        {
            "item_id": f"item-{index}",
            "work_id": f"work-{index}",
            "variant_id": f"variant-{index}",
            "source_id": f"source-{index}",
            "title": f"Episode {index}",
            "available": index != 1,
            "route": {"mode": "direct_agent_to_endpoint", "path": f"/media/{index}"},
        }
        for index in range(count)
    ]


def _session(repository: MediaControlRepository, *, actor: str = "profile:alice") -> dict:
    target = repository.register_target(
        "browser-tv",
        webspace_id="tv",
        label="Living room TV",
        kind="tv",
        capabilities={"video": True, "pip": True},
    )["target"]
    return repository.create_session(
        profile_id="alice",
        target_id=target["id"],
        actor_ref=actor,
        queue=_queue(12),
        route={"mode": "direct_agent_to_endpoint", "source_node_id": "node-a"},
    )["session"]


def test_session_queue_is_persistent_and_server_paged():
    repository = MediaControlRepository()
    session = _session(repository)

    page = repository.get_session(session["id"], queue_limit=100)["session"]

    assert page["state"] == "requested"
    assert page["autoplay"] is True
    assert page["auto_fullscreen"] is True
    assert page["route"]["source_node_id"] == "node-a"
    assert page["queue"]["count"] == 12
    assert page["queue"]["total_count"] == 12
    assert page["queue"]["pagination"]["has_more"] is False
    assert page["queue"]["pagination"]["limit"] == 30


def test_queue_replacement_can_select_item_and_reset_playback_state():
    repository = MediaControlRepository()
    session = _session(repository)
    with repository.connect() as connection:
        connection.execute(
            "UPDATE playback_sessions SET state='playing',position_ms=42000,duration_ms=90000 WHERE id=?",
            (session["id"],),
        )
        connection.commit()

    result = repository.update_queue(
        session["id"],
        queue=_queue(3),
        expected_queue_revision=session["queue_revision"],
        active_index=2,
        actor_ref="profile:alice",
    )

    updated = result["session"]
    assert updated["active_queue_index"] == 2
    assert updated["active_item_id"] == "item-2"
    assert updated["position_ms"] == 0
    assert updated["duration_ms"] == 0
    assert updated["state"] == "ready"
    assert updated["route"]["path"] == "/media/2"


def test_now_playing_exposes_human_titles_and_target_labels():
    repository = MediaControlRepository()
    session = _session(repository)

    projection = repository.now_playing(
        profile_id="alice",
        target_id=session["target_id"],
        limit=1,
    )

    assert projection["count"] == 1
    assert projection["items"][0]["title"] == "Episode 0"
    assert projection["items"][0]["target_label"] == "Living room TV"
    assert projection["items"][0]["target_kind"] == "tv"


def test_terminal_and_error_sessions_leave_now_playing():
    repository = MediaControlRepository()
    session = _session(repository)
    with repository.connect() as connection:
        connection.execute(
            "UPDATE playback_sessions SET state='error' WHERE id=?",
            (session["id"],),
        )
        connection.commit()

    assert repository.now_playing(profile_id="alice")["items"] == []


def test_stale_endpoint_session_is_not_projected_as_now_playing():
    repository = MediaControlRepository()
    session = _session(repository)
    with repository.connect() as connection:
        connection.execute(
            """
            UPDATE playback_sessions
            SET state='playing',created_at='2000-01-01T00:00:00+00:00',
                endpoint_last_seen_at='2000-01-01T00:00:01+00:00'
            WHERE id=?
            """,
            (session["id"],),
        )
        connection.commit()

    projection = repository.now_playing(profile_id="alice")

    assert projection["items"] == []
    assert projection["freshness_seconds"] == 300
    assert repository.get_session(session["id"])["session"]["state"] == "playing"


def test_checkpoint_handler_publishes_profile_observation(monkeypatch):
    repository = MediaControlRepository()
    session = _session(repository)
    published = []
    monkeypatch.setattr(main, "_repository", lambda: repository)
    monkeypatch.setattr(main, "_publish_updates", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "adaos.sdk.data.events.publish",
        lambda kind, payload, **kwargs: published.append((kind, payload, kwargs)),
    )

    result = main.checkpoint(
        session_id=session["id"],
        position_ms=42_000,
        duration_ms=120_000,
        state="paused",
        expected_revision=session["revision"],
        webspace_id="desktop",
    )

    assert result["ok"] is True
    assert published[0][0] == "media_control.playback.observed"
    assert published[0][1]["position_ms"] == 42_000
    assert published[0][1]["duration_ms"] == 120_000
    assert published[0][1]["session_revision"] == session["revision"] + 1
    assert published[0][1]["profile_id"] == "alice"
    assert published[0][1]["item_id"] == "item-0"
    assert published[0][1]["webspace_id"] == "desktop"
    assert published[0][1]["playback_confirmed"] is True


def test_target_projection_exposes_device_endpoint_and_authorization_labels():
    repository = MediaControlRepository()
    repository.register_target(
        "browser-desktop",
        webspace_id="desktop",
        label="Chrome on Windows",
        kind="browser",
        capabilities={
            "device_display_name": "My PC",
            "endpoint_display_name": "Chrome on Windows",
            "authorization_state": "authorized",
            "authorized": True,
        },
    )

    target = main.list_targets(limit=1)["items"][0]

    assert target["display_label"] == "My PC"
    assert target["device_label"] == "My PC"
    assert target["endpoint_label"] == "Chrome on Windows"
    assert target["authorization_state"] == "authorized"
    assert target["authorization_label"]
    assert target["authorization_label_i18n"] == {
        "key": "runtime.media_control.ui.authorized"
    }


def test_endpoint_inbox_registers_idle_target_and_returns_bounded_active_window():
    repository = MediaControlRepository()

    idle = repository.endpoint_inbox(
        "browser-android-tv",
        webspace_id="tv",
        label="Living room TV",
        kind="tv",
        capabilities={"video": True},
    )
    target = idle["target"]
    session = repository.create_session(
        profile_id="alice",
        target_id=target["id"],
        actor_ref="profile:alice",
        queue=_queue(60),
        active_index=40,
    )["session"]

    assigned = repository.endpoint_inbox(
        "browser-android-tv",
        webspace_id="tv",
        label="Living room TV",
        kind="tv",
        capabilities={"video": True},
        queue_limit=30,
    )

    assert idle["assignment"] is None
    assert assigned["assignment"]["id"] == session["id"]
    assert assigned["changed"] is True
    queue = assigned["assignment"]["queue"]
    assert queue["count"] == 30
    assert queue["total_count"] == 60
    assert any(item["item_id"] == "item-40" for item in queue["items"])
    unchanged = repository.endpoint_inbox(
        "browser-android-tv",
        webspace_id="tv",
        label="Living room TV",
        kind="tv",
        known_session_id=session["id"],
    )
    assert unchanged["changed"] is False


def test_stale_targets_are_not_available_to_remote_controllers():
    repository = MediaControlRepository()
    registered = repository.register_target(
        "browser-stale",
        webspace_id="tv",
        label="Old TV tab",
        kind="tv",
        capabilities={"presence_mode": "heartbeat"},
    )["target"]
    with repository.connect() as connection:
        connection.execute(
            "UPDATE playback_targets SET last_seen_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (registered["id"],),
        )
        connection.commit()

    assert repository.get_target(registered["id"])["status"] == "unavailable"
    assert repository.list_targets()["items"] == []
    unavailable = repository.list_targets(include_unavailable=True)["items"]
    assert unavailable[0]["status"] == "unavailable"


def test_remote_session_creation_publishes_targeted_assignment(monkeypatch):
    repository = MediaControlRepository()
    target = repository.register_target(
        "browser-android-tv",
        webspace_id="tv",
        label="Living room TV",
        kind="tv",
    )["target"]
    published = []
    monkeypatch.setattr(main, "_repository", lambda: repository)
    monkeypatch.setattr(main, "_publish_updates", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "adaos.sdk.data.events.publish",
        lambda kind, payload, **kwargs: published.append((kind, payload, kwargs)),
    )

    result = main.create_session(
        target_id=target["id"],
        profile_id="alice",
        actor_ref="profile:alice",
        queue=_queue(3),
        webspace_id="desktop",
    )

    assignment = next(item for item in published if item[0] == "media_control.playback.assigned")
    assert assignment[1]["session_id"] == result["session"]["id"]
    assert assignment[1]["endpoint_id"] == "browser-android-tv"
    assert assignment[1]["adapter"]["inbox_method"] == "endpoint_inbox"


def test_parameterized_now_playing_projection_is_ready_and_kept_current(monkeypatch):
    repository = MediaControlRepository()
    session = _session(repository)
    published = []

    import adaos.sdk.io as sdk_io

    monkeypatch.setattr(main, "_repository", lambda: repository)
    monkeypatch.setattr(
        sdk_io,
        "stream_variable_publish",
        lambda receiver, value, **kwargs: published.append((receiver, value, kwargs)),
    )
    params = {"profile_id": "alice", "target_id": session["target_id"]}

    main.on_now_playing_subscription_changed(
        {
            "receiver": "media_control.now_playing",
            "action": "subscribed",
            "webspace_id": "desktop",
            "params": params,
        }
    )

    assert published[-1][1]["count"] == 1
    assert published[-1][2]["_meta"] == {
        "webspace_id": "desktop",
        "params": params,
    }

    repository.command(
        session["id"],
        command="play",
        arguments={},
        actor_ref="profile:alice",
        expected_revision=session["revision"],
        idempotency_key="projection-play",
    )
    main._publish_updates(repository)

    assert published[-1][1]["items"][0]["state"] == "playing"
    assert published[-1][2]["_meta"]["params"] == params


def test_now_playing_projection_registry_is_bounded():
    for index in range(main._ACTIVE_PROJECTION_LIMIT + 7):
        main._remember_projection(
            f"desktop-{index}",
            {"profile_id": "alice", "target_id": f"target-{index}"},
        )

    assert len(main._ACTIVE_NOW_PLAYING_PROJECTIONS) == main._ACTIVE_PROJECTION_LIMIT
    assert all("desktop-0" not in key for key in main._ACTIVE_NOW_PLAYING_PROJECTIONS)


def test_endpoint_open_retires_previous_session_and_scopes_command_pull():
    first = main.open_endpoint_session(
        endpoint_id="browser-tv",
        webspace_id="tv",
        label="Living room TV",
        kind="tv",
        profile_id="alice",
        queue=_queue(3),
        active_index=0,
    )
    repository = MediaControlRepository()
    command = repository.command(
        first["session"]["id"],
        command="play",
        arguments={},
        actor_ref="profile:alice",
        expected_revision=first["session"]["revision"],
        idempotency_key="first-play",
    )
    second = main.open_endpoint_session(
        endpoint_id="browser-tv",
        webspace_id="tv",
        label="Living room TV",
        kind="tv",
        profile_id="alice",
        queue=_queue(2),
        active_index=1,
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["retired_session_count"] == 1
    assert second["session"]["active_item_id"] == "item-1"
    assert repository.get_session(first["session"]["id"])["session"]["state"] == "stopped"
    scoped = repository.pull_commands(
        second["target"]["id"], session_id=second["session"]["id"]
    )
    previous = repository.pull_commands(
        second["target"]["id"], session_id=first["session"]["id"]
    )
    assert scoped["items"] == []
    assert previous["items"][0]["id"] == command["command"]["id"]


def test_commands_are_revision_safe_idempotent_and_lease_guarded():
    repository = MediaControlRepository()
    session = _session(repository)

    played = repository.command(
        session["id"],
        command="play",
        arguments={},
        actor_ref="profile:alice",
        expected_revision=session["revision"],
        idempotency_key="play-1",
    )
    replay = repository.command(
        session["id"],
        command="play",
        arguments={},
        actor_ref="profile:alice",
        expected_revision=session["revision"],
        idempotency_key="play-1",
    )
    stale = repository.command(
        session["id"],
        command="pause",
        arguments={},
        actor_ref="profile:alice",
        expected_revision=session["revision"],
        idempotency_key="pause-stale",
    )
    foreign = repository.command(
        session["id"],
        command="pause",
        arguments={},
        actor_ref="profile:bob",
        expected_revision=played["session"]["revision"],
        idempotency_key="pause-bob",
    )

    assert played["session"]["state"] == "playing"
    assert replay["idempotent_replay"] is True
    assert replay["command"]["id"] == played["command"]["id"]
    assert stale["error"] == "playback_revision_conflict"
    assert foreign["error"] == "playback_control_lease_conflict"


def test_autonext_skips_unavailable_queue_items():
    repository = MediaControlRepository()
    session = _session(repository)

    result = repository.command(
        session["id"],
        command="next",
        arguments={},
        actor_ref="profile:alice",
        expected_revision=session["revision"],
        idempotency_key="next-1",
    )

    assert result["session"]["active_queue_index"] == 2
    assert result["session"]["active_item_id"] == "item-2"
    assert result["session"]["state"] == "playing"


def test_checkpoint_and_target_command_cursor_support_recovery():
    repository = MediaControlRepository()
    session = _session(repository)
    played = repository.command(
        session["id"], command="play", arguments={}, actor_ref="profile:alice",
        expected_revision=session["revision"], idempotency_key="play-recovery",
    )
    checkpoint = repository.checkpoint(
        session["id"], position_ms=42_000, duration_ms=180_000, state="paused",
        source="app_shell", expected_revision=played["session"]["revision"],
    )
    commands = repository.pull_commands(session["target_id"], limit=1)
    acknowledged = repository.acknowledge_command(commands["items"][0]["id"], status="applied", result={"position_ms": 0})

    assert checkpoint["session"]["position_ms"] == 42_000
    assert checkpoint["session"]["checkpoint_at"]
    assert commands["count"] == 1
    assert commands["next_cursor"]
    assert acknowledged["command"]["status"] == "applied"


def test_settings_and_handoff_are_profile_target_scoped():
    repository = MediaControlRepository()
    session = _session(repository)
    speaker = repository.register_target(
        "speaker-kitchen", webspace_id="kitchen", label="Kitchen", kind="speaker",
        capabilities={"audio": True},
    )["target"]
    settings = repository.set_settings(
        profile_id="alice",
        target_id=session["target_id"],
        values={"autoplay": False, "auto_fullscreen": False, "video_close_policy": "pause"},
    )
    handed = repository.command(
        session["id"], command="handoff", arguments={"target_id": speaker["id"]},
        actor_ref="profile:alice", expected_revision=session["revision"], idempotency_key="handoff-1",
    )

    assert settings["settings"]["autoplay"] is False
    assert settings["settings"]["auto_fullscreen"] is False
    assert handed["session"]["target_id"] == speaker["id"]
    assert handed["session"]["state"] == "recovering"
    assert handed["session"]["interruption"]["reason"] == "handoff"


def test_voice_control_requires_clarification_for_multiple_sessions():
    repository = MediaControlRepository()
    first = _session(repository)
    second_target = repository.register_target(
        "browser-phone", webspace_id="phone", label="Phone", kind="mobile"
    )["target"]
    second = repository.create_session(
        profile_id="alice", target_id=second_target["id"], actor_ref="profile:alice", queue=_queue(2)
    )["session"]
    for item, key in ((first, "play-first"), (second, "play-second")):
        repository.command(
            item["id"], command="play", arguments={}, actor_ref="profile:alice",
            expected_revision=item["revision"], idempotency_key=key,
        )

    result = main.voice_command(action="pause", profile_id="alice", actor_ref="profile:alice")

    assert result["ok"] is False
    assert result["error"] == "playback_target_ambiguous"
    assert len(result["clarification"]["options"]) == 2


def test_voice_toggle_resolves_to_revisioned_play_and_pause_commands():
    repository = MediaControlRepository()
    session = _session(repository)

    played = main.voice_command(
        action="toggle",
        session_id=session["id"],
        actor_ref="profile:alice",
    )
    paused = main.voice_command(
        action="toggle",
        session_id=session["id"],
        actor_ref="profile:alice",
    )

    assert played["command"]["command"] == "play"
    assert played["session"]["state"] == "playing"
    assert paused["command"]["command"] == "pause"
    assert paused["session"]["state"] == "paused"


def test_public_playback_contracts_validate_strictly():
    jsonschema = pytest.importorskip("jsonschema")
    repository = MediaControlRepository()
    session = _session(repository)
    played = repository.command(
        session["id"], command="play", arguments={}, actor_ref="profile:alice",
        expected_revision=session["revision"], idempotency_key="schema-play",
    )
    payloads = {
        "playback-target.v1.schema.json": played["session"]["target"],
        "playback-session.v1.schema.json": played["session"],
        "playback-command.v1.schema.json": played["command"],
        "playback-queue.v1.schema.json": played["session"]["queue"],
    }
    import json

    for filename, payload in payloads.items():
        schema = json.loads((SKILL_ROOT / "schemas" / filename).read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(payload)


def test_endpoint_reconciliation_is_idempotent_and_acks_command_revisions():
    repository = MediaControlRepository()
    session = _session(repository)
    played = repository.command(
        session["id"],
        command="play",
        arguments={},
        actor_ref="profile:alice",
        expected_revision=session["revision"],
        idempotency_key="reconcile-play",
    )
    observed = {
        "active_item_id": session["active_item_id"],
        "state": "playing",
        "position_ms": 42_000,
        "duration_ms": 180_000,
        "rate": 1.0,
        "volume": 0.8,
        "muted": False,
    }

    pending = repository.reconcile_endpoint(
        session["id"],
        target_id=session["target_id"],
        endpoint_revision=1,
        acknowledged_command_revision=0,
        observed=observed,
    )
    replay = repository.reconcile_endpoint(
        session["id"],
        target_id=session["target_id"],
        endpoint_revision=1,
        acknowledged_command_revision=0,
        observed=observed,
    )
    converged = repository.reconcile_endpoint(
        session["id"],
        target_id=session["target_id"],
        endpoint_revision=2,
        acknowledged_command_revision=played["command"]["command_revision"],
        observed=observed,
    )

    assert pending["action"]["type"] == "replay_commands"
    assert replay["idempotent_replay"] is True
    assert replay["action"] == pending["action"]
    assert converged["action"] == {
        "type": "noop",
        "reason": "endpoint_state_accepted",
    }
    assert converged["session"]["position_ms"] == 42_000
    assert converged["session"]["observed_command_revision"] == 1
    assert repository.pull_commands(session["target_id"])["items"][0]["status"] == "applied"

    import json

    schema = json.loads(
        (SKILL_ROOT / "schemas" / "endpoint-reconciliation.v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    pytest.importorskip("jsonschema").Draft202012Validator(schema).validate(
        converged
    )


def test_coordinator_preferred_reconcile_does_not_repeat_seek():
    repository = MediaControlRepository()
    session = _session(repository)
    checkpoint = repository.checkpoint(
        session["id"],
        position_ms=60_000,
        duration_ms=180_000,
        state="paused",
        source="app_shell",
        expected_revision=session["revision"],
    )
    observed = {
        "active_item_id": session["active_item_id"],
        "state": "paused",
        "position_ms": 10_000,
        "duration_ms": 180_000,
    }

    first = repository.reconcile_endpoint(
        session["id"],
        target_id=session["target_id"],
        endpoint_revision=1,
        acknowledged_command_revision=0,
        observed=observed,
        authority="coordinator_preferred",
    )
    replay = repository.reconcile_endpoint(
        session["id"],
        target_id=session["target_id"],
        endpoint_revision=1,
        acknowledged_command_revision=0,
        observed=observed,
        authority="coordinator_preferred",
    )

    assert checkpoint["session"]["position_ms"] == 60_000
    assert first["action"] == {
        "type": "seek",
        "position_ms": 60_000,
        "reason": "coordinator_checkpoint_newer",
    }
    assert replay["action"] == first["action"]
    assert replay["idempotent_replay"] is True


def test_endpoint_preferred_reconcile_accepts_autonomous_queue_advance():
    repository = MediaControlRepository()
    session = _session(repository)

    advanced = repository.reconcile_endpoint(
        session["id"],
        target_id=session["target_id"],
        endpoint_revision=1,
        acknowledged_command_revision=0,
        observed={
            "active_item_id": "item-2",
            "state": "playing",
            "position_ms": 1500,
            "duration_ms": 180_000,
            "rate": 1,
            "volume": 0.75,
            "muted": False,
        },
        authority="endpoint_preferred",
    )

    assert advanced["action"] == {
        "type": "noop",
        "reason": "endpoint_queue_advance_accepted",
    }
    assert advanced["session"]["active_queue_index"] == 2
    assert advanced["session"]["active_item_id"] == "item-2"
    assert advanced["session"]["state"] == "playing"
    assert advanced["session"]["position_ms"] == 1500


def test_sleep_timer_expires_to_a_durable_pause_command():
    repository = MediaControlRepository()
    session = _session(repository)
    playing = repository.command(
        session["id"],
        command="play",
        arguments={},
        actor_ref="profile:alice",
        expected_revision=session["revision"],
        idempotency_key="timer-play",
    )
    timer = repository.command(
        session["id"],
        command="sleep_timer",
        arguments={"seconds": 60},
        actor_ref="profile:alice",
        expected_revision=playing["session"]["revision"],
        idempotency_key="timer-arm",
    )
    with repository.connect() as connection:
        connection.execute(
            "UPDATE playback_sessions SET sleep_timer_at=? WHERE id=?",
            (1, session["id"]),
        )
        connection.commit()

    expired = repository.apply_due_sleep_timers()
    current = repository.get_session(session["id"])["session"]
    commands = repository.pull_commands(session["target_id"])["items"]

    assert timer["session"]["sleep_timer_at"] > 0
    assert expired["applied_session_ids"] == [session["id"]]
    assert current["state"] == "paused"
    assert current["sleep_timer_at"] == 0
    assert commands[-1]["command"] == "pause"
    assert commands[-1]["arguments"]["reason"] == "sleep_timer"


def test_settings_inherit_profile_defaults_and_qoe_is_observable():
    repository = MediaControlRepository()
    session = _session(repository)
    repository.set_settings(
        profile_id="alice",
        values={
            "auto_fullscreen": False,
            "preferred_rate": 1.5,
            "checkpoint_interval_seconds": 20,
        },
    )
    inherited = repository.get_settings(
        profile_id="alice", target_id=session["target_id"]
    )["settings"]
    repository.record_qoe(session["id"], metric="first_frame_ms", value=450)
    repository.record_qoe(session["id"], metric="first_frame_ms", value=550)
    repository.record_qoe(
        session["id"], metric="rebuffer_ms", value=100, dimensions={"route": "direct"}
    )
    summary = repository.qoe_summary(session_id=session["id"], limit=2)

    assert inherited["auto_fullscreen"] is False
    assert inherited["preferred_rate"] == 1.5
    assert inherited["checkpoint_interval_seconds"] == 20
    assert inherited["inherited_from_profile"] is True
    metrics = {item["metric"]: item for item in summary["metrics"]}
    assert metrics["first_frame_ms"]["average"] == 500
    assert summary["count"] == 2
    assert summary["bounded"] is True


def test_declared_stream_subscriptions_have_runtime_handlers():
    source = (SKILL_ROOT / "handlers" / "main.py").read_text(encoding="utf-8")

    assert '@subscribe("sys.ready")' in source
    assert '"webio.stream.snapshot.requested"' in source
    assert '"webio.stream.subscription.changed"' in source
    assert 'receivers=("media_control.now_playing",)' in source


def test_media_remote_surfaces_are_owned_by_media_control_skill():
    webui = json.loads((SKILL_ROOT / "webui.json").read_text(encoding="utf-8"))

    assert webui["resources"]["media_control.i18n.en"]["path"] == "assets/i18n/en.json"
    assert webui["resources"]["media_control.i18n.ru"]["path"] == "assets/i18n/ru.json"
    assert (SKILL_ROOT / "assets" / "i18n" / "en.json").is_file()
    assert (SKILL_ROOT / "assets" / "i18n" / "ru.json").is_file()
    assert webui["apps"][0]["id"] == "media_remote_app"
    assert webui["widgets"][0]["id"] == "media_remote_compact"
    assert {
        (item["extensionPoint"], item["id"])
        for item in webui["contributions"]
    } == {
        ("desktop.apps", "media_remote_app"),
        ("desktop.widgets", "media_remote_compact"),
    }
    modal = webui["registry"]["modals"]["media_control_remote_modal"]
    widgets = {item["id"]: item for item in modal["schema"]["widgets"]}
    assert widgets["media-control-target"]["type"] == "input.selector"
    assert widgets["media-control-target"]["inputs"]["optionLabelPath"] == "display_label"
    assert widgets["media-control-target"]["inputs"]["optionMetaPaths"][:2] == [
        "authorization_label",
        "endpoint_label",
    ]
    assert widgets["media-control-now-playing"]["inputs"]["titleKey"] == "title"
    assert widgets["media-control-transport"]["actions"][1]["target"] == (
        "media_control_skill.voice_command"
    )
    assert [
        button["id"]
        for button in widgets["media-control-transport"]["inputs"]["buttons"]
    ] == ["previous", "toggle", "next", "stop"]
    assert widgets["media-control-transport"]["actions"][1]["params"]["action"] == (
        "toggle"
    )
    compact = webui["widgets"][0]
    assert [button["id"] for button in compact["inputs"]["buttons"]] == [
        "open",
        "previous",
        "toggle",
        "next",
        "stop",
    ]


def test_media_control_runtime_uses_only_public_adaos_sdk() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for root in (SKILL_ROOT / "handlers", SKILL_ROOT / "media_control")
        for path in sorted(root.rglob("*.py"))
    )

    assert "from adaos.sdk" in source
    assert "adaos.services" not in source
    assert "adaos.apps" not in source
    assert "adaos.domain" not in source
