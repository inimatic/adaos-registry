"""Conversational discovery does not create an unused browser read surface."""
from pathlib import Path
import yaml

def test_output_routes_are_conversational_only():
    manifest = yaml.safe_load((Path(__file__).resolve().parents[1] / "skill.yaml").read_text(encoding="utf-8"))
    assert not any(row.get("tool") == "get_output_routes" for row in manifest["data_routes"])
    assert "get_output_routes" in manifest["exports"]["tools"]

