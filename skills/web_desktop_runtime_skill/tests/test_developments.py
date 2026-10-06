from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from handlers import main


@pytest.mark.parametrize("limit,expected", [(1, 1), (6, 6), (40, 40), (500, 40)])
def test_development_query_is_bounded_at_sdk_boundary(monkeypatch, limit, expected):
    require = Mock()
    read = Mock(return_value=[{"id": "project", "title": "Project"}])
    monkeypatch.setattr(main.sdk_access, "require", require)
    monkeypatch.setattr(main.sdk_applications, "list_development_projects", read)

    result = main.list_developments(limit=limit, query=" desktop ", profile=" local ")

    require.assert_called_once_with("workspace.read")
    read.assert_called_once_with(limit=expected, query="desktop", profile="local")
    assert result["items"][0]["id"] == "project"


def test_development_denial_precedes_sdk_read(monkeypatch):
    monkeypatch.setattr(main.sdk_access, "require", Mock(side_effect=PermissionError("denied")))
    read = Mock()
    monkeypatch.setattr(main.sdk_applications, "list_development_projects", read)
    with pytest.raises(PermissionError):
        main.list_developments(limit=6)
    read.assert_not_called()


def test_empty_development_selection_does_not_scan_projects(monkeypatch):
    monkeypatch.setattr(main.sdk_access, "require", Mock())
    read = Mock()
    monkeypatch.setattr(main.sdk_applications, "list_development_projects", read)
    assert main.list_developments(require_selection=True)["items"] == []
    read.assert_not_called()
