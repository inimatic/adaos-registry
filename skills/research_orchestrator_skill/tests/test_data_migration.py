from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    path = Path(__file__).resolve().parents[1] / "migrations" / "data_migration.py"
    spec = importlib.util.spec_from_file_location("research_orchestrator_data_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_copies_durable_state_but_not_transient_uploads(tmp_path: Path) -> None:
    source = tmp_path / "v1" / "data"
    target = tmp_path / "v2" / "data"
    (source / "db").mkdir(parents=True)
    (source / "db" / "research.sqlite3").write_text("durable", encoding="utf-8")
    (source / "internal").mkdir()
    (source / "internal" / "binding.json").write_text("{}", encoding="utf-8")
    (source / "files" / "uploads").mkdir(parents=True)
    (source / "files" / "uploads" / "private.ipynb").write_bytes(b"private")
    (source / "files" / "receipts").mkdir()
    (source / "files" / "receipts" / "receipt.json").write_text("{}", encoding="utf-8")

    result = _load_migration().migrate(
        {"source_data_root": str(source), "target_data_root": str(target)}
    )

    assert result["ok"] is True
    assert result["source_deleted"] is False
    assert "files/uploads" in result["excluded"]
    assert (target / "db" / "research.sqlite3").read_text(encoding="utf-8") == "durable"
    assert (target / "internal" / "binding.json").is_file()
    assert (target / "files" / "receipts" / "receipt.json").is_file()
    assert not (target / "files" / "uploads").exists()
    assert (source / "files" / "uploads" / "private.ipynb").is_file()
