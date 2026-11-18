"""Bounded published-catalog checks; never import or execute Application code."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path, PurePosixPath
import re
import subprocess

DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?\Z")


def safe_path(root: Path, value: str) -> Path:
    relative = PurePosixPath(str(value))
    if (not value or relative.is_absolute() or ".." in relative.parts
            or "\\" in value or ":" in value):
        raise ValueError("unsafe_catalog_path")
    target = root.joinpath(*relative.parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("catalog_path_escapes_checkout")
    return target


def read_json(path: Path) -> dict:
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("catalog_record_too_large")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("catalog_object_required")
    return value


def canonical_digest(value: dict) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def published_projects(root: Path) -> dict:
    path = root / "semantic/catalog/index.json"
    if not path.is_file():
        return {}
    index = read_json(path)
    result = {}
    for app_id, app in index.get("applications", {}).items():
        digest = app.get("stable_release_digest")
        if not digest:
            continue
        entry = index.get("releases", {}).get(app.get("release_key"))
        if not isinstance(entry, dict) or entry.get("release_digest") != digest:
            raise ValueError("public_application_release_binding_missing:" + app_id)
        record = read_json(safe_path(root, entry["path"]))
        if (canonical_digest({k: v for k, v in record.items() if k != "projection_digest"})
                != record.get("projection_digest") or record.get("projection_digest") != entry.get("projection_digest")):
            raise ValueError("public_application_projection_digest_mismatch:" + app_id)
        release = record.get("release") or {}
        project = release.get("project_release") or {}
        if (release.get("application_id") != app_id or release.get("release_digest") != digest
                or project.get("release_digest") != digest or (record.get("channels") or {}).get("stable") != digest):
            raise ValueError("public_application_release_identity_mismatch:" + app_id)
        identifier = project["project_id"]
        components = project["components"]
        primary = next((m["ref"] for m in (project.get("composition_lock") or {}).get("members", [])
                        if m.get("role") == "primary"), None)
        package = next((c for c in components if c["kind"] + ":" + c["artifact_id"] == primary), components[0])
        result[identifier] = {"version": project["version"], "release": identifier + "@" + project["version"],
            "release_digest": digest, "source_revision": project["source_ref"]["revision"],
            "package_digest": package["digest"]}
    return result


def validate(root: Path, *, retire_unavailable: bool = False, synchronize_published: bool = False) -> dict:
    root = root.resolve()
    catalog = read_json(root / "registry.json")
    original_digest = canonical_digest(catalog)
    try:
        tracked = subprocess.run(["git", "-C", str(root), "ls-files", "-v", "--",
            "skills/*/skill.yaml", "scenarios/*/scenario.yaml", "projects/*/project.yaml"],
            capture_output=True, text=True, encoding="utf-8", timeout=10)
        sparse_manifests = {line[2:] for line in tracked.stdout.splitlines() if line.startswith("S ")}
    except (OSError, subprocess.TimeoutExpired):
        sparse_manifests = set()
    if catalog.get("version") != 2:
        raise ValueError("registry_version_2_required")
    semantic_path = root / "semantic/index.json"
    semantic = read_json(semantic_path).get("application_releases", {}) if semantic_path.is_file() else {}
    errors, retired, counts = [], [], {}
    published = published_projects(root)
    if synchronize_published:
        existing = {e.get("id"): e for e in catalog.get("projects", [])}
        for identifier, pointer in published.items():
            entry = existing.get(identifier)
            if entry is None:
                source = "projects/" + identifier
                entry = {"kind": "project", "id": identifier, "name": identifier, "path": source,
                    "manifest": source + "/project.yaml", "source": {"path": source, "manifest": source + "/project.yaml"},
                    "install": {"kind": "project", "id": identifier, "name": identifier}}
                catalog.setdefault("projects", []).append(entry)
            entry["version"] = pointer["version"]
            entry.setdefault("channels", {})["stable"] = pointer
    indexed = {e.get("id"): e for e in catalog.get("projects", [])}
    for identifier, pointer in published.items():
        existing = indexed.get(identifier)
        if (existing is None or existing.get("version") != pointer["version"]
                or (existing.get("channels") or {}).get("stable", {}).get("release_digest") != pointer["release_digest"]):
            errors.append({"entry": "projects:" + identifier, "code": "published_catalog_projection_stale"})
    for collection, kind in (("skills", "skill"), ("scenarios", "scenario"), ("projects", "project")):
        rows = catalog.get(collection)
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ValueError("invalid_registry_collection:" + collection)
        seen, retained = set(), []
        for entry in rows:
            if not isinstance(entry, dict):
                raise ValueError("invalid_registry_entry")
            name = str(entry.get("name") or entry.get("id") or "")
            identity = collection + ":" + name
            faults = []
            if not name or name in seen:
                faults.append("duplicate_or_empty_identity")
            seen.add(name)
            if entry.get("kind") != kind or not VERSION.fullmatch(str(entry.get("version") or "")):
                faults.append("kind_or_version_invalid")
            source = entry.get("source") or {}
            source_path = str(entry.get("path") or source.get("path") or "")
            manifest_path = str(entry.get("manifest") or source.get("manifest") or "")
            manifest = safe_path(root, manifest_path)
            directory = safe_path(root, source_path)
            if (not source_path.startswith(collection + "/") or manifest.parent != directory
                    or manifest.name != kind + ".yaml"):
                faults.append("manifest_path_mismatch")
            channels = entry.get("channels") or {}
            if not isinstance(channels, dict):
                faults.append("channels_object_required")
                channels = {}
            has_retained_release = False
            for channel, pointer in channels.items():
                if not isinstance(pointer, dict):
                    faults.append("invalid_channel:" + channel)
                    continue
                if (not VERSION.fullmatch(str(pointer.get("version") or ""))
                        or any(not DIGEST.fullmatch(str(pointer.get(field) or ""))
                               for field in ("release_digest", "package_digest"))):
                    faults.append("invalid_channel_identity:" + channel)
                    continue
                project = str(pointer.get("release") or "").split("@", 1)[0]
                key = "project:" + project + "@" + pointer["release_digest"]
                binding = semantic.get(key)
                if not isinstance(binding, dict):
                    faults.append("published_release_binding_missing:" + channel)
                    continue
                record = read_json(safe_path(root, str(binding.get("path") or "")))
                if (record.get("project_id") != project
                        or record.get("project_release_digest") != pointer["release_digest"]
                        or binding.get("project_release_digest") != pointer["release_digest"]
                        or (kind == "project" and record.get("version") != pointer["version"])
                        or record.get("projection_digest") != binding.get("projection_digest")
                        or canonical_digest({k: v for k, v in record.items() if k != "projection_digest"})
                            != record.get("projection_digest")):
                    faults.append("published_release_binding_mismatch:" + channel)
                else:
                    has_retained_release = True
            if not manifest.is_file() and manifest_path not in sparse_manifests and not has_retained_release:
                if retire_unavailable and all(f.startswith("published_release_binding_missing:") for f in faults):
                    retired.append({"collection": collection, "entry": entry,
                                    "reason": "source_and_verified_distribution_unavailable"})
                    continue
                faults.append("source_and_verified_distribution_unavailable")
            if kind == "project" and channels.get("stable", {}).get("version") not in {None, entry.get("version")}:
                faults.append("project_index_differs_from_stable_version")
            errors.extend({"entry": identity, "code": fault} for fault in faults)
            retained.append(entry)
        counts[collection] = len(retained)
        catalog[collection] = retained
    if ((synchronize_published or (retire_unavailable and retired)) and not errors
            and canonical_digest(catalog) != original_digest):
        catalog.setdefault("retired_entries", []).extend(retired)
        catalog["updated_at"] = datetime.now(timezone.utc).isoformat()
        temporary = root / ".tmp" / "registry-catalog-validation" / "registry.json"
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(root / "registry.json")
    return {"ok": not errors, "counts": counts, "errors": errors, "retired_count": len(retired)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--retire-unavailable", action="store_true")
    parser.add_argument("--synchronize-published", action="store_true")
    args = parser.parse_args()
    result = validate(args.root, retire_unavailable=args.retire_unavailable, synchronize_published=args.synchronize_published)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
