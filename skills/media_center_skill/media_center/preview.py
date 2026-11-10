"""Consume Core-verified preview identity; never derive it from tool input."""
from adaos.sdk.data import preview_binding, skill_env
from pathlib import Path

MAPPING = [{"logical_ref": "media.catalog",
            "implementation_ref": "skill:media_center_skill:fixture",
            "status": "fixture_only"}]
TITLES = (
    "Aurora Station", "Glass Harbor", "The Last Observatory", "Northern Lights",
    "Quiet Orbit", "Ocean Signals", "Morning Frequency", "The Clockwork Garden",
)


def binding():
    value = preview_binding.preview_data_binding()
    if value and value.get("read_policy") == "none":
        raise PermissionError("preview_data_profile_read_denied")
    if value and (value.get("mode") == "fixture"
                  or value.get("profile_id") == "demo-fixture"
                  or value.get("source_ref") == "fixture:media_center:demo-v1"):
        expected = {
            "profile_id": "demo-fixture", "mode": "fixture",
            "source_ref": "fixture:media_center:demo-v1",
            "schema": "adaos.builder.materialized_data_binding.v1",
            "owner": "project:media_center", "read_policy": "fixture",
            "write_policy": "none", "capabilities": ["media.catalog"],
            "implementation_mappings": MAPPING,
            "logical_schema_ref": "adaos.media.catalog.v1",
            "sensitivity": "internal", "redaction": "none", "expires_at": None,
            "runtime_source": "isolated_preview", "provider_opened": False,
            "ready": True,
        }
        if any(key not in value or value[key] != item for key, item in expected.items()):
            raise PermissionError("preview_fixture_binding_mismatch")
    return value


def catalog_path():
    return Path(skill_env.skill_data_root()) / "db" / "media_center.sqlite3"


def prepare_fixture_read():
    """Initialize the tiny isolated fixture before the interactive read budget."""
    value = binding()
    if value and value.get("mode") == "fixture":
        from adaos.sdk.access import require
        require("workspace.read")
        from .catalog import MediaCenterRepository
        MediaCenterRepository()


def initialize_fixture(repository, value):
    """The verified, bounded fixture bootstrap is the only preview read write.

    Ordinary interactive reads remain read-only. Initialize both existing schema
    projections before entering that path, using only the verified isolated root.
    No background worker, provider or live discovery participates.
    """
    if not value or value.get("mode") != "fixture":
        return
    if binding() != value or repository.db_path.resolve() != catalog_path().resolve():
        raise PermissionError("preview_fixture_binding_mismatch")
    from .read_models import read_deadline
    if read_deadline():
        return
    from .coordinator import MediaCatalogCoordinator
    repository.db_path.parent.mkdir(parents=True, exist_ok=True)
    repository.ensure_schema()
    created = seed(repository, value)
    coordinator = MediaCatalogCoordinator(repository)
    if created:
        coordinator.refresh_search_index(force_legacy=True)


def seed(repository, value):
    """Bootstrap only a verified fixture, atomically and without external IO."""
    if not value or value.get("mode") != "fixture":
        return
    # Recheck at the actual bootstrap boundary, before creating any rows.
    if binding() != value or repository.db_path.resolve() != catalog_path().resolve():
        raise PermissionError("preview_fixture_binding_mismatch")
    from .catalog import _normalize_resource, _json_dumps
    with repository.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT source, resource_id FROM catalog_items").fetchall()
        expected_ids = {f"demo-v1-{index:02}" for index in range(1, 9)}
        if existing:
            if {(row["source"], row["resource_id"]) for row in existing} != {
                ("demo_fixture", key) for key in expected_ids
            }:
                raise PermissionError("preview_fixture_store_mismatch")
            return False
        for index, title in enumerate(TITLES, 1):
            row = _normalize_resource({
                "source": "demo_fixture", "resource_id": f"demo-v1-{index:02}",
                "name": title + (".mp4" if index <= 4 else ".flac"), "title": title,
                "mime_type": "video/mp4" if index <= 4 else "audio/flac",
                "modified_at": "2026-01-01T00:00:00Z",
                "metadata": {"demonstration": True, "fixture": "demo-v1"},
            })
            connection.execute(
                "INSERT INTO catalog_items (id,source,resource_id,name,title,media_kind,"
                "mime_type,descriptor_json,metadata_json,fingerprint,indexed_at,last_seen_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(row[key] for key in ("id", "source", "resource_id", "name", "title", "media_kind", "mime_type"))
                + (_json_dumps(row["descriptor"]), _json_dumps(row["metadata"]), row["fingerprint"],
                   "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
        return True
