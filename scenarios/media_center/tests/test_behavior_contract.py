"""Synthetic consumer checks; no external providers or deployed runtime."""
import importlib.util
from pathlib import Path
import sys

import pytest
from adaos.sdk import access
from adaos.sdk.data import skill_env

ROOT = Path(__file__).resolve().parents[3]
CONTROL = ROOT / "skills/media_control_skill"
if str(CONTROL) not in sys.path:
    sys.path.insert(0, str(CONTROL))
from media_control.repository import MediaControlRepository, default_db_path


def test_runtime_path_uses_owned_sdk_root_and_fails_closed(monkeypatch, tmp_path):
    monkeypatch.delenv("MEDIA_CONTROL_DB_PATH", raising=False)
    monkeypatch.setattr(skill_env, "skill_data_root", lambda: tmp_path)
    assert default_db_path() == tmp_path / "db/media_control.sqlite3"
    def unavailable():
        raise RuntimeError("runtime unavailable")
    monkeypatch.setattr(skill_env, "skill_data_root", unavailable)
    with pytest.raises(RuntimeError, match="runtime unavailable"):
        default_db_path()


def test_settings_survive_repository_recreation_and_handler_denial(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_CONTROL_DB_PATH", str(tmp_path / "control.sqlite3"))
    spec = importlib.util.spec_from_file_location("behavior_control_handlers", CONTROL / "handlers/main.py")
    handlers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(handlers)
    calls = []
    monkeypatch.setattr(access, "require", lambda permission: calls.append(permission))
    result = handlers.set_settings(profile_id="synthetic", settings={"autoplay": False})
    assert result["ok"] is True
    before = MediaControlRepository().get_settings(profile_id="synthetic")
    def deny(permission):
        raise PermissionError("denied")
    monkeypatch.setattr(access, "require", deny)
    with pytest.raises(PermissionError):
        handlers.set_settings(profile_id="synthetic", settings={"autoplay": True})
    assert MediaControlRepository().get_settings(profile_id="synthetic") == before
    assert calls == ["workspace.write"]
