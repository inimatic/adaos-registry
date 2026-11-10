"""Domain unit tests use an explicit granted SDK boundary double.

Access denial is exercised separately in the project application checkpoint;
these tests do not qualify Core authentication or provisioning.
"""
import pytest
from adaos.sdk import access
from adaos.sdk.data import skill_env


@pytest.fixture(autouse=True)
def granted_sdk_boundary(monkeypatch, tmp_path):
    monkeypatch.setattr(skill_env, "skill_data_root", lambda: tmp_path)
    monkeypatch.setattr(access, "require", lambda capability: {"capability": capability})
