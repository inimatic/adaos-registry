from __future__ import annotations

import sys
import importlib.util
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

    assert page["state"] == "ready"
    assert page["autoplay"] is True
    assert page["auto_fullscreen"] is True
    assert page["route"]["source_node_id"] == "node-a"
    assert page["queue"]["count"] == 12
    assert page["queue"]["total_count"] == 12
    assert page["queue"]["pagination"]["has_more"] is False
    assert page["queue"]["pagination"]["limit"] == 30


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
    assert 'receivers=("media_control.now_playing",)' in source
