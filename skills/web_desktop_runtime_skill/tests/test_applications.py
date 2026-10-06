from pathlib import Path
import sys
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from handlers import main  # noqa: E402


def _model(application_id: str, *, kind: str = "application") -> dict:
    return {
        "application": {
            "application_id": application_id,
            "kind": kind,
            "owner_application_id": "builder" if kind == "project" else None,
            "display": {"title": application_id.replace("_", " ").title()},
            "entrypoints": [{"presentation_ref": f"scenario:{application_id}"}],
        },
        "installed": True,
    }


def _setup(monkeypatch, *, show_projects: bool, pinned: list[str] | None = None) -> None:
    monkeypatch.setattr(main.sdk_access, "require", Mock())
    monkeypatch.setattr(
        main,
        "_read_application_models",
        Mock(return_value=[_model("mail"), _model("managed_project", kind="project")]),
    )
    monkeypatch.setattr(
        main,
        "_preference_record",
        Mock(return_value={"showProjects": show_projects}),
    )
    monkeypatch.setattr(main, "_pinned_application_ids", Mock(return_value=pinned or []))


def test_default_discovery_hides_managed_projects(monkeypatch):
    _setup(monkeypatch, show_projects=False)

    result = main.list_applications()

    assert [item["id"] for item in result["items"]] == ["mail"]


def test_show_projects_preference_adds_managed_projects(monkeypatch):
    _setup(monkeypatch, show_projects=True)

    result = main.list_applications()

    assert [item["id"] for item in result["items"]] == ["mail", "managed_project"]
    project = next(item for item in result["items"] if item["id"] == "managed_project")
    assert project["application_kind"] == "project"
    assert project["owner_application_id"] == "builder"


def test_explicit_home_pin_is_stronger_than_hidden_project_discovery(monkeypatch):
    _setup(monkeypatch, show_projects=False, pinned=["managed_project"])

    result = main.list_applications(pinned_only=True, webspace_id="desktop")

    assert [item["id"] for item in result["items"]] == ["managed_project"]
    assert result["items"][0]["pinned"] is True
